"""Rate limiting and CORS behavior of the app factory."""

from fastapi.testclient import TestClient

from shade_api.app import create_app
from shade_api.settings import ApiSettings


def test_rate_limit_returns_429(api_settings: ApiSettings) -> None:
    """A dedicated app with a tiny per-hour window (no rollover flakes)."""
    settings = api_settings.model_copy(update={"rate_limit": "2/hour", "rate_limit_enabled": True})
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").status_code == 200
        response = client.get("/healthz")
        assert response.status_code == 429
        assert "rate limit" in response.json()["detail"]


def test_disabled_rate_limit_never_throttles(client: TestClient) -> None:
    """The shared fixture app has the limiter disabled."""
    for _ in range(5):
        assert client.get("/healthz").status_code == 200


def test_cors_allows_configured_origin(client: TestClient) -> None:
    response = client.get("/healthz", headers={"Origin": "https://example.test"})
    assert response.headers["access-control-allow-origin"] == "https://example.test"


def test_cors_preflight(client: TestClient) -> None:
    response = client.options(
        "/v1/cities",
        headers={
            "Origin": "https://example.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://example.test"


def test_cors_denies_unknown_origin(client: TestClient) -> None:
    response = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_no_cors_headers_when_unconfigured(api_settings: ApiSettings) -> None:
    settings = api_settings.model_copy(update={"cors_origins": []})
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz", headers={"Origin": "https://example.test"})
        assert "access-control-allow-origin" not in response.headers
