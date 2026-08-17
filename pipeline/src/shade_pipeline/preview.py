"""Look at a built city before it goes anywhere.

This is the step that gets skipped, and it is the only one that looks at the
result instead of at numbers. It has been skipped because it was never a step:
it was two servers to start by hand, with the right environment variables, in
two different repositories.

The API half lives here and always works. The web half is best effort on
purpose -- ``shade-web`` is a separate, private repository, and the engine must
not depend on it being checked out. Without it you still get an API to query;
with it you get the map.

A city with no ``basemap.pmtiles`` previews fine: the client falls back to OSM
online when the extract is missing, so the overlay is exactly what it will be in
production and only the backdrop differs.
"""

import os
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

DEFAULT_WEB_DIR = Path.home() / "shade" / "web"
DEFAULT_API_PORT = 8000
DEFAULT_WEB_PORT = 5173


class PreviewError(RuntimeError):
    """The preview could not be started."""


@dataclass(frozen=True)
class Preview:
    """What is running, and where to point a browser."""

    api_url: str
    web_url: str | None

    @property
    def url(self) -> str:
        return self.web_url or self.api_url


def _wait_for(url: str, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=2.0)
            return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise PreviewError(f"{what} did not answer at {url} within {timeout:.0f}s")


@contextmanager
def preview(
    *,
    cities_dir: Path,
    output_root: Path,
    web_dir: Path | None = DEFAULT_WEB_DIR,
    api_port: int = DEFAULT_API_PORT,
    web_port: int = DEFAULT_WEB_PORT,
    progress: Callable[[str], None] | None = None,
) -> Iterator[Preview]:
    """Run a local API (and the viewer, if it is there) until the block exits."""
    echo = progress if progress is not None else lambda _message: None
    environment = {
        **os.environ,
        "SHADE_API_CITIES_DIR": str(cities_dir),
        "SHADE_API_ARTIFACTS_ROOT": str(output_root),
        # A local look at a city should never be throttled by the production
        # rate limit; nothing here is public.
        "SHADE_API_RATE_LIMIT_ENABLED": "false",
    }
    api_url = f"http://127.0.0.1:{api_port}"
    processes: list[subprocess.Popen[bytes]] = []
    try:
        echo(f"starting the api on {api_url}")
        processes.append(
            subprocess.Popen(
                [
                    "uvicorn",
                    "shade_api.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                env=environment,
            )
        )
        _wait_for(f"{api_url}/healthz", 30.0, "the api")

        web_url: str | None = None
        if web_dir is not None and (web_dir / "package.json").exists():
            echo(f"starting the viewer from {web_dir}")
            processes.append(
                subprocess.Popen(
                    ["pnpm", "vite", "--host", "127.0.0.1", "--port", str(web_port)],
                    cwd=web_dir,
                    # The viewer's dev server proxies /v1 and /tiles; pointing
                    # that proxy here is what makes it show the local build
                    # instead of production.
                    env={**environment, "SHADE_API_PROXY": api_url},
                )
            )
            web_url = f"http://127.0.0.1:{web_port}"
            _wait_for(web_url, 60.0, "the viewer")
        elif web_dir is not None:
            echo(f"no viewer at {web_dir}; serving the api alone")

        yield Preview(api_url=api_url, web_url=web_url)
    finally:
        # Reverse order, and terminate rather than kill: vite writes to its
        # cache on the way out.
        for process in reversed(processes):
            process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
