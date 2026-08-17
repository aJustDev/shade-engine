"""Preview: what the two servers get told, which is the whole of what can break.

Nothing here starts a server. What went wrong in practice was neither process
dying nor a port clash -- both halves came up fine -- but the viewer being
handed an environment that left its tile URLs pointing at nothing, so the page
loaded, the city list worked, and the map parsed ``index.html`` as JSON. That is
a wiring bug, and wiring is exactly what a test can pin.
"""

from pathlib import Path
from typing import Any

import pytest

from shade_pipeline import preview as preview_module
from shade_pipeline.preview import preview


class FakeProcess:
    """Enough of Popen for the context manager to start and stop it."""

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.env: dict[str, str] = kwargs.get("env") or {}
        self.cwd = kwargs.get("cwd")
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:  # pragma: no cover - only on a hung child
        self.terminated = True


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[FakeProcess]:
    """Every process the preview would have launched, in order."""
    processes: list[FakeProcess] = []

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        process = FakeProcess(argv, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(preview_module, "_wait_for", lambda *args, **kwargs: None)
    monkeypatch.setattr(preview_module, "_claim_ports", lambda *args: None)
    return processes


@pytest.fixture
def viewer(tmp_path: Path) -> Path:
    """A directory that looks enough like shade-web to be started."""
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text("{}", encoding="utf-8")
    (web / ".env.local").write_text("VITE_API_BASE_URL=http://localhost:5173\n", encoding="utf-8")
    return web


def _vite(processes: list[FakeProcess]) -> FakeProcess:
    return next(process for process in processes if "vite" in process.argv)


def test_the_viewer_is_told_where_the_tiles_are(
    started: list[FakeProcess], viewer: Path, tmp_path: Path
) -> None:
    """The bug: tiles are not proxied, they are served off disk by a plugin.

    That plugin only mounts when SHADE_TILES_DIR is set, because PMTiles are
    read with Range requests and vite's static server cannot answer 206.
    Without it every tile URL falls through to the SPA fallback, and the client
    tries to parse index.html as JSON.
    """
    artifacts = tmp_path / "out"
    artifacts.mkdir()

    with preview(cities_dir=tmp_path / "cities", output_root=artifacts, web_dir=viewer):
        pass

    assert _vite(started).env["SHADE_TILES_DIR"] == str(artifacts.resolve())


def test_the_viewer_is_pointed_at_the_local_api(
    started: list[FakeProcess], viewer: Path, tmp_path: Path
) -> None:
    with preview(
        cities_dir=tmp_path / "cities",
        output_root=tmp_path,
        web_dir=viewer,
        api_port=8123,
    ):
        pass

    assert _vite(started).env["SHADE_API_PROXY"] == "http://127.0.0.1:8123"


def test_the_api_serves_the_city_being_previewed_and_is_not_throttled(
    started: list[FakeProcess], tmp_path: Path
) -> None:
    cities = tmp_path / "cities"

    with preview(cities_dir=cities, output_root=tmp_path / "out", web_dir=None):
        pass

    api = started[0]
    assert api.env["SHADE_API_CITIES_DIR"] == str(cities)
    assert api.env["SHADE_API_RATE_LIMIT_ENABLED"] == "false"


def test_a_viewer_with_no_env_local_is_called_out(
    started: list[FakeProcess], tmp_path: Path
) -> None:
    """Without it the viewer reads production: the map looks right and is wrong."""
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text("{}", encoding="utf-8")
    said: list[str] = []

    with preview(
        cities_dir=tmp_path / "cities",
        output_root=tmp_path,
        web_dir=web,
        progress=said.append,
    ):
        pass

    assert any("production instead of this preview" in line for line in said)


def test_without_a_viewer_the_api_still_runs(started: list[FakeProcess], tmp_path: Path) -> None:
    """shade-web is a separate private repo; the engine cannot require it."""
    with preview(
        cities_dir=tmp_path / "cities", output_root=tmp_path, web_dir=tmp_path / "missing"
    ) as running:
        assert running.web_url is None
        assert running.url == running.api_url

    assert len(started) == 1


def test_everything_started_is_stopped_again(
    started: list[FakeProcess], viewer: Path, tmp_path: Path
) -> None:
    with preview(cities_dir=tmp_path / "cities", output_root=tmp_path, web_dir=viewer):
        pass

    assert started and all(process.terminated for process in started)
