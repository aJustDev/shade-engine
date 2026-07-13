"""Read per-city raster artifacts back into the in-memory engine types.

The pipeline writes six COGs plus a metadata JSON per city version
(``data/cities/<id>/v1/``); this module is the reading side. Two paths:
:func:`load_scene` reads whole arrays (tests, small fixtures) and
:class:`SceneReader` serves point queries through windowed reads with a
bounded block cache (the API path).

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
import math
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Final

import numpy as np
import numpy.typing as npt
import rasterio
from pydantic import BaseModel
from rasterio.windows import Window

from shade_core.config import Bbox
from shade_core.horizon import HorizonGrid
from shade_core.shade import ShadeScene

DSM_FILENAME: Final = "dsm.tif"
DTM_FILENAME: Final = "dtm.tif"
LANDCOVER_FILENAME: Final = "landcover.tif"
CANOPY_FILENAME: Final = "canopy.tif"
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

    The canopy mask is its own artifact (``canopy.tif``): vegetation pixels
    tall enough to stand under, per the pipeline's height threshold and
    speckle sieve (params recorded as COG tags; see shade_pipeline.canopy).
    """
    directory = Path(artifact_dir)
    metadata = load_metadata(directory)
    horizon = load_horizon(directory / HORIZON_FILENAME)
    georef = (horizon.resolution_m, horizon.origin)

    landcover = _read_single_band(directory / LANDCOVER_FILENAME, georef).astype(np.uint8)
    dsm = _read_single_band(directory / DSM_FILENAME, georef).astype(np.float64)
    dtm = _read_single_band(directory / DTM_FILENAME, georef).astype(np.float64)
    sector_classes, _, _, _ = _read_north_up(directory / BLOCKER_CLASS_FILENAME, georef)
    canopy: npt.NDArray[np.bool_] = _read_single_band(_canopy_path(directory), georef) != 0
    return ShadeScene(
        horizon=horizon,
        landcover=landcover,
        canopy=canopy,
        dsm=dsm,
        dtm=dtm,
        sector_classes=sector_classes.astype(np.uint8),
        observer_height_m=metadata.horizon.observer_height_m,
    )


