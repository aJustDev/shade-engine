"""End to end: synthetic LAZ -> build -> COG artifacts -> golden queries."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import rasterio
from numpy.testing import assert_allclose
from typer.testing import CliRunner

import laz_fixture
import synthetic
from shade_core import artifacts
from shade_core.horizon import compute_horizon_reference
from shade_core.shade import NO_BLOCKER, ShadeState, ShadeType, is_shaded
from shade_core.solar import sun_position
from shade_pipeline.cli import app

CORDOBA_LAT, CORDOBA_LON = 37.88, -4.78
NEAR = (
    synthetic.UTM_ORIGIN[0] + synthetic.QUERY_X,
    synthetic.UTM_ORIGIN[1] + synthetic.CUBE_NORTH_WALL_Y + 10.0,
)
WINTER_NOON = datetime(2026, 12, 21, 13, 20, tzinfo=ZoneInfo("Europe/Madrid"))
SUMMER_NOON = datetime(2026, 6, 21, 14, 20, tzinfo=ZoneInfo("Europe/Madrid"))

ARTIFACT_FILES = (
    artifacts.DSM_FILENAME,
    artifacts.DTM_FILENAME,
    artifacts.LANDCOVER_FILENAME,
    artifacts.CANOPY_FILENAME,
    artifacts.HORIZON_FILENAME,
    artifacts.HORIZON_NOVEG_FILENAME,
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
    # Written, not left at None: without its datum a cube cannot be reproduced.
    # This fixture's terrain is at z=0, so the datum is 0 -- the point is that
    # the field travelled from the sweep into the manifest at all.
    assert metadata.horizon.height_datum_m == 0.0


def test_loaded_horizon_matches_reference_crop(built_city: Path) -> None:
    """LAZ -> rasters -> sweep -> COG -> loader stays within quantization error."""
    grid = artifacts.load_horizon(built_city / artifacts.HORIZON_FILENAME)
    dsm, dtm = synthetic.cube_scene()
    reference = compute_horizon_reference(dsm, dtm, 1.0, max_distance_m=20.0)
    assert grid.origin == (synthetic.UTM_ORIGIN[0] + 20.0, synthetic.UTM_ORIGIN[1] + 100.0)
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


def test_build_masks_everything_outside_the_computation_area(masked_city: Path) -> None:
    """A city with an ``area`` builds the same grid, computed only where it says.

    The whole chain in one go: a drawn polygon in WGS84, projected, burnt onto
    the grid, the tiles outside it never swept, and ``coverage.tif`` left
    behind so a reader can tell "no data" from "open sky" -- which the cubes
    cannot, both being zeros.
    """
    metadata = artifacts.load_metadata(masked_city)
    assert metadata.coverage is not None
    assert metadata.coverage.covered_fraction == pytest.approx(0.5, abs=0.02)

    with rasterio.open(masked_city / artifacts.COVERAGE_FILENAME) as src:
        coverage = src.read(1) != 0
    assert coverage[:, :39].all() and not coverage[:, 41:].any()

    # Outside the area the cubes say "nothing raises this sector, and nothing
    # is the raiser": the only pair that satisfies the artifact invariants.
    with rasterio.open(masked_city / artifacts.HORIZON_FILENAME) as src:
        assert not src.read(1)[:, 41:].any()
    with rasterio.open(masked_city / artifacts.BLOCKER_CLASS_FILENAME) as src:
        assert (src.read(1)[:, 41:] == NO_BLOCKER).all()


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
            # The suite never talks to Overpass; the footprint path has its own
            # tests with an injected source.
            "--no-footprints",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "binning [1/1] cube.laz" in result.output
    assert "sweeping 1 tiles serially" in result.output
    assert "swept tile [1/1]" in result.output
    assert "binning done in" in result.output
    assert "horizon sweep done in" in result.output
    assert "horizon.tif written (" in result.output
    assert "build done in" in result.output
    assert "of artifacts)" in result.output
    for name in ARTIFACT_FILES:
        assert (output_root / "cube" / "v1" / name).exists(), name


def test_cli_step_mode_geometric(tmp_path: Path) -> None:
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
            "--step-mode",
            "geometric",
            "--no-footprints",
        ],
    )
    assert result.exit_code == 0, result.output
    metadata = artifacts.load_metadata(output_root / "cube" / "v1")
    assert metadata.horizon.step_mode == "geometric"


def test_cli_workers_sweeps_in_parallel(tmp_path: Path) -> None:
    """--workers reaches the sweep and the build comes out whole.

    The one-tile fixture only proves the wiring; that N workers produce the
    same cubes as one is pinned in tests/test_pipeline_horizon.py.
    """
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
            "--tile-size",
            "40",
            "--workers",
            "2",
            "--no-footprints",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "sweeping 4 tiles on 2 workers" in result.output
    for name in ARTIFACT_FILES:
        assert (output_root / "cube" / "v1" / name).exists(), name


def test_cli_requires_lidar_dir(tmp_path: Path) -> None:
    cities_dir = tmp_path / "cities"
    cities_dir.mkdir()
    (cities_dir / "cube.yaml").write_text(CUBE_CITY_YAML)
    result = CliRunner().invoke(app, ["build", "cube", "--cities-dir", str(cities_dir)])
    assert result.exit_code == 1
