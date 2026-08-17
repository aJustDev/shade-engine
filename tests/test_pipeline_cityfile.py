"""Editing a city YAML must not cost it its comments, and must never corrupt it.

Those comments are the only place some of the reasoning lives -- why the sweep
radius is 500, which PNOA series the tiles came from -- so a parse-and-dump
round trip is not an option. These pin the surgery and, more importantly, the
refusals.
"""

from pathlib import Path

import pytest

from shade_core.config import load_city
from shade_pipeline.cityfile import (
    CityFileError,
    edit_city,
    new_city_yaml,
    rewrite_scalar,
    write_new_city,
)

ANNOTATED = """\
id: cordoba
name: Cordoba
country: ES
timezone: Europe/Madrid
crs: EPSG:25830 # UTM 30N ETRS89
bbox: [338415, 4189828, 347432, 4200642] # en CRS local (metros)
area: cities/cordoba/area.geojson
resolution_m: 1.0
horizon_sectors: 64
horizon_max_distance_m: 500 # radio del barrido; tambien buffer del bbox
# Arbolado municipal, para auditar la mascara de copa. Ver ADR-021.
tree_inventory:
  wfs: https://ide.cordoba.es/geoserver/idecordoba/wfs
  layers: [idecordoba:Arbolado, idecordoba:palmeras]
sources:
  lidar: pnoa # driver de descarga (CnigSource)
  pnoa_series: LIDA3 # codSerie del centro de descargas
attribution:
  - Obra derivada de PNOA-cob3 2022-2025 CC-BY 4.0 scne.es
"""


@pytest.fixture
def city_file(tmp_path: Path) -> Path:
    path = tmp_path / "cordoba.yaml"
    path.write_text(ANNOTATED, encoding="utf-8")
    return path


def test_an_edit_keeps_the_trailing_comment(city_file: Path) -> None:
    edit_city(city_file, "horizon_max_distance_m", 800)

    text = city_file.read_text(encoding="utf-8")
    assert "horizon_max_distance_m: 800 # radio del barrido" in text


def test_an_edit_keeps_every_other_line_untouched(city_file: Path) -> None:
    before = ANNOTATED.split("\n")

    edit_city(city_file, "horizon_sectors", 128)

    after = city_file.read_text(encoding="utf-8").split("\n")
    changed = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert changed == [("horizon_sectors: 64", "horizon_sectors: 128")]


def test_the_result_still_loads(city_file: Path) -> None:
    config = edit_city(city_file, "resolution_m", 0.5)

    assert config.resolution_m == 0.5
    assert load_city(city_file).resolution_m == 0.5
    assert load_city(city_file).tree_inventory is not None, "the nested block survived"


def test_a_resolution_of_one_stays_written_as_a_float(city_file: Path) -> None:
    """The unit matters to whoever reads the file, even where YAML would not care."""
    edit_city(city_file, "resolution_m", 1.0)

    assert "resolution_m: 1.0" in city_file.read_text(encoding="utf-8")


def test_an_invalid_value_is_refused_and_the_file_is_untouched(city_file: Path) -> None:
    with pytest.raises(CityFileError, match="does not produce a valid city"):
        edit_city(city_file, "resolution_m", -3)

    assert city_file.read_text(encoding="utf-8") == ANNOTATED


def test_an_unknown_timezone_is_refused(city_file: Path) -> None:
    """CityConfig validates the zone; this proves the validation really runs."""
    with pytest.raises(CityFileError):
        edit_city(city_file, "timezone", "Europe/Cordoba")

    assert city_file.read_text(encoding="utf-8") == ANNOTATED


@pytest.mark.parametrize("key", ["bbox", "area", "id"])
def test_the_protected_keys_are_refused(city_file: Path, key: str) -> None:
    """bbox and area belong to `shade-engine area`; id names everything already built."""
    with pytest.raises(CityFileError, match="not editable here"):
        edit_city(city_file, key, "whatever")

    assert city_file.read_text(encoding="utf-8") == ANNOTATED


def test_a_missing_key_is_refused_rather_than_appended() -> None:
    with pytest.raises(CityFileError, match="found 0"):
        rewrite_scalar(ANNOTATED, "observer_height_m", 1.7)


def test_a_nested_key_is_not_mistaken_for_a_top_level_one() -> None:
    """`pnoa_series` is indented under `sources:`, so it is not a top-level scalar."""
    with pytest.raises(CityFileError, match="found 0"):
        rewrite_scalar(ANNOTATED, "pnoa_series", "LIDA2")


def test_a_generated_city_loads_and_carries_its_comments(tmp_path: Path) -> None:
    text = new_city_yaml(
        city_id="labana",
        name="La Bana",
        country="ES",
        timezone="Europe/Madrid",
        crs="EPSG:25830",
        crs_note="UTM 30N ETRS89",
        bbox=(340000.4, 4190000.2, 342000.9, 4192000.1),
        area="cities/labana/area.geojson",
    )

    path = write_new_city(tmp_path, text, "labana")
    config = load_city(path)

    assert config.id == "labana"
    assert config.crs == "EPSG:25830"
    assert config.bbox == (340000, 4190000, 342001, 4192000)
    assert "# en CRS local (metros), no lat/lon" in text, "the annotation is the point"
    assert "# IANA" in text


def test_a_generated_city_without_an_area_omits_the_line(tmp_path: Path) -> None:
    text = new_city_yaml(
        city_id="sinarea",
        name="Sin Area",
        country="ES",
        timezone="Europe/Madrid",
        crs="EPSG:25830",
        crs_note="UTM 30N",
        bbox=(340000, 4190000, 341000, 4191000),
        area=None,
    )

    assert "area:" not in text
    assert load_city(write_new_city(tmp_path, text, "sinarea")).area is None


def test_writing_over_an_existing_city_is_refused(tmp_path: Path) -> None:
    text = new_city_yaml(
        city_id="cube",
        name="Cube",
        country="ES",
        timezone="Europe/Madrid",
        crs="EPSG:25830",
        crs_note="UTM 30N",
        bbox=(340000, 4190000, 341000, 4191000),
        area=None,
    )
    write_new_city(tmp_path, text, "cube")

    with pytest.raises(CityFileError, match="already exists"):
        write_new_city(tmp_path, text, "cube")
