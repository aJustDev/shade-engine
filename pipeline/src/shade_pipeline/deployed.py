"""What the public API is actually serving, next to what this machine holds.

The console has always been able to say where a city stands *here*: which steps
ran, which are stale, what is running now. What it could never say is anything
about the other end -- whether the city is published at all, whether production
is serving the build in ``data/cities`` or one from three engines ago. The
answer was a browser tab and a memory, which is exactly the pairing
:mod:`shade_pipeline.runstate` exists to replace.

It needs no ssh: ``GET /v1/cities/{id}`` already returns the artifact's whole
:class:`shade_core.artifacts.BuildMetadata`, and it is a public endpoint with a
one-hour cache header.

**The trap, and it is the reason nothing here is strict.** The deployed API
reserializes ``metadata.json`` through *its own* model, so a field its version
does not know about simply does not appear in the response. An absent
``engine_version`` therefore means "the artifact predates the field" *or* "the
API does" and the two cannot be told apart from outside. Both mean the same
thing for the only question being asked -- it is not the engine in this working
copy -- so the verdict is the same and only the wording changes. Nothing here
guesses which.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import httpx
from pydantic import BaseModel, Field

from shade_core.artifacts import BuildMetadata
from shade_core.engine import ARTIFACT_ENGINE_VERSION

DEFAULT_TIMEOUT_S = 10.0
"""Enough for a cold proxy, short enough that a dead server does not hang a key.

The console calls this from a worker thread on a keypress. A default httpx
timeout of five seconds per phase would still be fine; this is one number for
the whole request because there is nothing useful to do with a partial answer.
"""


class DeployedError(RuntimeError):
    """The server could not be asked. Never "the city is not there"."""


class Verdict(StrEnum):
    """The relationship between what is here and what is served."""

    LIVE = "live"
    """Production is serving this build."""
    BEHIND = "behind"
    """Production is serving an older build than the one on this machine."""
    AHEAD = "ahead"
    """Production is serving a newer one, which means it was built elsewhere."""
    NOT_PUBLISHED = "unpublished"
    """Built here, and the server has never heard of it."""
    NOT_BUILT = "unbuilt"
    """Nothing here to compare, and nothing there either."""
    UNKNOWN = "unknown"
    """The server could not be reached. Deliberately not "no"."""


class DeployedCity(BaseModel):
    """The little of a served city's metadata this comparison needs.

    Every field optional, and it is not laziness: the response comes from a
    deployment that may be older or newer than this checkout, so a model that
    required anything would fail on the very drift it exists to report. Extra
    keys are ignored, which is pydantic's default and the right one here.
    """

    city: str
    built_at: datetime | None = None
    engine_version: int | None = None
    resolution_m: float | None = None
    sectors: int | None = None
    max_distance_m: float | None = None
    step_mode: str | None = None
    software: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def of(cls, city: str, payload: dict[str, Any]) -> Self:
        """Read one ``/v1/cities/{id}`` body, tolerating anything missing."""
        artifacts = payload.get("artifacts") or {}
        horizon = artifacts.get("horizon") or {}
        return cls(
            city=city,
            built_at=artifacts.get("built_at"),
            engine_version=artifacts.get("engine_version"),
            resolution_m=artifacts.get("resolution_m"),
            sectors=horizon.get("sectors"),
            max_distance_m=horizon.get("max_distance_m"),
            step_mode=horizon.get("step_mode"),
            software=artifacts.get("software") or {},
        )


@dataclass(frozen=True)
class Comparison:
    """One city's verdict, plus the differences a reader needs to check it."""

    city: str
    verdict: Verdict
    lines: list[str]

    def describe(self) -> str:
        return "\n".join([f"{self.city}: {self.verdict.value}", *(f"  {x}" for x in self.lines)])