class SceneReader:
    """Windowed, cached access to one city's artifacts for point queries.

    :func:`load_scene` reads whole rasters -- right for tests, wrong for an
    API process holding a city (gigabytes decompressed). A COG can serve any
    window by reading only the internal 512-px tiles it touches, but a cold
    one-pixel read still decompresses a full tile per touched band (64 for
    the horizon cube), so caching *pixels* would pay that cost on nearly
    every query. The reader instead caches aligned ``block_size`` square
    blocks, each stored as a ready-to-query block-local :class:`ShadeScene`
    (dequantized horizon + sector classes + canopy; no dsm/dtm, so
    classification takes the one-lookup sector-class path). A warm query is
    a dict lookup, and a whole day's timeline (~288 queries at one point)
    hits a single block.

    The default block of 64 divides the 512-px internal COG tile, so an
    aligned block never straddles two tiles. Memory stays bounded by the
    LRU: 64 sectors x 64 x 64 float32 (~1 MiB) plus classes and canopy is
    ~1.3 MiB per block, ~84 MiB per city at the default ``max_blocks=64``.

    :meth:`scene_for` returns the *pixel center* to query alongside the
    scene. The engine re-derives (row, col) from coordinates against the
    block-local origin, and near a block edge that float arithmetic can
    round differently than the global grid's -- a query at the exact
    boundary could index one pixel off, outside the block. Snapping to the
    center is semantically free (all spatial sampling is nearest-pixel, a
    phase-1 decision) and makes the local rowcol unambiguous.

    Reads happen under a lock: rasterio dataset handles are not safe for
    concurrent use and API endpoints run in a threadpool. A cold block costs
    one tile decompression per band group; warm blocks never touch rasterio.
    """

    def __init__(
        self, artifact_dir: str | Path, *, block_size: int = 64, max_blocks: int = 64
    ) -> None:
        directory = Path(artifact_dir)
        self.metadata = load_metadata(directory)
        self._horizon = rasterio.open(directory / HORIZON_FILENAME)
        self._blocker = rasterio.open(directory / BLOCKER_CLASS_FILENAME)
        self._canopy = rasterio.open(_canopy_path(directory))
        georef = _georef_of(self._horizon, directory / HORIZON_FILENAME)
        for src, name in (
            (self._blocker, BLOCKER_CLASS_FILENAME),
            (self._canopy, CANOPY_FILENAME),
        ):
            if _georef_of(src, directory / name) != georef:
                raise ValueError(
                    f"{directory / name}: georeference does not match the horizon's; "
                    "mixed artifact versions?"
                )
        sectors = self.metadata.horizon.sectors
        if self._horizon.count != sectors or self._blocker.count != sectors:
            raise ValueError(f"{directory}: band counts do not match {sectors} sectors")
        self._resolution_m, self._origin = georef
        self._rows = int(self._horizon.height)
        self._cols = int(self._horizon.width)
        self._angle_max_deg = float(self._horizon.tags()["angle_max_deg"])
        self._block_size = block_size
        self._max_blocks = max_blocks
        self._blocks: OrderedDict[tuple[int, int], ShadeScene] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def cached_blocks(self) -> int:
        return len(self._blocks)

    def contains(self, x: float, y: float) -> bool:
        """True when (x, y) falls on the artifact grid (half-open extent).

        Matches the floor arithmetic of :meth:`scene_for`: a point exactly on
        the max-x or min-y edge maps past the last pixel and is outside. Also
        rejects the non-finite coordinates a CRS transform yields for points
        outside its domain (every comparison with nan/inf is False).
        """
        x_min, y_max = self._origin
        return (
            x_min <= x < x_min + self._cols * self._resolution_m
            and y_max - self._rows * self._resolution_m < y <= y_max
        )

    def scene_for(self, x: float, y: float) -> tuple[ShadeScene, float, float]:
        """Block-local scene plus the pixel-center point to query it with.

        Raises ValueError when (x, y) falls outside the artifact grid.
        """
        col = math.floor((x - self._origin[0]) / self._resolution_m)
        row = math.floor((self._origin[1] - y) / self._resolution_m)
        if not (0 <= row < self._rows and 0 <= col < self._cols):
            raise ValueError(f"point ({x}, {y}) is outside the artifact grid")
        key = (row // self._block_size, col // self._block_size)
        with self._lock:
            scene = self._blocks.get(key)
            if scene is None:
                scene = self._load_block(*key)
                self._blocks[key] = scene
                if len(self._blocks) > self._max_blocks:
                    self._blocks.popitem(last=False)
            else:
                self._blocks.move_to_end(key)
        center_x = self._origin[0] + (col + 0.5) * self._resolution_m
        center_y = self._origin[1] - (row + 0.5) * self._resolution_m
        return scene, center_x, center_y

    def _load_block(self, block_row: int, block_col: int) -> ShadeScene:
        """Read one aligned block from the three COGs; caller holds the lock."""
        row0 = block_row * self._block_size
        col0 = block_col * self._block_size
        window = Window(
            col0,
            row0,
            min(self._block_size, self._cols - col0),
            min(self._block_size, self._rows - row0),
        )
        quantized = self._horizon.read(window=window)
        angles = quantized.astype(np.float32) * np.float32(self._angle_max_deg / 255.0)
        grid = HorizonGrid(
            angles_deg=angles,
            resolution_m=self._resolution_m,
            origin=(
                self._origin[0] + col0 * self._resolution_m,
                self._origin[1] - row0 * self._resolution_m,
            ),
        )
        sector_classes = self._blocker.read(window=window).astype(np.uint8)
        # read()[0] instead of read(1): rasterio's single-band path reshapes in
        # place, which numpy 2.5 deprecates.
        canopy: npt.NDArray[np.bool_] = self._canopy.read(window=window)[0] != 0
        return ShadeScene(
            horizon=grid,
            canopy=canopy,
            sector_classes=sector_classes,
            observer_height_m=self.metadata.horizon.observer_height_m,
        )

    def close(self) -> None:
        self._horizon.close()
        self._blocker.close()
        self._canopy.close()

    def __enter__(self) -> SceneReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _canopy_path(directory: Path) -> Path:
    """The canopy artifact, pointing at the backfill command when absent."""
    path = directory / CANOPY_FILENAME
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing; artifacts predate the canopy mask -- "
            "run `shade-engine canopy <city>` to derive it"
        )
    return path


def _read_single_band(
    path: Path, expected_georef: tuple[float, tuple[float, float]] | None = None
) -> npt.NDArray[np.generic]:
    data, _, _, _ = _read_north_up(path, expected_georef)
    if data.shape[0] != 1:
        raise ValueError(f"{path}: expected a single band, found {data.shape[0]}")
    band: npt.NDArray[np.generic] = data[0]
    return band


def _georef_of(src: rasterio.DatasetReader, path: str | Path) -> tuple[float, tuple[float, float]]:
    """(resolution_m, origin) of an open dataset, enforcing the north-up contract."""
    transform = src.transform
    if transform.b != 0.0 or transform.d != 0.0 or transform.e != -transform.a:
        raise ValueError(f"{path}: expected a north-up transform with square pixels")
    return float(transform.a), (float(transform.c), float(transform.f))


def _read_north_up(
    path: str | Path, expected_georef: tuple[float, tuple[float, float]] | None = None
) -> tuple[npt.NDArray[np.generic], float, tuple[float, float], dict[str, str]]:
    """Read a whole raster, enforcing the north-up square-pixel contract."""
    with rasterio.open(path) as src:
        resolution_m, origin = _georef_of(src, path)
        if expected_georef is not None and expected_georef != (resolution_m, origin):
            raise ValueError(
                f"{path}: georeference {(resolution_m, origin)} does not match "
                f"the horizon's {expected_georef}; mixed artifact versions?"
            )
        data: npt.NDArray[np.generic] = src.read()
        tags: dict[str, str] = src.tags()
        return data, resolution_m, origin, tags
