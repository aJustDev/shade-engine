import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import laz_fixture
import synthetic
from shade_api.app import create_app
from shade_api.settings import ApiSettings
from shade_core.config import CityConfig
from shade_core.horizon import HorizonGrid, compute_horizon_reference
from shade_core.shade import ShadeScene
from shade_pipeline.build import build_city
from shade_pipeline.sources import LocalDirectory


@pytest.fixture(scope="session")
def cube_grid() -> HorizonGrid:
    """Horizon of the 20 m cube scene; computed once, reused across test files."""
    dsm, dtm = synthetic.cube_scene()
    return compute_horizon_reference(dsm, dtm, synthetic.RESOLUTION_M, max_distance_m=80.0)


@pytest.fixture(scope="session")
def cube_shade_scene(cube_grid: HorizonGrid) -> ShadeScene:
    dsm, dtm = synthetic.cube_scene()
    return ShadeScene(horizon=cube_grid, landcover=synthetic.cube_landcover(), dsm=dsm, dtm=dtm)


@pytest.fixture(scope="session")
def tree_shade_scene() -> ShadeScene:
    dsm, dtm, landcover, canopy = synthetic.tree_scene()
    grid = compute_horizon_reference(dsm, dtm, synthetic.RESOLUTION_M, max_distance_m=40.0)
    return ShadeScene(horizon=grid, landcover=landcover, canopy=canopy, dsm=dsm, dtm=dtm)


# The built-city fixture config, at real Cordoba UTM coordinates so lat/lon
# queries through the API resolve into the scene. Single source of truth for
# every test that needs the city's georeference or metadata.
CUBE_CITY = CityConfig(
    id="cube",
    name="Cube",
    country="ES",
    timezone="Europe/Madrid",
    crs="EPSG:25830",
    bbox=(
        synthetic.UTM_ORIGIN[0] + 20.0,
        synthetic.UTM_ORIGIN[1] + 20.0,
        synthetic.UTM_ORIGIN[0] + 100.0,
        synthetic.UTM_ORIGIN[1] + 100.0,
    ),
    resolution_m=1.0,
    horizon_sectors=64,
    horizon_max_distance_m=20.0,
    attribution=["Synthetic LiDAR (test fixture)"],
)


@pytest.fixture(scope="session")
def built_city(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Full pipeline run over the cube LAZ; returns the artifact directory.

    The city bbox is the inner 80x80 of the synthetic scene and max_distance
    is 20 m, so the padded bbox is exactly the full 120x120 scene: coverage
    passes with nothing to spare, exercising the padding arithmetic end to
    end. The golden NEAR point stays inside the inner bbox.
    """
    root = tmp_path_factory.mktemp("built_city")
    lidar_dir = root / "lidar"
    lidar_dir.mkdir()
    laz_fixture.write_cube_laz(lidar_dir / "cube.laz", origin=synthetic.UTM_ORIGIN)
    return build_city(CUBE_CITY, LocalDirectory(lidar_dir), root / "data")


@pytest.fixture(scope="session")
def api_settings(built_city: Path, tmp_path_factory: pytest.TempPathFactory) -> ApiSettings:
    """Settings pointing at the built fixture city plus a ghost city.

    The ghost has a valid YAML but no artifacts: the registry must skip it.
    Rate limiting is off; the dedicated limits test builds its own app.
    """
    cities_dir = tmp_path_factory.mktemp("api_cities")
    (cities_dir / "cube.yaml").write_text(yaml.safe_dump(CUBE_CITY.model_dump(mode="json")))
    ghost = CUBE_CITY.model_copy(update={"id": "ghost"})
    (cities_dir / "ghost.yaml").write_text(yaml.safe_dump(ghost.model_dump(mode="json")))
    return ApiSettings(
        cities_dir=cities_dir,
        artifacts_root=built_city.parent.parent,
        cors_origins=["https://example.test"],
        rate_limit_enabled=False,
    )


@pytest.fixture(scope="session")
def client(api_settings: ApiSettings) -> Iterator[TestClient]:
    """API test client; the context manager runs the lifespan (registry load)."""
    with TestClient(create_app(api_settings)) as instance:
        yield instance


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://shade:shade@localhost:5432/shade"


@pytest.fixture(scope="session")
def parking_db() -> Iterator[Engine]:
    """Engine bound to a scratch PostGIS database with migrations applied.

    Skips when no server is reachable (local runs without the compose db);
    in CI the postgis service container is mandatory, so unreachable there
    is a hard failure, never a silent skip. A scratch database with a
    unique name keeps pytest away from dev data imported into the compose
    database on the same port, and running ``alembic upgrade head`` here
    means every DB test also proves the migrations apply.
    """
    admin_url = os.environ.get("SHADE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    admin = create_engine(
        admin_url, connect_args={"connect_timeout": 2}, isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        admin.dispose()
        if os.environ.get("CI"):
            raise RuntimeError(f"PostGIS unreachable in CI at {admin_url}") from exc
        pytest.skip(f"no PostGIS server at {admin_url}")
    scratch = f"shade_test_{uuid.uuid4().hex[:8]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{scratch}"'))
    scratch_url = make_url(admin_url).set(database=scratch).render_as_string(hide_password=False)
    alembic_config = Config(str(REPO_ROOT / "alembic.ini"))
    alembic_config.set_main_option("sqlalchemy.url", scratch_url)
    command.upgrade(alembic_config, "head")
    engine = create_engine(scratch_url)
    yield engine
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{scratch}" WITH (FORCE)'))
    admin.dispose()
