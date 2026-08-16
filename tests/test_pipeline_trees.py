"""Tree inventory: WFS fetch and cache, zone burning, corroboration counting."""

import json
from pathlib import Path

import numpy as np
import pytest
from affine import Affine
from numpy.testing import assert_array_equal

from shade_pipeline.trees import (
    DENSE_RADIUS_M,
    NEAR_RADIUS_M,
    ZONE_DENSE,
    ZONE_NEAR,
    ZONE_NONE,
    WfsTreeSource,
    corroborated_area,
    inventory_zones,
)

# North-up, 1 m pixels, origin at (0, 200): row r covers y in (200-r-1, 200-r].
TRANSFORM = Affine(1.0, 0.0, 0.0, 0.0, -1.0, 200.0)
SHAPE = (200, 200)


def _point_collection(coordinates: list[tuple[float, float]]) -> str:
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "Point", "coordinates": list(xy)},
                }
                for xy in coordinates
            ],
        }
    )


def test_zones_grade_outward_from_a_specimen() -> None:
    """One tree at a pixel centre: near, then dense, then nothing."""
    zones = inventory_zones(np.array([[100.5, 99.5]]), TRANSFORM, SHAPE)
    row, col = 100, 100
    assert zones[row, col] == ZONE_NEAR
    assert zones[row, col + int(NEAR_RADIUS_M)] == ZONE_NEAR
    assert zones[row, col + int(NEAR_RADIUS_M) + 2] == ZONE_DENSE
    assert zones[row, col + int(DENSE_RADIUS_M)] == ZONE_DENSE
    assert zones[row, col + int(DENSE_RADIUS_M) + 2] == ZONE_NONE
    assert zones[0, 0] == ZONE_NONE


def test_zones_are_circular_not_square() -> None:
    """The diagonal reaches sqrt(2) times less far than the axis, as a disk does."""
    zones = inventory_zones(np.array([[100.5, 99.5]]), TRANSFORM, SHAPE)
    reach = int(DENSE_RADIUS_M)
    assert zones[100, 100 + reach] == ZONE_DENSE
    assert zones[100 + reach, 100 + reach] == ZONE_NONE  # corner of the square


def test_banding_does_not_change_the_answer() -> None:
    """The halo makes narrow bands identical to one pass over the whole grid."""
    positions = np.array([[20.5, 180.5], [100.5, 99.5], [175.5, 30.5]])
    whole = inventory_zones(positions, TRANSFORM, SHAPE, band_rows=SHAPE[0])
    banded = inventory_zones(positions, TRANSFORM, SHAPE, band_rows=16)
    assert_array_equal(whole, banded)


def test_specimens_outside_the_grid_are_dropped_not_wrapped() -> None:
    """Negative indices would wrap to the far edge; they must not."""
    zones = inventory_zones(np.array([[-50.0, 99.5], [1000.0, 99.5]]), TRANSFORM, SHAPE)
    assert zones.max() == ZONE_NONE


def test_no_specimens_gives_an_empty_grid() -> None:
    zones = inventory_zones(np.zeros((0, 2)), TRANSFORM, SHAPE)
    assert zones.shape == SHAPE
    assert zones.dtype == np.uint8
    assert not zones.any()


def test_corroboration_is_measured_per_crown_not_per_pixel() -> None:
    """A trunk is a point and its crown is metres wide: the whole region counts.

    The pixel-wise reading of the same data answers 1 of 6; the region-wise
    one answers 6 of 6, and only the second is the question anybody is asking.
    """
    zones = np.zeros((3, 6), dtype=np.uint8)
    zones[:] = ZONE_DENSE
    zones[1, 1] = ZONE_NEAR  # one catalogued trunk under a 3 x 2 crown
    canopy = np.zeros((3, 6), dtype=np.uint8)
    canopy[:, :2] = 1

    assert corroborated_area(canopy, zones) == (6, 6)


def test_a_crown_with_no_tree_under_it_is_judged_and_fails() -> None:
    zones = np.full((3, 6), ZONE_DENSE, dtype=np.uint8)
    zones[1, 1] = ZONE_NEAR
    canopy = np.zeros((3, 6), dtype=np.uint8)
    canopy[:, :2] = 1  # corroborated
    canopy[:, 4:] = 1  # same size, nothing catalogued anywhere near

    assert corroborated_area(canopy, zones) == (6, 12)


def test_crowns_off_surveyed_ground_are_not_judged_at_all() -> None:
    """A courtyard tree with no municipal record is not a false positive."""
    zones = np.zeros((3, 6), dtype=np.uint8)  # all ZONE_NONE
    canopy = np.ones((3, 6), dtype=np.uint8)

    assert corroborated_area(canopy, zones) == (0, 0)


def test_wfs_source_reads_its_cache_without_network(tmp_path: Path) -> None:
    """A warm cache means no HTTP call at all, which is what makes builds repeatable."""
    source = WfsTreeSource(
        url="http://unreachable.invalid/wfs", layers=("t:Trees",), cache_dir=tmp_path
    )
    bbox = (0.0, 0.0, 100.0, 100.0)
    cached = tmp_path / "t-Trees-EPSG-25830-0-0-100-100.json"
    cached.write_text(_point_collection([(10.0, 20.0), (30.0, 40.0)]), encoding="utf-8")

    positions = source.fetch(bbox, "EPSG:25830")

    assert_array_equal(positions, np.array([[10.0, 20.0], [30.0, 40.0]]))


def test_wfs_source_concatenates_layers(tmp_path: Path) -> None:
    source = WfsTreeSource(
        url="http://unreachable.invalid/wfs", layers=("t:Trees", "t:Palms"), cache_dir=tmp_path
    )
    bbox = (0.0, 0.0, 100.0, 100.0)
    (tmp_path / "t-Trees-EPSG-25830-0-0-100-100.json").write_text(_point_collection([(1.0, 2.0)]))
    (tmp_path / "t-Palms-EPSG-25830-0-0-100-100.json").write_text(_point_collection([(3.0, 4.0)]))

    assert_array_equal(source.fetch(bbox, "EPSG:25830"), np.array([[1.0, 2.0], [3.0, 4.0]]))


def test_wfs_source_rejects_a_document_that_is_not_geojson(tmp_path: Path) -> None:
    source = WfsTreeSource(
        url="http://unreachable.invalid/wfs", layers=("t:Trees",), cache_dir=tmp_path
    )
    (tmp_path / "t-Trees-EPSG-25830-0-0-100-100.json").write_text('{"nope": 1}')
    with pytest.raises(ValueError, match="not a GeoJSON FeatureCollection"):
        source.fetch((0.0, 0.0, 100.0, 100.0), "EPSG:25830")
