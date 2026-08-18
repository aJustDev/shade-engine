"""A render survives being interrupted: finished units are kept, the rest redone.

The scenario these exist for is real and cost a day: a render of 83 instants
aborted at the nineteenth, and the eighteen that had succeeded were worthless
because nothing could tell a finished archive from a truncated one, and because
re-running started from zero.

Two mechanisms, pinned separately -- an archive only gets its real name once it
is complete, and a resumed run only trusts names that came from the same inputs.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import CUBE_CITY
from shade_pipeline.tiles import (
    MANIFEST_FILENAME,
    PARTIAL_SUFFIX,
    RENDER_STATE_FILENAME,
    build_tiles,
)

MORNING = datetime(2026, 6, 21, 9, 0, tzinfo=ZoneInfo("Europe/Madrid"))
NOON = datetime(2026, 6, 21, 13, 0, tzinfo=ZoneInfo("Europe/Madrid"))
MIN_ZOOM, MAX_ZOOM = 17, 18


@pytest.fixture
def rendered(built_city: Path, tmp_path: Path) -> Path:
    """A private copy of the artifacts to render into.

    ``build_tiles`` writes ``tiles/`` inside the artifact directory and
    ``built_city`` is session-scoped, so rendering into it would leak a
    manifest into every later test that copies the fixture.
    """
    target = tmp_path / "city"
    shutil.copytree(built_city, target)
    return target


def _stamps(tiles_dir: Path) -> dict[str, int]:
    return {path.name: path.stat().st_mtime_ns for path in sorted(tiles_dir.glob("*.pmtiles"))}


def test_a_finished_render_leaves_no_partials_and_records_its_inputs(rendered: Path) -> None:
    tiles_dir = build_tiles(CUBE_CITY, rendered, [NOON], min_zoom=MIN_ZOOM, max_zoom=MAX_ZOOM)

    assert list(tiles_dir.glob(f"*{PARTIAL_SUFFIX}")) == []
    recorded = json.loads((tiles_dir / RENDER_STATE_FILENAME).read_text(encoding="utf-8"))
    assert recorded["min_zoom"] == 17
    assert recorded["max_zoom"] == 18
    assert recorded["artifact_built_at"]


def test_an_interrupted_render_leaves_no_archive_under_its_real_name(
    rendered: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism the whole resume rests on.

    A PMTiles writer lays its directory down in ``finalize``, so before this
    change a killed render left a plausible-looking (usually empty) file
    exactly where a finished one belongs. Now the real name only ever appears
    on a complete archive.
    """
    from shade_pipeline import tiles as tiles_module

    real_encode = tiles_module._encode_png
    calls = 0

    def explode(*args: object, **kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        if calls > 3:
            raise RuntimeError("boom, halfway through")
        return real_encode(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tiles_module, "_encode_png", explode)
    with pytest.raises(RuntimeError, match="boom"):
        build_tiles(CUBE_CITY, rendered, [NOON], min_zoom=MIN_ZOOM, max_zoom=MAX_ZOOM)

    tiles_dir = rendered / "tiles"
    assert list(tiles_dir.glob("*.pmtiles")) == [], "a dead render must claim no finished name"
    assert list(tiles_dir.glob(f"*{PARTIAL_SUFFIX}")), "the scratch archive should still be there"


def test_resume_keeps_finished_units_and_still_publishes_the_whole_timeline(
    rendered: Path,
) -> None:
    """Reused units have to reach the manifest, or the timeline loses them.

    The manifest is assembled from the units this run reports, so a resume that
    simply dropped the ones it did not personally render would publish a
    timeline missing every instant it skipped -- a far worse failure than
    re-rendering them.
    """
    build_tiles(CUBE_CITY, rendered, [MORNING, NOON], min_zoom=MIN_ZOOM, max_zoom=MAX_ZOOM)
    tiles_dir = rendered / "tiles"
    before = _stamps(tiles_dir)

    lines: list[str] = []
    build_tiles(
        CUBE_CITY,
        rendered,
        [MORNING, NOON],
        min_zoom=MIN_ZOOM,
        max_zoom=MAX_ZOOM,
        resume=True,
        progress=lines.append,
    )

    assert _stamps(tiles_dir) == before, "resume rewrote archives it should have kept"
    assert any("5 of 5 units already rendered" in line for line in lines)
    manifest = json.loads((tiles_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert [entry["id"] for entry in manifest["instants"]] == ["20260621T0900", "20260621T1300"]


def test_resume_redoes_only_what_is_missing(rendered: Path) -> None:
    build_tiles(CUBE_CITY, rendered, [MORNING, NOON], min_zoom=MIN_ZOOM, max_zoom=MAX_ZOOM)
    tiles_dir = rendered / "tiles"
    before = _stamps(tiles_dir)
    for path in tiles_dir.glob("shade-20260621T1300-*.pmtiles"):
        path.unlink()

    build_tiles(
        CUBE_CITY, rendered, [MORNING, NOON], min_zoom=MIN_ZOOM, max_zoom=MAX_ZOOM, resume=True
    )

    after = _stamps(tiles_dir)
    assert set(after) == set(before), "the deleted unit should be back"
    kept = [name for name in after if "T1300" not in name]
    assert all(after[name] == before[name] for name in kept), "untouched units were rewritten"
    assert all(after[name] != before[name] for name in after if "T1300" in name)


def test_resume_refuses_tiles_rendered_at_another_zoom(rendered: Path) -> None:
    """Existence says "finished"; render.json says "finished from *these* inputs"."""
    build_tiles(CUBE_CITY, rendered, [NOON], min_zoom=17, max_zoom=18)
    tiles_dir = rendered / "tiles"
    before = _stamps(tiles_dir)

    lines: list[str] = []
    build_tiles(
        CUBE_CITY, rendered, [NOON], min_zoom=17, max_zoom=19, resume=True, progress=lines.append
    )

    assert _stamps(tiles_dir) != before, "a deeper pyramid cannot reuse the shallower one"
    assert any("came from other inputs" in line for line in lines)


def test_resume_on_an_empty_directory_just_renders(rendered: Path) -> None:
    lines: list[str] = []
    tiles_dir = build_tiles(
        CUBE_CITY,
        rendered,
        [NOON],
        min_zoom=MIN_ZOOM,
        max_zoom=MAX_ZOOM,
        resume=True,
        progress=lines.append,
    )

    assert any("no render.json" in line for line in lines)
    assert (tiles_dir / MANIFEST_FILENAME).exists()
