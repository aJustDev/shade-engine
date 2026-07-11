"""End to end: synthetic LAZ -> build -> COG artifacts -> golden queries."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from numpy.testing import assert_allclose
from typer.testing import CliRunner

import laz_fixture
import synthetic
from shade_core import artifacts
from shade_core.horizon import compute_horizon_reference
from shade_core.shade import ShadeState, ShadeType, is_shaded
from shade_core.solar import sun_position
from shade_pipeline.cli import app

CORDOBA_LAT, CORDOBA_LON = 37.88, -4.78
NEAR = (synthetic.QUERY_X, synthetic.CUBE_NORTH_WALL_Y + 10.0)
WINTER_NOON = datetime(2026, 12, 21, 13, 20, tzinfo=ZoneInfo("Europe/Madrid"))
SUMMER_NOON = datetime(2026, 6, 21, 14, 20, tzinfo=ZoneInfo("Europe/Madrid"))

ARTIFACT_FILES = (
    artifacts.DSM_FILENAME,
    artifacts.DTM_FILENAME,
    artifacts.LANDCOVER_FILENAME,
    artifacts.HORIZON_FILENAME,
    artifacts.BLOCKER_CLASS_FILENAME,
    artifacts.METADATA_FILENAME,
)

CUBE_CITY_YAML = """\
id: cube
name: Cube
country: ES
timezone: Europe/Madrid
crs: EPSG:25830
bbox: [20, 20, 100, 100]
resolution_m: 1.0
horizon_sectors: 64
horizon_max_distance_m: 20
"""


def test_build_writes_all_artifacts(built_city: Path) -> None:
    for name in ARTIFACT_FILES:
        assert (built_city / name).exists(), name
    metadata = artifacts.load_metadata(built_city)
    assert metadata.city_id == "cube"
    assert metadata.inputs[0].points == synthetic.SIZE * synthetic.SIZE
    assert all(metadata.software.values())


def test_loaded_horizon_matches_reference_crop(built_city: Path) -> None:
    """LAZ -> rasters -> sweep -> COG -> loader stays within quantization error."""
    grid = artifacts.load_horizon(built_city / artifacts.HORIZON_FILENAME)
    dsm, dtm = synthetic.cube_scene()
    reference = compute_horizon_reference(dsm, dtm, 1.0, max_distance_m=20.0)
    assert grid.origin == (20.0, 100.0)
    assert_allclose(
        grid.angles_deg,
        reference.angles_deg[:, 20:100, 20:100],
        atol=90.0 / 255.0 / 2.0 + 1e-4,
    )


def test_golden_queries_on_built_city(built_city: Path) -> None:
    """The spec's golden verdicts, answered from disk artifacts alone."""
    scene = artifacts.load_scene(built_city)
    winter = is_shaded(scene, *NEAR, sun_position(CORDOBA_LAT, CORDOBA_LON, WINTER_NOON))
    assert winter.state is ShadeState.SHADE
    assert winter.shade_type is ShadeType.BUILDING
    summer = is_shaded(scene, *NEAR, sun_position(CORDOBA_LAT, CORDOBA_LON, SUMMER_NOON))
    assert summer.state is ShadeState.SUN


def test_cli_smoke(tmp_path: Path) -> None:
    cities_dir = tmp_path / "cities"
    cities_dir.mkdir()
    (cities_dir / "cube.yaml").write_text(CUBE_CITY_YAML)
    lidar_dir = tmp_path / "lidar"
    lidar_dir.mkdir()
    laz_fixture.write_cube_laz(lidar_dir / "cube.laz")
    output_root = tmp_path / "data"

    result = CliRunner().invoke(
        app,
        [
            "build",
            "cube",
            "--cities-dir",
            str(cities_dir),
            "--lidar-dir",
            str(lidar_dir),
            "--output-root",
            str(output_root),
        ],
    )
    assert result.exit_code == 0, result.output
    for name in ARTIFACT_FILES:
        assert (output_root / "cube" / "v1" / name).exists(), name


def test_cli_requires_lidar_dir(tmp_path: Path) -> None:
    cities_dir = tmp_path / "cities"
    cities_dir.mkdir()
    (cities_dir / "cube.yaml").write_text(CUBE_CITY_YAML)
    result = CliRunner().invoke(app, ["build", "cube", "--cities-dir", str(cities_dir)])
    assert result.exit_code == 1
