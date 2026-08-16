"""The shade boundary is thresholded on the tile grid, not on the 1 m lattice.

What the change claims is narrow: the same verdict, placed better. These pin
both halves -- that the arithmetic is unchanged where it is unambiguous, and
that the boundary really does leave the metre lattice.
"""

import io
import itertools
import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import mercantile
import numpy as np
import pytest
import rasterio
from PIL import Image
from pmtiles.reader import MmapSource, Reader
from pyproj import Transformer
from scipy import ndimage

from conftest import CUBE_CITY
from shade_core import artifacts
from shade_core.shade import Landcover
from shade_core.solar import SunPosition, sun_position
from shade_pipeline.shade_raster import (
    compose_state,
    compute_state_raster,
    read_shade_fields,
    signed_distance,
)
from shade_pipeline.tiles import MANIFEST_FILENAME, bounds_wgs84, build_tiles

WINTER_NOON = datetime(2026, 12, 21, 13, 20, tzinfo=ZoneInfo("Europe/Madrid"))


@pytest.fixture
def rendered(built_city: Path, tmp_path: Path) -> Path:
    """A private copy of the artifacts to render into.

    ``build_tiles`` writes ``tiles/`` *inside* the artifact directory, and
    ``built_city`` is session-scoped: rendering straight into it would leave a
    manifest behind for every later test that copies the fixture.
    """
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    return target


def _sun() -> SunPosition:
    west, south, east, north = bounds_wgs84(CUBE_CITY.crs, CUBE_CITY.bbox)
    return sun_position((south + north) / 2, (west + east) / 2, WINTER_NOON)


def test_fields_compose_to_the_same_state_at_one_metre(built_city: Path) -> None:
    """The parity that matters: same inputs, same answer, wherever it is decided.

    ``compute_state_raster`` still serves the routing graph at 1 m, so the two
    paths have to agree exactly when the tile path is evaluated on the
    artifacts' own grid. Any difference at this resolution would be a bug, not
    a sub-pixel refinement.
    """
    sun = _sun()
    fields = read_shade_fields(built_city, sun)
    composed = compose_state(
        shaded=fields.margin > 0.0,
        holds=fields.margin_noveg > 0.0,
        blocker=fields.blocker,
        under_canopy=fields.canopy_distance > 0.0,
    )
    assert np.array_equal(composed, compute_state_raster(built_city, sun))


def test_signed_distance_crosses_zero_on_the_pixel_boundary() -> None:
    mask = np.zeros((7, 7), dtype=bool)
    mask[2:5, 2:5] = True
    distance = signed_distance(mask)
    assert distance[3, 3] > 0  # interior
    assert distance[3, 2] == pytest.approx(1.0)  # last pixel inside
    assert distance[3, 1] == pytest.approx(-1.0)  # first pixel outside
    # The crossing sits halfway between those two centres: the pixel edge.
    assert distance[3, 2] + distance[3, 1] == pytest.approx(0.0)


def test_signed_distance_rounds_a_corner_but_keeps_the_area() -> None:
    """Thresholding an interpolated distance cuts corners; it does not erode."""
    mask = np.zeros((9, 9), dtype=bool)
    mask[2:7, 2:7] = True
    distance = signed_distance(mask)
    fine = ndimage.zoom(distance, 4, order=1, grid_mode=True, mode="nearest") > 0
    blocky = np.repeat(np.repeat(mask, 4, axis=0), 4, axis=1)
    assert blocky[8, 8] and not fine[8, 8]  # the outermost corner sub-pixel goes
    assert fine[16, 8] and fine[16, 16]  # mid-side and interior stay
    assert fine.sum() > 0.95 * blocky.sum()


def _tile_states(tiles_dir: Path, url: str, lon: float, lat: float, zoom: int) -> np.ndarray:
    """Palette indexes of the tile covering (lon, lat), as written."""
    tile = mercantile.tile(lon, lat, zoom)
    with open(tiles_dir / url, "rb") as handle:
        data = Reader(MmapSource(handle)).get(zoom, tile.x, tile.y)
    assert data is not None, "expected a tile at this zoom"
    return np.array(Image.open(io.BytesIO(data)))


def test_the_deep_zoom_boundary_leaves_the_metre_lattice(rendered: Path) -> None:
    """A z19 tile is not a magnified z17 one: its edge carries sub-pixel steps.

    The staircase of a nearest-upsampled raster repeats each source row, so
    every boundary run has a length that is a multiple of the zoom factor. A
    boundary thresholded on the tile grid does not, and that is the whole
    visible difference.
    """
    tiles_dir = build_tiles(CUBE_CITY, rendered, [WINTER_NOON], min_zoom=17, max_zoom=19)
    manifest = json.loads((tiles_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    url = str(manifest["instants"][0]["urls"]["building"]).split("?")[0]
    lon, lat = manifest["center_wgs84"]

    states = _tile_states(tiles_dir, url, lon, lat, 19)
    shade = states != 0  # palette index 0 is STATE_SUN
    assert shade.any(), "the fixture should cast building shade at this instant"

    # Column at which shade starts, per row, over the rows that have any.
    rows = [np.argmax(row) for row in shade if row.any()]
    steps = {int(a) - int(b) for a, b in itertools.pairwise(rows)}
    assert steps - {0}, "a boundary with no steps at all would mean an axis-aligned edge"
    # Nearest upsampling from 1 m to z19 (~0.24 m/px) can only shift the start
    # of a run by whole source pixels, so every nonzero step would be >= 4.
    assert any(0 < abs(step) < 4 for step in steps)


def test_a_roof_still_punches_a_hole_at_every_zoom(rendered: Path) -> None:
    """The roof clip survives the move to per-tile composition."""
    tiles_dir = build_tiles(CUBE_CITY, rendered, [WINTER_NOON], min_zoom=17, max_zoom=19)
    manifest = json.loads((tiles_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    url = str(manifest["instants"][0]["urls"]["building"]).split("?")[0]

    with rasterio.open(rendered / artifacts.LANDCOVER_FILENAME) as src:
        roof = src.read()[0] == Landcover.BUILDING
        transform = src.transform
    inner = ndimage.binary_erosion(roof, np.ones((5, 5), bool))
    row, col = (int(a[0]) for a in np.nonzero(inner))
    x, y = transform * (col + 0.5, row + 0.5)
    lon, lat = Transformer.from_crs(CUBE_CITY.crs, "EPSG:4326", always_xy=True).transform(x, y)

    for zoom in (17, 19):
        states = _tile_states(tiles_dir, url, lon, lat, zoom)
        tile = mercantile.tile(lon, lat, zoom)
        bounds = mercantile.xy_bounds(tile)
        mx, my = Transformer.from_crs(CUBE_CITY.crs, "EPSG:3857", always_xy=True).transform(x, y)
        px = int((mx - bounds.left) / (bounds.right - bounds.left) * states.shape[1])
        py = int((bounds.top - my) / (bounds.top - bounds.bottom) * states.shape[0])
        # Palette index 4 is STATE_OUTSIDE, which is what a roof becomes.
        assert states[py, px] == 4, f"roof interior should be clipped at z{zoom}"
