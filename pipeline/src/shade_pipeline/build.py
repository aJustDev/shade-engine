"""Build orchestration: city config -> LiDAR -> rasters -> horizon -> COGs.

The pipeline rasterizes the *padded* bbox (city bbox plus the horizon
buffer) so every pixel of the city proper sees its obstacles, sweeps the
horizon for the inner window only, and crops all exports back to the city
bbox -- every artifact shares one shape and georeference, as the engine's
``ShadeScene`` requires.
"""

import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path

import numpy as np
import numpy.typing as npt

from shade_core.artifacts import (
    BLOCKER_CLASS_FILENAME,
    CANOPY_FILENAME,
    COVERAGE_FILENAME,
    DSM_FILENAME,
    DTM_FILENAME,
    HORIZON_FILENAME,
    HORIZON_NOVEG_FILENAME,
    LANDCOVER_FILENAME,
    METADATA_FILENAME,
    TREE_INVENTORY_FILENAME,
    ArtifactInput,
    BuildMetadata,
    CoverageBuildParams,
    DeclutterBuildParams,
    HorizonBuildParams,
    LandcoverBuildParams,
    TreeInventoryBuildParams,
)
from shade_core.config import CityConfig
from shade_core.shade import NO_BLOCKER, Landcover
from shade_pipeline.area import coverage_mask, read_area
from shade_pipeline.canopy import CANOPY_TAGS, canopy_mask
from shade_pipeline.cog import write_cog
from shade_pipeline.declutter import (
    LINEAR_MAX_BASE_M,
    LINEAR_OPENING_PX,
    LINEAR_PROTRUSION_M,
    SLAB_ROUGHNESS_MAX_M,
    declutter_dsm,
)
from shade_pipeline.events import EventSink, emit
from shade_pipeline.footprints import (
    ROOF_TOLERANCE_M,
    FootprintSource,
    apply_footprint_override,
    footprint_ids,
)
from shade_pipeline.grid import buffer_pixels, grid_shape, padded_bbox, transform_from_bbox
from shade_pipeline.horizon import ANGLE_MAX_DEG, HorizonParams, compute_horizon_tiled
from shade_pipeline.progress import format_bytes, format_duration
from shade_pipeline.rasterize import BUILDING_MARGIN_M, rasterize_lidar
from shade_pipeline.sources import LidarSource
from shade_pipeline.tiles import bounds_wgs84
from shade_pipeline.trees import (
    DENSE_RADIUS_M,
    NEAR_RADIUS_M,
    TreeInventorySource,
    corroborated_area,
    inventory_zones,
)
from shade_pipeline.verify import ensure_verified

ARTIFACT_VERSION = "v1"
_VERSIONED_PACKAGES = ("shade-pipeline", "shade-core", "laspy", "rasterio", "numpy")


