"""Choosing the CRS for a city, and refusing one the city does not sit in.

Both of these are regressions from the same afternoon. Registering Montalban de
Cordoba wrote ``EPSG:25831`` -- UTM zone 31N, which covers 0 to 6 degrees *east*
-- for a town at 4.75 degrees *west*, because a longitude was typed without its
minus sign. The bbox that came out was ``[-185284, 4186682, -184109, 4188706]``:
a negative easting, which UTM's 500 km false origin exists precisely to make
impossible.

Nothing downstream could catch it. The bbox was self-consistent, the rasters
would have been the right shape, and the only complaint arrived hours later from
the LiDAR downloader, about tiles named ``PNOA-*--186-4187-*.laz`` that of course
do not exist.
"""

import json
from pathlib import Path

import pytest

from shade_pipeline.area import AreaError, check_area_of_use, read_area, snap_bbox, utm_crs

MONTALBAN = [
    [-4.754379, 37.589995],
    [-4.742928, 37.589995],
    [-4.742928, 37.572506],
    [-4.754379, 37.572506],
    [-4.754379, 37.589995],
]


def _polygon(path: Path, ring: list[list[float]]) -> Path:
    path.write_text(
        json.dumps({"type": "Polygon", "coordinates": [ring]}),
        encoding="utf-8",
    )
    return path


def test_the_zone_is_the_one_the_neighbouring_cities_already_use() -> None:
    """Montalban is beside Montilla, so it belongs in the same zone Cordoba does."""
    assert utm_crs(37.59, -4.75)[0] == "EPSG:25830"
    assert utm_crs(37.58, -4.64)[0] == "EPSG:25830"  # Montilla
    assert utm_crs(37.88, -4.78)[0] == "EPSG:25830"  # Cordoba


def test_the_transposed_twin_is_never_chosen() -> None:
    """EPSG:3042 is the same zone with the axes swapped, and the database offers it first.

    Writing it would put a bbox of [x, y, x, y] into a CRS that reads it as
    [y, x, y, x], and the whole city would come out transposed in silence.
    """
    code, name = utm_crs(37.88, -4.78)

    assert code != "EPSG:3042"
    assert "(N-E)" not in name


def test_a_point_outside_the_european_datum_still_resolves() -> None:
    assert utm_crs(-33.45, -70.67)[0].startswith("EPSG:")  # Santiago de Chile


def test_a_polygon_outside_its_crs_is_refused_and_the_right_zone_named(
    tmp_path: Path,
) -> None:
    """The exact failure, pinned: a town at 4.75 W declared in a zone that starts at 0."""
    drawn = read_area(_polygon(tmp_path / "montalban.geojson", MONTALBAN), "EPSG:25831")

    with pytest.raises(AreaError) as caught:
        check_area_of_use(drawn, "EPSG:25831")

    message = str(caught.value)
    assert "outside what EPSG:25831 is defined for" in message
    assert "EPSG:25830" in message, "it has to name the zone that would work"


def test_the_wrong_zone_is_what_produces_an_impossible_easting(tmp_path: Path) -> None:
    """Why this matters: the number that comes out is not merely wrong, it cannot exist."""
    drawn = read_area(_polygon(tmp_path / "montalban.geojson", MONTALBAN), "EPSG:25831")

    min_x, _, _, _ = snap_bbox(drawn.projected.bounds, 1.0)

    assert min_x < 0, "UTM eastings carry a 500 km false origin so they never go negative"


def test_the_right_zone_lands_beside_montilla(tmp_path: Path) -> None:
    drawn = read_area(_polygon(tmp_path / "montalban.geojson", MONTALBAN), "EPSG:25830")

    check_area_of_use(drawn, "EPSG:25830")
    min_x, min_y, max_x, max_y = snap_bbox(drawn.projected.bounds, 1.0)

    # Montilla's own bbox is [352889, 4157095, 357142, 4163509]; a village next
    # door has to land in the same neighbourhood.
    assert 340_000 < min_x < 360_000
    assert 4_150_000 < min_y < 4_170_000
    assert max_x > min_x and max_y > min_y


def test_a_crs_with_no_declared_area_of_use_is_left_alone(tmp_path: Path) -> None:
    """The check reports what it knows and never invents a refusal."""
    drawn = read_area(_polygon(tmp_path / "montalban.geojson", MONTALBAN), "EPSG:4326")

    check_area_of_use(drawn, "EPSG:4326")  # world-wide: nothing to complain about


def test_an_unknown_crs_says_so(tmp_path: Path) -> None:
    drawn = read_area(_polygon(tmp_path / "montalban.geojson", MONTALBAN), "EPSG:25830")

    with pytest.raises(AreaError, match="not a CRS this system knows"):
        check_area_of_use(drawn, "EPSG:999999")
