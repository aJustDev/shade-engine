"""/v1/cities, /v1/cities/{id} and /healthz over the built fixture city."""

from fastapi.testclient import TestClient


def test_lists_only_built_cities(client: TestClient) -> None:
    response = client.get("/v1/cities")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=3600"
    cities = response.json()
    assert [city["id"] for city in cities] == ["cube"]  # ghost has no artifacts
    (cube,) = cities
    assert cube["name"] == "Cube"
    assert cube["timezone"] == "Europe/Madrid"
    assert cube["attribution"] == ["Synthetic LiDAR (test fixture)"]
    min_lon, min_lat, max_lon, max_lat = cube["bbox_wgs84"]
    assert -4.9 < min_lon < max_lon < -4.7
    assert 37.8 < min_lat < max_lat < 37.95


def test_city_detail_exposes_build_metadata(client: TestClient) -> None:
    response = client.get("/v1/cities/cube")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "cube"
    assert body["artifacts"]["artifact_version"] == "v1"
    assert body["artifacts"]["horizon"]["sectors"] == 64
    assert body["artifacts"]["crs"] == "EPSG:25830"


def test_unknown_city_is_404(client: TestClient) -> None:
    response = client.get("/v1/cities/atlantis")
    assert response.status_code == 404
    assert "atlantis" in response.json()["detail"]


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "cities": 1}


def test_openapi_is_public_documentation(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/v1/cities" in response.json()["paths"]
