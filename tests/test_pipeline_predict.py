"""shade-engine predict: prediction sheets for field-validation points."""

import re
from datetime import date
from pathlib import Path

import yaml
from pyproj import Transformer
from typer.testing import CliRunner

import synthetic
from conftest import CUBE_CITY
from shade_pipeline.cli import app
from shade_pipeline.predict import FieldPoint, prediction_table, read_points

_TO_WGS84 = Transformer.from_crs("EPSG:25830", "EPSG:4326", always_xy=True)
NEAR_LON, NEAR_LAT = _TO_WGS84.transform(
    synthetic.UTM_ORIGIN[0] + synthetic.QUERY_X,
    synthetic.UTM_ORIGIN[1] + synthetic.CUBE_NORTH_WALL_Y + 10.0,
)
WINTER_DAY = date(2026, 12, 21)


def test_prediction_table_lists_intervals(built_city: Path) -> None:
    points = [FieldPoint("near", "North of the cube", NEAR_LAT, NEAR_LON)]
    table = prediction_table(CUBE_CITY, built_city, points, WINTER_DAY)
    assert re.search(r"\| near \| \d{2}:\d{2}-\d{2}:\d{2} \| sun \| - \|", table)
    assert re.search(r"\| near \| \d{2}:\d{2}-\d{2}:\d{2} \| shade \| building \|", table)


def test_prediction_table_marks_points_outside_coverage(built_city: Path) -> None:
    points = [FieldPoint("madrid", "Puerta del Sol", 40.4169, -3.7035)]
    table = prediction_table(CUBE_CITY, built_city, points, WINTER_DAY)
    assert "| madrid | - | fuera de cobertura | - |" in table


def _write_kit(tmp_path: Path) -> tuple[Path, Path]:
    cities_dir = tmp_path / "cities"
    cities_dir.mkdir()
    (cities_dir / "cube.yaml").write_text(yaml.safe_dump(CUBE_CITY.model_dump(mode="json")))
    points_csv = tmp_path / "points.csv"
    points_csv.write_text(f"id,name,lat,lon\nnear,North of the cube,{NEAR_LAT},{NEAR_LON}\n")
    return cities_dir, points_csv


def test_cli_predict(built_city: Path, tmp_path: Path) -> None:
    cities_dir, points_csv = _write_kit(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "predict",
            "cube",
            str(points_csv),
            "--day",
            WINTER_DAY.isoformat(),
            "--cities-dir",
            str(cities_dir),
            "--output-root",
            str(built_city.parent.parent),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "| near |" in result.output
    assert "shade" in result.output


def test_cli_predict_requires_build(tmp_path: Path) -> None:
    cities_dir, points_csv = _write_kit(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "predict",
            "cube",
            str(points_csv),
            "--day",
            WINTER_DAY.isoformat(),
            "--cities-dir",
            str(cities_dir),
            "--output-root",
            str(tmp_path / "empty"),
        ],
    )
    assert result.exit_code == 1
    assert "build" in result.output


def test_read_points_roundtrip(tmp_path: Path) -> None:
    _, points_csv = _write_kit(tmp_path)
    points = read_points(points_csv)
    assert points == [FieldPoint("near", "North of the cube", NEAR_LAT, NEAR_LON)]


def test_field_kit_csv_parses() -> None:
    """The committed Cordoba kit stays loadable and inside the city bbox rough area."""
    points = read_points(Path(__file__).parent.parent / "docs" / "validacion-cordoba-puntos.csv")
    assert len(points) == 10
    for point in points:
        assert 37.8 < point.lat < 37.95
        assert -4.9 < point.lon < -4.7
