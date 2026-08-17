"""Look at a built city before it goes anywhere.

This is the step that gets skipped, and it is the only one that looks at the
result instead of at numbers. It has been skipped because it was never a step:
it was two servers to start by hand, with the right environment variables, in
two different repositories.

The API half lives here and always works. The web half is best effort on
purpose -- ``shade-web`` is a separate, private repository, and the engine must
not depend on it being checked out. Without it you still get an API to query;
with it you get the map.

A city with no ``basemap.pmtiles`` previews, but it does not preview *fine*, and
this file used to claim otherwise. The overlay carries no street, no label and
no building outline -- all of that is the basemap underneath -- so without it the
map is shade on black, which at low zoom reads as a haze over nothing. The
viewer falls back to OSM online only when the manifest omits ``basemap_url``
entirely; a manifest that names a file which is not there gets a source that
404s and a black backdrop. Hence the warnings below: what is missing is said out
loud, before you spend ten minutes wondering what you are looking at.
"""

import contextlib
import os
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _in_use(port: int) -> bool:
    """Whether something already holds ``port`` on the loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def claim_ports(api_port: int, web_port: int) -> None:
    """Refuse to start on top of a preview that is already running.

    Both halves used to fail quietly and in different ways, which between them
    produced the worst outcome available: uvicorn exits on a bound port, so the
    health check was answered by the *previous* API; vite does not exit, it
    walks forward to 5174, 5175, and the caller went on announcing 5173. What
    you got was a browser pointed at somebody else's server, showing an older
    build, with nothing anywhere saying so.
    """
    taken = [str(port) for port in (api_port, web_port) if _in_use(port)]
    if taken:
        raise PreviewError(
            f"already listening on 127.0.0.1:{', '.join(taken)} -- a preview does not stop "
            f"by itself, so this is probably an earlier one. Stop it first, or pass "
            f"--api-port/--web-port to run beside it."
        )


def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the child's whole process group, not just the child.

    ``pnpm vite`` is a chain: pnpm execs a second pnpm which spawns the node
    process that actually holds the port. Terminating what we started killed the
    wrapper and left the server orphaned and listening, which is what made every
    later preview walk to the next port -- and why five of them could pile up
    with nothing to show for it. Each child is started in its own session, so
    its group id is its pid and one signal reaches the lot.
    """
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except ProcessLookupError, PermissionError:
        # Already gone, or not ours to signal: fall back to the child alone.
        with contextlib.suppress(OSError):
            process.send_signal(sig)


def _wait_for(
    url: str, timeout: float, what: str, process: subprocess.Popen[bytes] | None = None
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # A child that has already exited will never answer, and waiting the
        # full timeout to say so buries the reason it exited.
        if process is not None and process.poll() is not None:
            raise PreviewError(f"{what} exited with {process.returncode} before answering")
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
    log: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> Iterator[Preview]:
    """Run a local API (and the viewer, if it is there) until the block exits.

    ``log`` collects both servers' own output. They are the two processes here
    that can fail *after* starting -- a missing artifact, a proxy that 502s, a
    tile the viewer cannot find -- and without it that goes to whatever stdout
    the caller had, which for a console launching this detached is nowhere.
    """
    echo = progress if progress is not None else lambda _message: None
    claim_ports(api_port, web_port)
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
    # One file for both servers rather than one each: what you want to read is
    # a request arriving at the viewer and what the API made of it, in order.
    sink = log.open("a", encoding="utf-8", buffering=1) if log is not None else None
    output: dict[str, Any] = (
        {"stdout": sink, "stderr": subprocess.STDOUT} if sink is not None else {}
    )
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
                # Its own session, so _stop can signal a group. Also keeps a
                # Ctrl-C in the launching terminal from racing the orderly
                # shutdown below.
                start_new_session=True,
                **output,
            )
        )
        _wait_for(f"{api_url}/healthz", 30.0, "the api", processes[-1])

        web_url: str | None = None
        if web_dir is not None and (web_dir / "package.json").exists():
            echo(f"starting the viewer from {web_dir}")
            if not (web_dir / ".env.local").exists():
                # Which URLs the app calls is the viewer's own configuration,
                # and without that file it defaults to production: the map would
                # look right and be somebody else's city.
                echo(
                    f"warning: no .env.local in {web_dir}; the viewer will read "
                    f"production instead of this preview (see its .env.example)"
                )
            processes.append(
                subprocess.Popen(
                    # --strictPort: without it vite walks forward to the next
                    # free port and says so only in its own output, so the URL
                    # this function returns stops being true.
                    [
                        "pnpm",
                        "vite",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(web_port),
                        "--strictPort",
                    ],
                    cwd=web_dir,
                    # Two different mechanisms, and they are easy to confuse.
                    # The dev server proxies **/v1 only**, to SHADE_API_PROXY.
                    # Tiles are not proxied at all: they are served off the disk
                    # by a plugin that only mounts when SHADE_TILES_DIR is set,
                    # because PMTiles are read with Range requests and vite's
                    # static server cannot answer 206. Without it every tile URL
                    # falls through to the SPA fallback and the client parses
                    # index.html as JSON.
                    env={
                        **environment,
                        "SHADE_API_PROXY": api_url,
                        "SHADE_TILES_DIR": str(output_root.resolve()),
                    },
                    start_new_session=True,
                    **output,
                )
            )
            web_url = f"http://127.0.0.1:{web_port}"
            _wait_for(web_url, 60.0, "the viewer", processes[-1])
        elif web_dir is not None:
            echo(f"no viewer at {web_dir}; serving the api alone")

        yield Preview(api_url=api_url, web_url=web_url)
    finally:
        # Reverse order, and signal the group rather than the process: see
        # _signal_group. Terminate rather than kill, because vite writes its dependency
        # cache on the way out.
        for process in reversed(processes):
            _signal_group(process, signal.SIGTERM)
        for process in reversed(processes):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _signal_group(process, signal.SIGKILL)
        if sink is not None:
            sink.close()