def fetch_deployed(
    city: str,
    *,
    base_url: str,
    client: httpx.Client | None = None,
) -> DeployedCity | None:
    """Ask the API about one city; None when it answers 404.

    ``client`` is injected rather than built here for the reason
    :class:`shade_pipeline.cnig.CnigSource` does it: the tests drive this with
    ``httpx.MockTransport`` and never open a socket.
    """
    url = f"{base_url.rstrip('/')}/v1/cities/{city}"
    try:
        if client is None:
            with httpx.Client(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True) as fresh:
                response = fresh.get(url)
        else:
            response = client.get(url)
    except httpx.HTTPError as error:
        raise DeployedError(f"{url}: {error}") from error
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise DeployedError(f"{url}: HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise DeployedError(f"{url}: the answer is not JSON") from error
    return DeployedCity.of(city, payload)


def local_metadata(city: str, output_root: Path, artifact_version: str = "v1") -> BuildMetadata:
    """This machine's build of a city, or a raise if there is not one.

    Imported lazily by callers that care about startup: it opens
    :mod:`shade_core.artifacts`, which brings rasterio.
    """
    from shade_core.artifacts import load_metadata

    return load_metadata(output_root / city / artifact_version)


def compare(city: str, local: BuildMetadata | None, remote: DeployedCity | None) -> Comparison:
    """The verdict, and every difference worth reading under it."""
    if remote is None:
        if local is None:
            return Comparison(city, Verdict.NOT_BUILT, ["nothing built here, nothing served"])
        return Comparison(
            city,
            Verdict.NOT_PUBLISHED,
            [f"built here {_when(local.built_at)}, and the server does not list it"],
        )
    if local is None:
        return Comparison(
            city,
            Verdict.AHEAD,
            [
                f"served {_when(remote.built_at)}, and nothing is built here: "
                f"that build was made somewhere else"
            ],
        )

    lines = [f"served {_when(remote.built_at)}, built here {_when(local.built_at)}"]
    lines.extend(_differences(local, remote))
    return Comparison(city, _order(local.built_at, remote.built_at), lines)


def _order(local_at: datetime | None, remote_at: datetime | None) -> Verdict:
    if remote_at is None or local_at is None:
        # A served city that will not say when it was built cannot be placed
        # against anything. It is not "live" and it is not "behind".
        return Verdict.UNKNOWN
    if remote_at == local_at:
        return Verdict.LIVE
    return Verdict.BEHIND if remote_at < local_at else Verdict.AHEAD


def _differences(local: BuildMetadata, remote: DeployedCity) -> list[str]:
    """Everything that does not match, plus the engine line, which always shows."""
    lines: list[str] = []
    if remote.engine_version is None:
        lines.append(
            f"the server reports no engine version: either that artifact predates "
            f"the field or the deployed API does, and from outside the two look "
            f"the same. Either way it is not v{ARTIFACT_ENGINE_VERSION}"
        )
    elif remote.engine_version != ARTIFACT_ENGINE_VERSION:
        lines.append(f"engine v{remote.engine_version} served, v{ARTIFACT_ENGINE_VERSION} here")
    for label, there, here in (
        ("resolution_m", remote.resolution_m, local.resolution_m),
        ("sectors", remote.sectors, local.horizon.sectors),
        ("max_distance_m", remote.max_distance_m, local.horizon.max_distance_m),
        ("step_mode", remote.step_mode, local.horizon.step_mode),
    ):
        if there is not None and there != here:
            lines.append(f"{label}: {there} served, {here} here")
    return lines


def _when(moment: datetime | None) -> str:
    return "at an unknown date" if moment is None else moment.strftime("%d %b %Y %H:%M")


def survey(
    cities: list[str],
    *,
    output_root: Path,
    base_url: str,
    client: httpx.Client | None = None,
    artifact_version: str = "v1",
) -> list[Comparison]:
    """Compare every city, turning an unreachable server into a verdict.

    One request per city and no concurrency: this runs on a keypress over a
    handful of cities, and a thread pool here would buy milliseconds at the
    price of a second way for the console to hang.
    """
    results: list[Comparison] = []
    for city in cities:
        try:
            local = local_metadata(city, output_root, artifact_version)
        except OSError, ValueError:
            local = None
        try:
            remote = fetch_deployed(city, base_url=base_url, client=client)
        except DeployedError as error:
            results.append(Comparison(city, Verdict.UNKNOWN, [str(error)]))
            continue
        results.append(compare(city, local, remote))
    return results
