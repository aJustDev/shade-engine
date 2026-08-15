"""The area planner: reading a drawn polygon and pricing it before a build."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shade_pipeline.area import (
    AreaError,
    area_geojson,
    bbox_literal,
    lidar_needs,
    read_area,
    rewrite_config,
    snap_bbox,
    sweep_seconds,
    tile_saving,
)
from shade_pipeline.cli import app

CRS = "EPSG:25830"
CITY_YAML = """\
id: teso
name: Teso
country: ES
timezone: Europe/Madrid
crs: EPSG:25830
bbox: [354000, 4160000, 356000, 4161000] # provisional, a ojo
resolution_m: 1.0
horizon_sectors: 64
horizon_max_distance_m: 100
"""
# A square kilometre of Montilla, in the city's own CRS: 1000 x 1000 m starting
# on a round coordinate, so every pixel count below is countable by hand.
SQUARE = ((354000, 4160000), (355000, 4160000), (355000, 4161000), (354000, 4161000))


def _write_geojson(path: Path, *geometries: dict[str, object]) -> Path:
    features = [{"type": "Feature", "properties": {}, "geometry": geom} for geom in geometries]
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8"
    )
    return path


def _polygon(corners: tuple[tuple[float, float], ...]) -> dict[str, object]:
    return {"type": "Polygon", "coordinates": [[list(point) for point in (*corners, corners[0])]]}


def test_reads_a_projected_area(tmp_path: Path) -> None:
    """--geojson-crs lets a file already in the city CRS through untouched."""
    source = _write_geojson(tmp_path / "area.geojson", _polygon(SQUARE))
    area = read_area(source, CRS, source_crs=CRS)
    assert area.features == 1
    assert area.area_km2 == pytest.approx(1.0)
    # ...and the WGS84 twin really is degrees, whatever the input was.
    lon, lat = area.wgs84.centroid.x, area.wgs84.centroid.y
    assert -5.0 < lon < -4.0 and 37.0 < lat < 38.0


def test_merges_several_features(tmp_path: Path) -> None:
    """Drawing tools emit one polygon per click; the area is their union."""
    east = tuple((x + 1000, y) for x, y in SQUARE)
    source = _write_geojson(tmp_path / "area.geojson", _polygon(SQUARE), _polygon(east))
    area = read_area(source, CRS, source_crs=CRS)
    assert area.features == 2
    assert area.area_km2 == pytest.approx(2.0)


def test_projected_coordinates_are_refused_as_wgs84(tmp_path: Path) -> None:
    """Meters read as degrees land off the planet; that has to fail at the door."""
    source = _write_geojson(tmp_path / "area.geojson", _polygon(SQUARE))
    with pytest.raises(AreaError, match="--geojson-crs"):
        read_area(source, CRS)


def test_a_line_is_not_an_area(tmp_path: Path) -> None:
    source = _write_geojson(
        tmp_path / "area.geojson",
        {"type": "LineString", "coordinates": [[-4.6, 37.5], [-4.5, 37.6]]},
    )
    with pytest.raises(AreaError, match="no area"):
        read_area(source, CRS)


def test_a_self_intersecting_ring_is_repaired(tmp_path: Path) -> None:
    """A hand-drawn bowtie still has an intent; make_valid keeps it."""
    bowtie = ((354000, 4160000), (355000, 4161000), (355000, 4160000), (354000, 4161000))
    source = _write_geojson(tmp_path / "area.geojson", _polygon(bowtie))
    area = read_area(source, CRS, source_crs=CRS)
    assert area.repaired
    assert area.area_km2 > 0


def test_not_geojson_at_all(tmp_path: Path) -> None:
    source = tmp_path / "area.geojson"
    source.write_text('{"hello": "world"}', encoding="utf-8")
    with pytest.raises(AreaError, match="not GeoJSON"):
        read_area(source, CRS)


def test_snap_bbox_grows_outward_to_the_lattice() -> None:
    """Never inward: a snapped bbox always still contains what was drawn."""
    assert snap_bbox((100.4, 200.6, 300.1, 400.9), 1.0) == (100.0, 200.0, 301.0, 401.0)
    # Already on the lattice: nothing moves.
    assert snap_bbox((100.0, 200.0, 300.0, 400.0), 1.0) == (100.0, 200.0, 300.0, 400.0)
    # And the lattice follows the resolution, not the pixel count.
    assert snap_bbox((101.0, 201.0, 309.0, 409.0), 5.0) == (100.0, 200.0, 310.0, 410.0)


def test_tile_saving_counts_tiles_by_hand(tmp_path: Path) -> None:
    """A square in the western half of a 2 x 1 km box: at 500 m tiles, half go."""
    inset = ((354010, 4160010), (354990, 4160010), (354990, 4160990), (354010, 4160990))
    source = _write_geojson(tmp_path / "area.geojson", _polygon(inset))
    area = read_area(source, CRS, source_crs=CRS)
    bbox = (354000.0, 4160000.0, 356000.0, 4161000.0)  # the square plus a km of nothing
    saving = tile_saving(area.projected, bbox, 1.0, 500)
    assert saving.total == 8  # 2 rows x 4 columns of 500 px
    assert saving.swept == 4  # only the western half is drawn
    assert saving.saved == pytest.approx(0.5)


def test_a_bigger_tile_saves_less(tmp_path: Path) -> None:
    """The saving is quantized by the tile: this is the whole point of the report."""
    strip = ((354000, 4160010), (356000, 4160010), (356000, 4160490), (354000, 4160490))
    source = _write_geojson(tmp_path / "area.geojson", _polygon(strip))
    area = read_area(source, CRS, source_crs=CRS)
    bbox = (354000.0, 4160000.0, 356000.0, 4161000.0)
    fine = tile_saving(area.projected, bbox, 1.0, 250)
    coarse = tile_saving(area.projected, bbox, 1.0, 1000)
    assert fine.saved == pytest.approx(0.5)
    assert coarse.saved == 0.0  # every 1000 px tile is touched by the strip


def test_a_tile_the_area_only_grazes_is_still_swept(tmp_path: Path) -> None:
    """Touching the boundary counts as touching, which is the safe direction.

    Coverage is rasterized with ``all_touched``, so a polygon edge lying
    exactly on a tile boundary does put pixels of that tile inside the area.
    Counting it as skipped would promise a saving the build cannot take.
    """
    flush = ((354000, 4160000), (355000, 4160000), (355000, 4161000), (354000, 4161000))
    source = _write_geojson(tmp_path / "area.geojson", _polygon(flush))
    area = read_area(source, CRS, source_crs=CRS)
    bbox = (354000.0, 4160000.0, 356000.0, 4161000.0)
    saving = tile_saving(area.projected, bbox, 1.0, 500)
    assert saving.swept == 6  # the four covered tiles, plus the two it grazes at x=355000


def test_sweep_estimate_matches_the_measured_build() -> None:
    """The reference build: 1489 x 860 px, 64 sectors, 500 m, serial, 15m 56s.

    Pinned because every minute this command promises comes off this constant;
    a silent drift here is a plan built on a number nobody measured.
    """
    measured = 15 * 60 + 56
    estimate = sweep_seconds(1489 * 860, 64, 500.0, 1.0, 1)
    assert estimate == pytest.approx(measured, rel=0.05)


def test_sweep_estimate_scales_with_the_work() -> None:
    """Twice the sectors is twice the passes; twice the radius, twice the samples."""
    base = sweep_seconds(1_000_000, 64, 500.0, 1.0, 1)
    assert sweep_seconds(1_000_000, 128, 500.0, 1.0, 1) == pytest.approx(2 * base)
    assert sweep_seconds(1_000_000, 64, 1000.0, 1.0, 1) == pytest.approx(2 * base)
    # And more workers never promise a linear speedup.
    assert sweep_seconds(1_000_000, 64, 500.0, 1.0, 7) > base / 7


def test_lidar_needs_sees_the_cache(tmp_path: Path) -> None:
    """Cached tiles are matched by their PNOA name, missing ones by its pattern."""
    (tmp_path / "PNOA-2024-AND-354-4161-H30-NPC01.laz").write_bytes(b"LASF")
    (tmp_path / "unrelated.txt").write_text("", encoding="utf-8")
    need = lidar_needs((354000.0, 4160000.0, 354500.0, 4160500.0), 0.0, 1, tmp_path)
    assert need.needed == 1
    assert need.cached == 1
    assert need.missing == ()

    farther = lidar_needs((354000.0, 4160000.0, 355500.0, 4160500.0), 0.0, 1, tmp_path)
    assert farther.needed == 2
    assert farther.cached == 1
    assert farther.missing == ("PNOA-*-355-4161-*.laz",)


def test_rewrite_config_keeps_every_comment() -> None:
    """A YAML dumper would eat the hand-written notes; this edits two lines."""
    original = (
        "id: montilla\n"
        "name: Montilla\n"
        "bbox: [353600, 4159400, 356600, 4161900] # casco urbano + margen\n"
        "resolution_m: 1.0 # un metro por pixel\n"
    )
    updated = rewrite_config(
        original, (353000.0, 4159000.0, 357000.0, 4162000.0), Path("a.geojson")
    )
    assert "bbox: [353000, 4159000, 357000, 4162000] # casco urbano + margen" in updated
    assert "area: a.geojson\n" in updated
    assert "resolution_m: 1.0 # un metro por pixel" in updated
    assert updated.startswith("id: montilla\nname: Montilla\n")


def test_rewrite_config_replaces_an_existing_area() -> None:
    original = "bbox: [1, 2, 3, 4]\narea: cities/old.geojson # el area de antes\nid: x\n"
    updated = rewrite_config(original, (1.0, 2.0, 3.0, 4.0), Path("cities/new.geojson"))
    assert "area: cities/new.geojson # el area de antes" in updated
    assert "old.geojson" not in updated
    assert updated.count("area:") == 1


def test_rewrite_config_refuses_an_ambiguous_file() -> None:
    with pytest.raises(AreaError, match="exactly one"):
        rewrite_config("id: x\n", (1.0, 2.0, 3.0, 4.0), Path("a.geojson"))


def test_bbox_literal_keeps_round_meters_round() -> None:
    """The city files write integers; a rewrite must not turn them into floats."""
    assert bbox_literal((353600.0, 4159400.0, 356600.0, 4161900.0)) == (
        "[353600, 4159400, 356600, 4161900]"
    )
    assert bbox_literal((0.5, 1.0, 2.0, 3.0)) == "[0.5, 1, 2, 3]"


def test_area_geojson_round_trips_as_wgs84(tmp_path: Path) -> None:
    """Whatever came in, what goes out is one feature in lon/lat, per RFC 7946."""
    source = _write_geojson(tmp_path / "in.geojson", _polygon(SQUARE))
    area = read_area(source, CRS, source_crs=CRS)
    written = tmp_path / "out.geojson"
    written.write_text(area_geojson(area, "montilla"), encoding="utf-8")

    reloaded = read_area(written, CRS)  # no --geojson-crs: it must be degrees now
    assert reloaded.features == 1
    assert reloaded.area_km2 == pytest.approx(area.area_km2, rel=1e-3)
    document = json.loads(written.read_text(encoding="utf-8"))
    assert document["features"][0]["properties"] == {"city": "montilla"}


def _city(tmp_path: Path) -> Path:
    cities_dir = tmp_path / "cities"
    cities_dir.mkdir()
    (cities_dir / "teso.yaml").write_text(CITY_YAML, encoding="utf-8")
    return cities_dir


def test_cli_reports_without_touching_anything(tmp_path: Path) -> None:
    cities_dir = _city(tmp_path)
    source = _write_geojson(tmp_path / "drawn.geojson", _polygon(SQUARE))

    result = CliRunner().invoke(
        app,
        ["area", "teso", str(source), "--cities-dir", str(cities_dir), "--geojson-crs", CRS],
    )
    assert result.exit_code == 0, result.output
    assert "1 feature(s), 1.00 km2 in EPSG:25830" in result.output
    assert "bbox: [354000, 4160000, 355000, 4161000]" in result.output
    # The count the build will write to metadata.json, not an approximation.
    assert "1,000,000 px inside the area, 100% of the box" in result.output
    assert "nothing written" in result.output
    # The report is a report: the config still says what it said.
    assert (cities_dir / "teso.yaml").read_text(encoding="utf-8") == CITY_YAML


def test_cli_write_applies_both_halves(tmp_path: Path) -> None:
    """--write leaves a city that ``build`` can read: YAML edited, area beside it."""
    cities_dir = _city(tmp_path)
    source = _write_geojson(tmp_path / "drawn.geojson", _polygon(SQUARE))

    result = CliRunner().invoke(
        app,
        [
            "area",
            "teso",
            str(source),
            "--cities-dir",
            str(cities_dir),
            "--geojson-crs",
            CRS,
            "--write",
        ],
    )
    assert result.exit_code == 0, result.output

    updated = (cities_dir / "teso.yaml").read_text(encoding="utf-8")
    assert "bbox: [354000, 4160000, 355000, 4161000] # provisional, a ojo" in updated
    assert f"area: {cities_dir / 'teso' / 'area.geojson'}" in updated
    assert "horizon_max_distance_m: 100" in updated

    written = cities_dir / "teso" / "area.geojson"
    assert written.exists()
    assert read_area(written, CRS).area_km2 == pytest.approx(1.0, rel=1e-3)


def test_cli_refuses_a_city_that_does_not_exist(tmp_path: Path) -> None:
    source = _write_geojson(tmp_path / "drawn.geojson", _polygon(SQUARE))
    result = CliRunner().invoke(
        app, ["area", "nowhere", str(source), "--cities-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "write the city YAML first" in result.output
