"""Vectorizing the building mask: the staircase goes, the building stays.

The risk here is not a crash, it is a plausible wrong shape. A regularized
outline is always tidier than the mask it came from, so "it looks like a
building" proves nothing; every test below fixes a value that was known before
the code ran -- an angle that was chosen, an area that was drawn, a wall that
was placed.
"""

import json
import math

import numpy as np
import pytest
import rasterio.features
from affine import Affine
from shapely.geometry import Polygon

from shade_pipeline.outlines import (
    EDGE_BIAS_CELLS,
    building_outlines,
    dominant_angle,
    outlines_geojson,
    polygonize,
    regularize,
)

TRANSFORM = Affine(1.0, 0.0, 400000.0, 0.0, -1.0, 4100000.0)
"""One metre cells, north-up, anchored somewhere plausible in EPSG:25830."""


def _rotated_square(side: float, degrees: float, centre: tuple[float, float]) -> Polygon:
    """A square of ``side`` metres turned ``degrees`` anticlockwise."""
    half = side / 2.0
    corners = np.array([(-half, -half), (half, -half), (half, half), (-half, half)])
    angle = math.radians(degrees)
    rotation = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    return Polygon(corners @ rotation.T + np.array(centre))


def _mask(polygon: Polygon, shape: tuple[int, int] = (120, 120)) -> np.ndarray:
    burned = rasterio.features.rasterize(
        [(polygon, 1)], out_shape=shape, transform=TRANSFORM, fill=0, dtype="uint8"
    )
    return burned.astype(bool)


def test_a_wall_that_is_straight_comes_out_straight() -> None:
    """The whole point, on the shape whose answer is known: a rotated square.

    Rasterized at 1 m a square turned 30 degrees is a staircase of dozens of
    steps. Four corners is the only right answer, and anything that returns
    the staircase has done nothing.
    """
    square = _rotated_square(40.0, 30.0, (400060.0, 4099940.0))

    outlines = building_outlines(_mask(square), TRANSFORM, edge_bias_cells=0.0)

    assert outlines.regularized == 1
    (drawn,) = outlines.polygons
    assert len(drawn.exterior.coords) - 1 == 4
    assert drawn.area == pytest.approx(square.area, rel=0.05)


def test_the_orientation_is_the_buildings_and_not_the_grids() -> None:
    """Not one edge of a traced staircase points where the building does.

    Every step is exactly parallel to the raster, so any answer read off the
    edges themselves is the grid's answer. The one measured here comes from the
    box around the shape, which is why it survives the staircase.
    """
    for degrees in (12.0, 30.0, 57.0):
        square = _rotated_square(40.0, degrees, (400060.0, 4099940.0))
        (traced,) = polygonize(_mask(square), TRANSFORM)

        assert dominant_angle(traced) == pytest.approx(degrees % 90.0, abs=1.0)


def test_a_patio_is_not_a_roof() -> None:
    """Interior rings survive, and they have to: a lost patio is invented roof."""
    block = _rotated_square(40.0, 20.0, (400060.0, 4099940.0))
    patio = _rotated_square(14.0, 20.0, (400060.0, 4099940.0))
    with_patio = Polygon(block.exterior.coords, [patio.exterior.coords])

    outlines = building_outlines(_mask(with_patio), TRANSFORM, edge_bias_cells=0.0)

    (drawn,) = outlines.polygons
    assert len(drawn.interiors) == 1
    assert drawn.interiors[0].is_ring
    assert drawn.area == pytest.approx(with_patio.area, rel=0.10)


