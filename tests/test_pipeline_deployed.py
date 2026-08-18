"""What production is serving, next to what this machine holds.

Driven with ``httpx.MockTransport`` and a real artifact directory: the payloads
are this repository's own ``metadata.json`` degraded on purpose, which is the
only way to test the case that matters -- a deployment older than the checkout
asking about it.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from shade_core.artifacts import BuildMetadata, load_metadata
from shade_core.engine import ARTIFACT_ENGINE_VERSION
from shade_pipeline.deployed import (
    DeployedError,
    Verdict,
    compare,
    fetch_deployed,
    survey,
)

BASE_URL = "https://example.invalid"


@pytest.fixture
def metadata(built_city: Path) -> BuildMetadata:
    return load_metadata(built_city)


def _body(metadata: BuildMetadata, **overrides: Any) -> dict[str, Any]:
    """A ``/v1/cities/{id}`` body from a real artifact, then degraded."""
    artifacts: dict[str, Any] = json.loads(metadata.model_dump_json())
    artifacts.update(overrides)
    return {"id": artifacts["city_id"], "artifacts": artifacts}


def _serving(body: dict[str, Any] | None) -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(404, json={"detail": "unknown city"})
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_same_build_on_both_sides_is_live(metadata: BuildMetadata) -> None:
    client = _serving(_body(metadata, engine_version=ARTIFACT_ENGINE_VERSION))

    result = compare("cube", metadata, fetch_deployed("cube", base_url=BASE_URL, client=client))

    assert result.verdict is Verdict.LIVE
    assert result.lines == [result.lines[0]], result.lines


def test_an_older_build_on_the_server_is_behind(metadata: BuildMetadata) -> None:
    older = (metadata.built_at - timedelta(days=2)).isoformat()
    client = _serving(_body(metadata, built_at=older, engine_version=ARTIFACT_ENGINE_VERSION))

    result = compare("cube", metadata, fetch_deployed("cube", base_url=BASE_URL, client=client))

    assert result.verdict is Verdict.BEHIND


def test_a_newer_build_on_the_server_was_made_somewhere_else(metadata: BuildMetadata) -> None:
    """Cordoba's case: the heavy build ran on the VPS, so this machine is behind it."""
    newer = (metadata.built_at + timedelta(days=2)).isoformat()
    client = _serving(_body(metadata, built_at=newer, engine_version=ARTIFACT_ENGINE_VERSION))

    result = compare("cube", metadata, fetch_deployed("cube", base_url=BASE_URL, client=client))

    assert result.verdict is Verdict.AHEAD


def test_a_city_the_server_does_not_list_is_unpublished(metadata: BuildMetadata) -> None:
    result = compare(
        "cube", metadata, fetch_deployed("cube", base_url=BASE_URL, client=_serving(None))
    )

    assert result.verdict is Verdict.NOT_PUBLISHED
    assert "does not list it" in result.lines[0]


def test_an_unreachable_server_is_unknown_and_never_absent(built_city: Path) -> None:
    """The distinction the whole module turns on: no answer is not a no."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(DeployedError):
        fetch_deployed("cube", base_url=BASE_URL, client=client)

    results = survey(
        ["cube"],
        output_root=built_city.parent.parent,
        base_url=BASE_URL,
        client=client,
    )

    assert results[0].verdict is Verdict.UNKNOWN


def test_a_response_without_an_engine_version_says_why_it_cannot_tell(
    metadata: BuildMetadata,
) -> None:
    """The ambiguity is real and reported as such: old artifact, or old API."""
    body = _body(metadata)
    del body["artifacts"]["engine_version"]
    client = _serving(body)

    result = compare("cube", metadata, fetch_deployed("cube", base_url=BASE_URL, client=client))

    assert result.verdict is Verdict.LIVE
    explanation = "\n".join(result.lines)
    assert "predates the field" in explanation
    assert "the deployed API does" in explanation


def test_an_older_engine_on_the_server_is_named(metadata: BuildMetadata) -> None:
    client = _serving(_body(metadata, engine_version=ARTIFACT_ENGINE_VERSION - 1))

    result = compare("cube", metadata, fetch_deployed("cube", base_url=BASE_URL, client=client))

    assert any(f"engine v{ARTIFACT_ENGINE_VERSION - 1} served" in line for line in result.lines)


def test_a_sweep_parameter_that_moved_is_reported(metadata: BuildMetadata) -> None:
    """What production would answer with, in the terms the ADRs argue about."""
    body = _body(metadata, engine_version=ARTIFACT_ENGINE_VERSION)
    body["artifacts"]["horizon"]["step_mode"] = "geometric"
    client = _serving(body)

    result = compare("cube", metadata, fetch_deployed("cube", base_url=BASE_URL, client=client))

    assert any("step_mode: geometric served" in line for line in result.lines)


def test_a_city_built_nowhere_is_neither_published_nor_missing(tmp_path: Path) -> None:
    results = survey(["cube"], output_root=tmp_path, base_url=BASE_URL, client=_serving(None))

    assert results[0].verdict is Verdict.NOT_BUILT