def build_city(
    config: CityConfig,
    source: LidarSource,
    output_root: Path,
    params: HorizonParams | None = None,
    progress: Callable[[str], None] | None = None,
    footprints: FootprintSource | None = None,
    trees: TreeInventorySource | None = None,
    declutter: bool = True,
    events: EventSink | None = None,
) -> Path:
    """Produce ``<output_root>/<city>/v1/`` artifacts; returns that directory.

    ``progress`` receives one line per LiDAR file binned and per horizon
    tile swept (with running average and ETA), plus a summary with the
    elapsed time as each phase closes -- city builds run for hours and
    silence reads as a hang.

    ``footprints`` corrects the LiDAR landcover with OSM building outlines
    (see :mod:`shade_pipeline.footprints`). None skips it, which is the only
    way to build with no network.

    ``trees`` audits the finished canopy mask against the city's own tree
    inventory (see :mod:`shade_pipeline.trees`). None skips it; so does a city
    whose config declares no inventory.

    ``declutter`` takes cables and awnings out of the DSM before the sweep
    reads it (see :mod:`shade_pipeline.declutter`). False reproduces the raw
    LiDAR surface every build shipped before ADR-022.

    ``events`` receives the same phase boundaries as structured records (see
    :mod:`shade_pipeline.events`), for a supervisor or console that has to know
    where a multi-hour build is without parsing the sentences above.
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

    def phase(name: str, since: float, **payload: object) -> None:
        """Close a phase on the structured channel, with what it cost."""
        emit(
            events,
            "build",
            "phase",
            name=name,
            elapsed_s=round(time.monotonic() - since, 1),
            **payload,
        )

    build_start = time.monotonic()
    resolution = config.resolution_m
    pad = buffer_pixels(params.max_distance_m, resolution)
    padded = padded_bbox(config.bbox, resolution, pad)

    # First of all, before any hour is spent: a computation area that does not
    # parse, or does not touch the bbox, has to fail now.
    coverage: npt.NDArray[np.bool_] | None = None
    if config.area is not None:
        drawn = read_area(Path(config.area), config.crs)
        coverage = coverage_mask(drawn.projected, config.bbox, resolution)
        covered = int(coverage.sum())
        if covered == 0:
            raise ValueError(
                f"the area in {config.area} covers no pixel of {config.id}'s bbox; "
                "run shade-engine area to check the two agree"
            )
        say(
            f"computation area from {config.area}: {covered:,} of {coverage.size:,} px "
            f"({100.0 * covered / coverage.size:.0f}% of the bbox)"
        )

    files = source.files_covering(config.bbox, pad * resolution)
    say(
        f"{len(files)} lidar files ready in {format_duration(time.monotonic() - build_start)} "
        f"({format_bytes(sum(path.stat().st_size for path in files))})"
    )
    phase("lidar", build_start, files=len(files))

    # Before the hours of binning and sweeping: a dead Overpass has to fail now,
    # not three phases in. The polygons cover the padded bbox because obstacles
    # outside the city still cast into it.
    outlines = []
    if footprints is not None:
        phase_start = time.monotonic()
        outlines = footprints.fetch(bounds_wgs84(config.crs, padded), config.crs)
        say(
            f"{len(outlines)} osm building footprints in "
            f"{format_duration(time.monotonic() - phase_start)}"
        )
        phase("footprints", phase_start, outlines=len(outlines))

    # Same reasoning: a dead WFS has to fail here and not after the sweep. Only
    # the trees over the city grid are asked for, plus the margin the dense
    # zone reaches inward from just outside it.
    tree_positions = np.zeros((0, 2), dtype=np.float64)
    if trees is not None:
        phase_start = time.monotonic()
        min_x, min_y, max_x, max_y = config.bbox
        tree_positions = trees.fetch(
            (
                min_x - DENSE_RADIUS_M,
                min_y - DENSE_RADIUS_M,
                max_x + DENSE_RADIUS_M,
                max_y + DENSE_RADIUS_M,
            ),
            config.crs,
        )
        say(
            f"{len(tree_positions):,} tree inventory specimens in "
            f"{format_duration(time.monotonic() - phase_start)}"
        )
        phase("trees", phase_start, specimens=len(tree_positions))

    phase_start = time.monotonic()
    stack = rasterize_lidar(files, padded, resolution, progress=progress)
    total_points = sum(stack.point_counts.values())
    say(
        f"binning done in {format_duration(time.monotonic() - phase_start)} "
        f"({total_points:,} points)"
    )
    phase("binning", phase_start, points=total_points)

    relabelled = 0
    built: npt.NDArray[np.bool_] | None = None
    if footprints is not None:
        phase_start = time.monotonic()
        ids = footprint_ids(outlines, stack.transform, stack.landcover.shape)
        chm = stack.dsm - stack.dtm
        relabelled = apply_footprint_override(stack.landcover, chm, ids)
        # One byte per pixel instead of four, and it outlives the ids: the
        # declutter below refuses to touch anything somebody has drawn.
        built = ids > 0
        del ids, chm  # both are city-sized; the sweep wants the room
        say(
            f"footprints relabelled {relabelled:,} cells as building in "
            f"{format_duration(time.monotonic() - phase_start)}"
        )
        phase("footprint_override", phase_start, relabelled=relabelled)

    # Last thing before the sweep, so every obstacle the horizon sees has
    # already been through it. Deliberately after the footprint override: that
    # one reads roof heights, and a cable strung over a roof is not part of it.
    cleaned = None
    if declutter:
        phase_start = time.monotonic()
        cleaned = declutter_dsm(stack.dsm, stack.dtm, stack.landcover, built)
        say(
            f"declutter removed {cleaned.linear_px:,} linear px and {cleaned.slab_px:,} "
            f"slab px from the dsm in {format_duration(time.monotonic() - phase_start)}"
        )
        phase("declutter", phase_start, linear_px=cleaned.linear_px, slab_px=cleaned.slab_px)
    del built  # city-sized; nothing downstream of the declutter wants it

    rows, cols = grid_shape(config.bbox, resolution)
    inner = (pad, pad + rows, pad, pad + cols)
    out_dir = output_root / config.id / ARTIFACT_VERSION
    out_dir.mkdir(parents=True, exist_ok=True)
    crop = (slice(pad, pad + rows), slice(pad, pad + cols))
    transform = transform_from_bbox(config.bbox, resolution)
    common = {"city_id": config.id}

    def timed_cog(
        path: Path,
        data: npt.NDArray[np.float32] | npt.NDArray[np.uint8],
        tags: Mapping[str, str],
    ) -> None:
        say(f"writing {path.name}")
        write_start = time.monotonic()
        write_cog(path, data, transform, config.crs, tags=tags)
        say(
            f"{path.name} written ({format_bytes(path.stat().st_size)}, "
            f"{format_duration(time.monotonic() - write_start)})"
        )

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
            coverage=coverage,
            scratch_dir=Path(scratch),
            progress=progress,
            events=events,
        )
        say(f"horizon sweep done in {format_duration(time.monotonic() - phase_start)}")
        phase("sweep", phase_start)
        # Read before `del result` below: the manifest is written much later,
        # and a cube without its datum cannot be reproduced.
        height_datum_m = result.height_datum_m
        timed_cog(
            out_dir / HORIZON_FILENAME,
            result.angles_q,
            tags={
                **common,
                "angle_max_deg": str(ANGLE_MAX_DEG),
                "sectors": str(params.sectors),
                "max_distance_m": str(params.max_distance_m),
                "observer_height_m": str(params.observer_height_m),
            },
        )
        timed_cog(
            out_dir / BLOCKER_CLASS_FILENAME,
            result.blocker_class,
            tags={**common, "no_blocker": str(NO_BLOCKER)},
        )
        timed_cog(
            out_dir / HORIZON_NOVEG_FILENAME,
            result.angles_noveg_q,
            tags={
                **common,
                "angle_max_deg": str(ANGLE_MAX_DEG),
                "sectors": str(params.sectors),
                "max_distance_m": str(params.max_distance_m),
                "observer_height_m": str(params.observer_height_m),
                "surface": "vegetation lowered to terrain",
            },
        )
        del result
    timed_cog(out_dir / DSM_FILENAME, stack.dsm[crop], tags=common)
    timed_cog(out_dir / DTM_FILENAME, stack.dtm[crop], tags=common)
    timed_cog(out_dir / LANDCOVER_FILENAME, stack.landcover[crop], tags=common)
    canopy = canopy_mask(stack.dsm[crop], stack.dtm[crop], stack.landcover[crop])
    timed_cog(
        out_dir / CANOPY_FILENAME,
        canopy,
        tags={**common, **CANOPY_TAGS},
    )
    tree_params: TreeInventoryBuildParams | None = None
    if trees is not None and config.tree_inventory is not None:
        zones = inventory_zones(tree_positions, transform, (rows, cols))
        corroborated, judged = corroborated_area(canopy, zones)
        say(
            f"tree inventory: {corroborated:,} of {judged:,} px of crown on surveyed ground "
            f"reaches a catalogued tree ({(100.0 * corroborated / judged) if judged else 0.0:.0f}%)"
        )
        timed_cog(
            out_dir / TREE_INVENTORY_FILENAME,
            zones,
            tags={
                **common,
                "dense_radius_m": str(DENSE_RADIUS_M),
                "near_radius_m": str(NEAR_RADIUS_M),
                "specimens": str(len(tree_positions)),
            },
        )
        tree_params = TreeInventoryBuildParams(
            source=config.tree_inventory.wfs,
            layers=list(config.tree_inventory.layers),
            specimens=len(tree_positions),
            dense_radius_m=DENSE_RADIUS_M,
            near_radius_m=NEAR_RADIUS_M,
            corroborated_px=corroborated,
            judged_px=judged,
        )
    del canopy
    if coverage is not None:
        # Written even though the cubes already encode it by construction:
        # zeros in the horizon are indistinguishable from a genuinely open sky,
        # so this file is the only thing standing between "no data" and a
        # confident "it is sunny here".
        timed_cog(
            out_dir / COVERAGE_FILENAME,
            coverage.astype(np.uint8),
            tags={**common, "area_source": config.area or "", "covered_px": str(coverage.sum())},
        )

    metadata = BuildMetadata(
        schema_version=2,
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
            step_mode="exact",
            tile_size=params.tile_size,
            height_datum_m=height_datum_m,
        ),
        landcover_classes={member.name.lower(): int(member) for member in Landcover},
        landcover=LandcoverBuildParams(
            building_margin_m=BUILDING_MARGIN_M,
            footprints="osm" if footprints is not None else None,
            footprint_count=len(outlines),
            footprint_relabelled=relabelled,
            roof_tolerance_m=ROOF_TOLERANCE_M if footprints is not None else None,
        ),
        declutter=None
        if cleaned is None
        else DeclutterBuildParams(
            linear_px=cleaned.linear_px,
            slab_px=cleaned.slab_px,
            protrusion_m=LINEAR_PROTRUSION_M,
            opening_px=LINEAR_OPENING_PX,
            max_base_m=LINEAR_MAX_BASE_M,
            slab_roughness_max_m=SLAB_ROUGHNESS_MAX_M,
        ),
        coverage=None
        if coverage is None or config.area is None
        else CoverageBuildParams(
            source=config.area,
            covered_px=int(coverage.sum()),
            covered_fraction=float(coverage.sum()) / coverage.size,
        ),
        tree_inventory=tree_params,
        no_blocker_value=NO_BLOCKER,
        software={name: importlib_metadata.version(name) for name in _VERSIONED_PACKAGES},
        inputs=[
            ArtifactInput(name=name, points=count) for name, count in stack.point_counts.items()
        ],
        attribution=config.attribution,
    )
    (out_dir / METADATA_FILENAME).write_text(metadata.model_dump_json(indent=2))
    # Verify the finished directory as a whole (write_cog already verified
    # each file): the horizon-blocker invariant is the cross-cube check that
    # a silent storage failure cannot survive.
    say("verifying artifacts")
    verify_start = time.monotonic()
    ensure_verified(out_dir, progress=progress)
    phase("verify", verify_start)
    artifact_size = sum(path.stat().st_size for path in out_dir.iterdir() if path.is_file())
    say(
        f"build done in {format_duration(time.monotonic() - build_start)} "
        f"({format_bytes(artifact_size)} of artifacts)"
    )
    emit(
        events,
        "build",
        "finished",
        elapsed_s=round(time.monotonic() - build_start, 1),
        bytes=artifact_size,
        directory=str(out_dir),
    )
    return out_dir
