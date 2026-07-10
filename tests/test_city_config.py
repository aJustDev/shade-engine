from pathlib import Path

import pytest
from pydantic import ValidationError

from shade_core.config import CityConfig, load_city

CITIES_DIR = Path(__file__).parent.parent / "cities"


def test_load_cordoba() -> None:
    city = load_city(CITIES_DIR / "cordoba.yaml")
    assert city.id == "cordoba"
    assert city.timezone == "Europe/Madrid"
    assert city.crs == "EPSG:25830"
    assert city.bbox == (341000, 4192000, 349000, 4199000)
    assert city.resolution_m == 1.0
    assert city.horizon_sectors == 64
    assert city.attribution  # IGN attribution is a license requirement


def test_every_city_file_validates() -> None:
    files = list(CITIES_DIR.glob("*.yaml"))
    assert files
    for path in files:
        load_city(path)


def test_unordered_bbox_rejected() -> None:
    data = load_city(CITIES_DIR / "cordoba.yaml").model_dump()
    data["bbox"] = (349000, 4192000, 341000, 4199000)  # min_x > max_x
    with pytest.raises(ValidationError):
        CityConfig.model_validate(data)


def test_unknown_timezone_rejected() -> None:
    data = load_city(CITIES_DIR / "cordoba.yaml").model_dump()
    data["timezone"] = "Europe/Cordoba"
    with pytest.raises(ValidationError):
        CityConfig.model_validate(data)