def test_the_outline_is_pulled_in_by_half_a_cell() -> None:
    """The mask claims a whole cell for one roof point, so it is given back.

    Measured against the point cloud: without this the drawn wall sits 0.59 m
    outside the outermost roof return, with it 0.09 m -- and returns are 0.28 m
    apart, so 0.09 m is the wall landing on the edge.
    """
    square = _rotated_square(40.0, 30.0, (400060.0, 4099940.0))
    mask = _mask(square)

    raw = building_outlines(mask, TRANSFORM, edge_bias_cells=0.0)
    pulled = building_outlines(mask, TRANSFORM)

    # Half a metre off every side of a 40 m square: 40^2 -> 39^2, about -4.9%.
    assert pulled.area_m2 < raw.area_m2
    assert pulled.area_m2 == pytest.approx(raw.area_m2 - 4.0 * 40.0 * EDGE_BIAS_CELLS, rel=0.15)


def test_a_shape_too_thin_to_have_walls_disappears_instead_of_inverting() -> None:
    """Eroding something narrower than the correction must not turn it inside out."""
    sliver = Polygon([(400010.0, 4099990.0), (400040.0, 4099990.0), (400040.0, 4099989.0)])

    outlines = building_outlines(_mask(sliver), TRANSFORM)

    assert all(polygon.area > 0.0 for polygon in outlines.polygons)


def test_specks_are_not_drawn_but_that_is_all_it_means() -> None:
    """The area threshold is a drawing decision, and the docstring says so.

    Worth a test anyway, because the number is what stops a city's outline file
    from being half single stray cells.
    """
    speck = _rotated_square(3.0, 0.0, (400020.0, 4099980.0))
    shed = _rotated_square(12.0, 0.0, (400060.0, 4099940.0))

    outlines = building_outlines(_mask(speck.union(shed)), TRANSFORM, min_area_m2=20.0)

    assert len(outlines.polygons) == 1
    assert outlines.polygons[0].centroid.distance(shed.centroid) < 2.0


def test_an_outline_that_cannot_be_regularized_is_still_drawn() -> None:
    """A staircase is a worse drawing than a wall and a better one than a hole."""
    blob = Polygon(
        [
            (
                400020.0 + 14.0 * math.cos(t),
                4099970.0 + 14.0 * math.sin(t) * (1.0 + 0.35 * math.sin(7 * t)),
            )
            for t in np.linspace(0.0, 2.0 * math.pi, 60, endpoint=False)
        ]
    )

    outlines = building_outlines(_mask(blob), TRANSFORM)

    assert len(outlines.polygons) >= 1
    assert outlines.regularized + outlines.fell_back == len(outlines.polygons)


def test_regularize_refuses_a_ring_it_cannot_close() -> None:
    """Two points are not a wall, and the caller has to be told rather than guess."""
    degenerate = Polygon([(0.0, 0.0), (1.0, 0.0), (0.5, 0.0)])

    assert regularize(degenerate, 0.0) is None


def test_the_geojson_is_lon_lat_and_rounded() -> None:
    """RFC 7946 order, which is the opposite of what a WFS hands back.

    Six decimals is about 0.1 m of longitude here -- far finer than a 1 m
    raster can justify, and the reason the file stays small enough to download
    whole.
    """
    square = _rotated_square(40.0, 30.0, (400060.0, 4099940.0))
    outlines = building_outlines(_mask(square), TRANSFORM)

    collection = json.loads(outlines_geojson(outlines.polygons, "EPSG:25830"))

    assert collection["type"] == "FeatureCollection"
    (feature,) = collection["features"]
    assert feature["geometry"]["type"] == "Polygon"
    lon, lat = feature["geometry"]["coordinates"][0][0]
    assert -10.0 < lon < 5.0 and 35.0 < lat < 45.0, "lon/lat, not lat/lon"
    assert lon == round(lon, 6)


def test_an_empty_mask_is_an_empty_collection() -> None:
    """A city with nothing built is an answer, not a crash."""
    outlines = building_outlines(np.zeros((40, 40), dtype=bool), TRANSFORM)

    assert outlines.polygons == ()
    assert json.loads(outlines_geojson(outlines.polygons, "EPSG:25830"))["features"] == []
