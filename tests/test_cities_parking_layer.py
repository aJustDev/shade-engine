"""Regression tests for the committed Cordoba parking layer.

The GeoJSON is the deliverable (its generator script runs once against a
frozen Wayback capture), so these tests validate the committed data itself:
schema of the spec's section 5.1, geometry sanity and coordinate ranges.
No network, no script execution.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

LAYER_PATH = Path(__file__).resolve().parents[1] / "cities" / "cordoba" / "parking.geojson"
HHMM_RE = re.compile(r"^\d{2}:\d{2}$")

# Urban Cordoba in WGS84 (generous bounds around the EPSG:25830 city bbox).
LON_RANGE = (-4.90, -4.70)
LAT_RANGE = (37.83, 37.95)


@pytest.fixture(scope="module")
def features() -> list[dict[str, Any]]:
    collection = json.loads(LAYER_PATH.read_text())
    assert collection["type"] == "FeatureCollection"
    result: list[dict[str, Any]] = collection["features"]
    return result


def test_zone_and_segment_counts(features: list[dict[str, Any]]) -> None:
    assert len(features) == 21
    segments = sum(len(feature["geometry"]["coordinates"]) for feature in features)
    assert segments == 51


def test_properties_follow_spec_schema(features: list[dict[str, Any]]) -> None:
    for feature in features:
        properties = feature["properties"]
        assert properties["zone_type"] == "blue"
        assert properties["orientation"] in ("bateria", "cordon")
        assert isinstance(properties["capacity"], int) and properties["capacity"] > 0
        assert properties["max_minutes"] == 120
        assert properties["tariff_eur_hour"] == 0.9
        assert properties["name"]
        for key in ("notes", "source", "last_verified"):
            assert properties[key], key
        for entry in properties["schedule"]:
            assert entry["days"] in ("mo-fr", "sa")
            assert HHMM_RE.match(entry["from"]) and HHMM_RE.match(entry["to"])
            assert entry["from"] < entry["to"]


def test_geometries_are_multilines_inside_cordoba(features: list[dict[str, Any]]) -> None:
    for feature in features:
        geometry = feature["geometry"]
        assert geometry["type"] == "MultiLineString"
        for segment in geometry["coordinates"]:
            assert len(segment) >= 2
            for lon, lat in segment:
                assert LON_RANGE[0] < lon < LON_RANGE[1]
                assert LAT_RANGE[0] < lat < LAT_RANGE[1]


def test_layer_is_ascii(features: list[dict[str, Any]]) -> None:
    LAYER_PATH.read_text(encoding="ascii")
