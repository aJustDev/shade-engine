"""Read per-city raster artifacts back into the in-memory engine types.

The pipeline writes five COGs plus a metadata JSON per city version
(``data/cities/<id>/v1/``); this module is the reading side, used by tests
today and by the API tomorrow. Reads are whole-array for now -- windowed
reads over city-sized COGs arrive with the API phase.

Georeference contract (see docs/learning/cog.md and crs.md): artifacts are
north-up rasters with square pixels in the city's projected CRS, transform
``(res, 0, x_min, 0, -res, y_max)`` -- the same (x_min, y_max) origin and
southward rows as :class:`shade_core.horizon.HorizonGrid`. Band k+1 of the
horizon and blocker-class cubes is sector k (azimuth ``k * 360 / sectors``,
0 = North, clockwise). ``horizon.tif`` stores uint8-quantized angles; the
dequantization scale travels in its ``angle_max_deg`` tag so the file stays
self-describing.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import rasterio
from pydantic import BaseModel

from shade_core.config import Bbox
from shade_core.horizon import HorizonGrid
from shade_core.shade import Landcover, ShadeScene

DSM_FILENAME: Final = "dsm.tif"
DTM_FILENAME: Final = "dtm.tif"
LANDCOVER_FILENAME: Final = "landcover.tif"
HORIZON_FILENAME: Final = "horizon.tif"
BLOCKER_CLASS_FILENAME: Final = "blocker_class.tif"
METADATA_FILENAME: Final = "metadata.json"


class HorizonBuildParams(BaseModel):
    """Sweep parameters recorded at build time."""

    sectors: int
    max_distance_m: float
    observer_height_m: float
    angle_max_deg: float
    step_mode: str
    tile_size: int


class ArtifactInput(BaseModel):
    """One source file that fed the build."""

    name: str
    points: int


class BuildMetadata(BaseModel):
    """Contents of ``metadata.json``: everything needed to trust an artifact."""

    schema_version: int
    city_id: str
    artifact_version: str
    built_at: datetime
    crs: str
    bbox: Bbox
    resolution_m: float
    horizon: HorizonBuildParams
    landcover_classes: dict[str, int]
    no_blocker_value: int
    software: dict[str, str]
    inputs: list[ArtifactInput]
    attribution: list[str]


def load_metadata(artifact_dir: str | Path) -> BuildMetadata:
    raw = json.loads((Path(artifact_dir) / METADATA_FILENAME).read_text(encoding="utf-8"))
    return BuildMetadata.model_validate(raw)


def load_horizon(path: str | Path) -> HorizonGrid:
    """Dequantize ``horizon.tif`` into a float32 :class:`HorizonGrid`."""
    data, resolution_m, origin, tags = _read_north_up(path)
    angle_max_deg = float(tags["angle_max_deg"])
    angles = data.astype(np.float32) * np.float32(angle_max_deg / 255.0)
    return HorizonGrid(angles_deg=angles, resolution_m=resolution_m, origin=origin)


def load_scene(artifact_dir: str | Path) -> ShadeScene:
    """Everything the engine needs to answer queries about one city version.

    The canopy mask falls out of the landcover: a VEGETATION cell is one
    whose surface top is vegetation, i.e. a street-level observer there
    stands *under* the canopy.
    """
    directory = Path(artifact_dir)
    metadata = load_metadata(directory)
    horizon = load_horizon(directory / HORIZON_FILENAME)
    georef = (horizon.resolution_m, horizon.origin)

    landcover = _read_single_band(directory / LANDCOVER_FILENAME, georef).astype(np.uint8)
    dsm = _read_single_band(directory / DSM_FILENAME, georef).astype(np.float64)
    dtm = _read_single_band(directory / DTM_FILENAME, georef).astype(np.float64)
    sector_classes, _, _, _ = _read_north_up(directory / BLOCKER_CLASS_FILENAME, georef)
    canopy: npt.NDArray[np.bool_] = landcover == Landcover.VEGETATION
    return ShadeScene(
        horizon=horizon,
        landcover=landcover,
        canopy=canopy,
        dsm=dsm,
        dtm=dtm,
        sector_classes=sector_classes.astype(np.uint8),
        observer_height_m=metadata.horizon.observer_height_m,
    )


def _read_single_band(
    path: Path, expected_georef: tuple[float, tuple[float, float]] | None = None
) -> npt.NDArray[np.generic]:
    data, _, _, _ = _read_north_up(path, expected_georef)
    if data.shape[0] != 1:
        raise ValueError(f"{path}: expected a single band, found {data.shape[0]}")
    band: npt.NDArray[np.generic] = data[0]
    return band


def _read_north_up(
    path: str | Path, expected_georef: tuple[float, tuple[float, float]] | None = None
) -> tuple[npt.NDArray[np.generic], float, tuple[float, float], dict[str, str]]:
    """Read a whole raster, enforcing the north-up square-pixel contract."""
    with rasterio.open(path) as src:
        transform = src.transform
        if transform.b != 0.0 or transform.d != 0.0 or transform.e != -transform.a:
            raise ValueError(f"{path}: expected a north-up transform with square pixels")
        resolution_m = float(transform.a)
        origin = (float(transform.c), float(transform.f))
        if expected_georef is not None and expected_georef != (resolution_m, origin):
            raise ValueError(
                f"{path}: georeference {(resolution_m, origin)} does not match "
                f"the horizon's {expected_georef}; mixed artifact versions?"
            )
        data: npt.NDArray[np.generic] = src.read()
        tags: dict[str, str] = src.tags()
        return data, resolution_m, origin, tags
