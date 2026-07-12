"""Build orchestration: city config -> LiDAR -> rasters -> horizon -> COGs.

The pipeline rasterizes the *padded* bbox (city bbox plus the horizon
buffer) so every pixel of the city proper sees its obstacles, sweeps the
horizon for the inner window only, and crops all exports back to the city
bbox -- every artifact shares one shape and georeference, as the engine's
``ShadeScene`` requires.
"""

import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path

from shade_core.artifacts import (
    BLOCKER_CLASS_FILENAME,
    DSM_FILENAME,
    DTM_FILENAME,
    HORIZON_FILENAME,
    LANDCOVER_FILENAME,
    METADATA_FILENAME,
    ArtifactInput,
    BuildMetadata,
    HorizonBuildParams,
)
from shade_core.config import CityConfig
from shade_core.shade import NO_BLOCKER, Landcover
from shade_pipeline.cog import write_cog
from shade_pipeline.grid import buffer_pixels, grid_shape, padded_bbox, transform_from_bbox
from shade_pipeline.horizon import ANGLE_MAX_DEG, HorizonParams, compute_horizon_tiled
from shade_pipeline.progress import format_duration
from shade_pipeline.rasterize import rasterize_lidar
from shade_pipeline.sources import LidarSource

ARTIFACT_VERSION = "v1"
_VERSIONED_PACKAGES = ("shade-pipeline", "shade-core", "laspy", "rasterio", "numpy")


def build_city(
    config: CityConfig,
    source: LidarSource,
    output_root: Path,
    params: HorizonParams | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Produce ``<output_root>/<city>/v1/`` artifacts; returns that directory.

    ``progress`` receives one line per LiDAR file binned and per horizon
    tile swept (with running average and ETA), plus a summary with the
    elapsed time as each phase closes -- city builds run for hours and
    silence reads as a hang.
    """
    if params is None:
        params = HorizonParams(
            sectors=config.horizon_sectors,
            max_distance_m=config.horizon_max_distance_m,
            observer_height_m=config.observer_height_m,
        )

    def say(message: str) -> None:
        if progress is not None:
            progress(message)

    build_start = time.monotonic()
    resolution = config.resolution_m
    pad = buffer_pixels(params.max_distance_m, resolution)
    padded = padded_bbox(config.bbox, resolution, pad)
    files = source.files_covering(config.bbox, pad * resolution)
    say(f"{len(files)} lidar files ready in {format_duration(time.monotonic() - build_start)}")

    phase_start = time.monotonic()
    stack = rasterize_lidar(files, padded, resolution, progress=progress)
    total_points = sum(stack.point_counts.values())
    say(
        f"binning done in {format_duration(time.monotonic() - phase_start)} "
        f"({total_points:,} points)"
    )

    rows, cols = grid_shape(config.bbox, resolution)
    inner = (pad, pad + rows, pad, pad + cols)
    out_dir = output_root / config.id / ARTIFACT_VERSION
    out_dir.mkdir(parents=True, exist_ok=True)
    crop = (slice(pad, pad + rows), slice(pad, pad + cols))
    transform = transform_from_bbox(config.bbox, resolution)
    common = {"city_id": config.id}

    # Scratch inside out_dir: same (gitignored) filesystem as the output, so
    # the memmapped cubes never land on a small tmpfs. float32 rasters go in
    # as-is -- the sweep casts per tile, a whole-array float64 copy buys
    # nothing but ~1.2 GB of peak RSS at city scale.
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".horizon-") as scratch:
        phase_start = time.monotonic()
        result = compute_horizon_tiled(
            stack.dsm,
            stack.dtm,
            stack.landcover,
            resolution,
            params,
            inner,
            scratch_dir=Path(scratch),
            progress=progress,
        )
        say(f"horizon sweep done in {format_duration(time.monotonic() - phase_start)}")
        say(f"writing {HORIZON_FILENAME}")
        write_cog(
            out_dir / HORIZON_FILENAME,
            result.angles_q,
            transform,
            config.crs,
            tags={
                **common,
                "angle_max_deg": str(ANGLE_MAX_DEG),
                "sectors": str(params.sectors),
                "max_distance_m": str(params.max_distance_m),
                "observer_height_m": str(params.observer_height_m),
            },
        )
        say(f"writing {BLOCKER_CLASS_FILENAME}")
        write_cog(
            out_dir / BLOCKER_CLASS_FILENAME,
            result.blocker_class,
            transform,
            config.crs,
            tags={**common, "no_blocker": str(NO_BLOCKER)},
        )
        del result
    say(f"writing {DSM_FILENAME}")
    write_cog(out_dir / DSM_FILENAME, stack.dsm[crop], transform, config.crs, tags=common)
    say(f"writing {DTM_FILENAME}")
    write_cog(out_dir / DTM_FILENAME, stack.dtm[crop], transform, config.crs, tags=common)
    say(f"writing {LANDCOVER_FILENAME}")
    write_cog(
        out_dir / LANDCOVER_FILENAME, stack.landcover[crop], transform, config.crs, tags=common
    )

    metadata = BuildMetadata(
        schema_version=1,
        city_id=config.id,
        artifact_version=ARTIFACT_VERSION,
        built_at=datetime.now(UTC),
        crs=config.crs,
        bbox=config.bbox,
        resolution_m=resolution,
        horizon=HorizonBuildParams(
            sectors=params.sectors,
            max_distance_m=params.max_distance_m,
            observer_height_m=params.observer_height_m,
            angle_max_deg=ANGLE_MAX_DEG,
            step_mode=params.step_mode,
            tile_size=params.tile_size,
        ),
        landcover_classes={member.name.lower(): int(member) for member in Landcover},
        no_blocker_value=NO_BLOCKER,
        software={name: importlib_metadata.version(name) for name in _VERSIONED_PACKAGES},
        inputs=[
            ArtifactInput(name=name, points=count) for name, count in stack.point_counts.items()
        ],
        attribution=config.attribution,
    )
    (out_dir / METADATA_FILENAME).write_text(metadata.model_dump_json(indent=2))
    say(f"build done in {format_duration(time.monotonic() - build_start)}")
    return out_dir
