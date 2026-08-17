"""Basemap: the date that expires, the margin in metres, and the half-written file.

Nothing here touches the network or the ``pmtiles`` binary. What has actually
gone wrong with this piece is not the extraction -- that is one subprocess call
-- but the three things around it: a build date that stops existing after a
week, a margin padded in the wrong units, and a file that exists without being
finished, which everything downstream reads as "there is a basemap".
"""

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from shade_core.config import CityConfig
from shade_pipeline.basemap import (
    BUILD_URL,
    FONT_STACKS,
    SPRITE_SET,
    BasemapError,
    build_basemap,
    declare_in_manifest,
    ensure_assets,
    extract_bbox,
    newest_build,
)
from shade_pipeline.tiles import BASEMAP_FILENAME, MANIFEST_FILENAME, PARTIAL_SUFFIX, TILES_DIRNAME

CITY = CityConfig(
    id="montalban",
    name="Montalban de Cordoba",
    country="ES",
    timezone="Europe/Madrid",
    crs="EPSG:25830",
    bbox=(345074.0, 4159819.0, 346122.0, 4161773.0),
    resolution_m=1.0,
)


# ------------------------------------------------------------------- the date


def test_newest_build_takes_today_when_it_is_there() -> None:
    assert newest_build(date(2026, 8, 17), probe=lambda _stamp: True) == "20260817"


def test_newest_build_walks_back_because_retention_is_about_a_week() -> None:
    """There is no "latest" URL and the archive is deleted after a few days.

    Cordoba was cut from 20260712, which is now a 404. A constant here would
    have been a bug with an expiry date on it.
    """
    asked: list[str] = []

    def probe(stamp: str) -> bool:
        asked.append(stamp)
        return stamp == "20260814"

    assert newest_build(date(2026, 8, 17), probe=probe) == "20260814"
    assert asked == ["20260817", "20260816", "20260815", "20260814"]


def test_newest_build_gives_up_with_the_url_in_the_message() -> None:
    with pytest.raises(BasemapError, match=r"build\.protomaps\.com"):
        newest_build(date(2026, 8, 17), back=3, probe=lambda _stamp: False)


# ------------------------------------------------------------------- the bbox


def test_extract_bbox_is_in_degrees_and_wider_than_the_city() -> None:
    """Padded in metres, then reprojected -- never the other way round.

    A margin in degrees is a different distance at every latitude, and at 37 N
    a degree of longitude is about 20% shorter than a degree of latitude. The
    projected CRS is the one where a metre is a metre.
    """
    tight = extract_bbox(CITY, margin_m=0.0)
    padded = extract_bbox(CITY, margin_m=2000.0)

    # Degrees, and Montalban is in western Andalusia.
    assert -5.0 < tight[0] < -4.0
    assert 37.0 < tight[1] < 38.0
    assert padded[0] < tight[0] and padded[1] < tight[1]
    assert padded[2] > tight[2] and padded[3] > tight[3]


def test_extract_bbox_margin_is_metres_not_degrees() -> None:
    """2 km at this latitude is a couple of hundredths of a degree, not two."""
    tight = extract_bbox(CITY, margin_m=0.0)
    padded = extract_bbox(CITY, margin_m=2000.0)

    # ~2 km of latitude is 0.018 deg; longitude is wider per metre at 37 N.
    assert 0.015 < padded[3] - tight[3] < 0.025
    assert 0.015 < tight[0] - padded[0] < 0.030


# ---------------------------------------------------------------- the extract


@pytest.fixture
def fake_pmtiles(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the argv and write a plausible archive, without the Go binary."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        Path(argv[3]).write_bytes(b"PMTiles" * 10)
        return subprocess.CompletedProcess(
            argv, 0, stdout="Extract required 30 requests.\n", stderr=""
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    return calls


def test_build_basemap_writes_the_file_and_the_manifest_key(
    fake_pmtiles: list[list[str]], tmp_path: Path
) -> None:
    tiles_dir = tmp_path / TILES_DIRNAME
    tiles_dir.mkdir()
    (tiles_dir / MANIFEST_FILENAME).write_text(json.dumps({"city": "montalban"}), encoding="utf-8")

    out = build_basemap(CITY, tmp_path, stamp="20260817")

    assert out == tiles_dir / BASEMAP_FILENAME
    assert out.exists()
    manifest = json.loads((tiles_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["basemap_url"] == BASEMAP_FILENAME


def test_build_basemap_asks_for_the_build_it_was_given(
    fake_pmtiles: list[list[str]], tmp_path: Path
) -> None:
    build_basemap(CITY, tmp_path, stamp="20260814", margin_m=2000.0)

    argv = fake_pmtiles[0]
    assert argv[:2] == ["pmtiles", "extract"]
    assert argv[2] == BUILD_URL.format(stamp="20260814")
    assert argv[3].endswith(BASEMAP_FILENAME + PARTIAL_SUFFIX)
    west, south, east, north = (float(part) for part in argv[4].removeprefix("--bbox=").split(","))
    assert west < east and south < north


def test_a_failed_extract_leaves_no_basemap_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Existing has to mean complete: everything downstream only checks presence.

    ``check_ready``, the manifest and the viewer all treat the file being there
    as the answer and never look inside it. A half-downloaded archive left
    behind by an interrupted extract would pass all three and then 404 tile by
    tile in the browser.
    """

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(argv[3]).write_bytes(b"half an archive")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="connection reset\n")

    monkeypatch.setattr("subprocess.run", fake_run)
    tiles_dir = tmp_path / TILES_DIRNAME

    with pytest.raises(BasemapError, match="exit 1"):
        build_basemap(CITY, tmp_path, stamp="20260817")

    assert not (tiles_dir / BASEMAP_FILENAME).exists()
    assert not (tiles_dir / (BASEMAP_FILENAME + PARTIAL_SUFFIX)).exists()


def test_a_missing_pmtiles_binary_says_which_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(BasemapError, match="go-pmtiles"):
        build_basemap(CITY, tmp_path, stamp="20260817")


def test_the_progress_bar_does_not_reach_the_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``pmtiles extract`` redraws one line with carriage returns.

    In a terminal that is a progress bar; captured into a log it is a single
    line a hundred kilobytes long, which is what happened to rsync's
    ``--info=progress2`` before it was taken out of publish.
    """
    noisy = "fetching 7 dirs\r  50% |####| (340/681 kB)\r 100% |########|\nCompleted in 8.8s\n"

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(argv[3]).write_bytes(b"PMTiles")
        return subprocess.CompletedProcess(argv, 0, stdout=noisy, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    said: list[str] = []

    build_basemap(CITY, tmp_path, stamp="20260817", progress=said.append)

    assert any("Completed in 8.8s" in line for line in said)
    assert not any("|" in line and "%" in line for line in said)


# ---------------------------------------------------------------- the manifest


def _manifest(tiles_dir: Path, **extra: Any) -> Path:
    tiles_dir.mkdir(parents=True, exist_ok=True)
    path = tiles_dir / MANIFEST_FILENAME
    path.write_text(json.dumps({"city": "montalban", **extra}), encoding="utf-8")
    return path


def test_declare_in_manifest_adds_the_key_when_the_file_is_there(tmp_path: Path) -> None:
    """Needed because the real order is not always the chain's.

    Montalban had its pyramid rendered and its basemap missing; re-rendering
    fourteen minutes of tiles to write one key would be absurd.
    """
    path = _manifest(tmp_path)
    (tmp_path / BASEMAP_FILENAME).write_bytes(b"PMTiles")

    assert declare_in_manifest(tmp_path) is True
    assert json.loads(path.read_text(encoding="utf-8"))["basemap_url"] == BASEMAP_FILENAME


def test_declare_in_manifest_removes_a_key_that_promises_nothing(tmp_path: Path) -> None:
    """The client believes the key: given it, a 404 source and a black map."""
    path = _manifest(tmp_path, basemap_url=BASEMAP_FILENAME)

    assert declare_in_manifest(tmp_path) is True
    assert "basemap_url" not in json.loads(path.read_text(encoding="utf-8"))


def test_declare_in_manifest_is_idempotent(tmp_path: Path) -> None:
    _manifest(tmp_path)
    (tmp_path / BASEMAP_FILENAME).write_bytes(b"PMTiles")

    assert declare_in_manifest(tmp_path) is True
    assert declare_in_manifest(tmp_path) is False


def test_declare_in_manifest_says_nothing_without_a_manifest(tmp_path: Path) -> None:
    assert declare_in_manifest(tmp_path) is False


# ------------------------------------------------------------------- the assets


def _assets_tarball() -> bytes:
    """A tarball shaped like protomaps/basemaps-assets, with one file each."""
    import io
    import tarfile

    buffer = io.BytesIO()
    members = [f"fonts/{stack}/0-255.pbf" for stack in FONT_STACKS]
    members += [f"{SPRITE_SET}/light.json", f"{SPRITE_SET}/black.json"]
    # The bulk of the real archive, and nothing references it.
    members += ["fonts/Noto Sans Devanagari Regular v1/0-255.pbf", "README.md"]
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in members:
            info = tarfile.TarInfo(f"basemaps-assets-main/{name}")
            info.size = len(name)
            archive.addfile(info, io.BytesIO(name.encode("utf-8")))
    return buffer.getvalue()


@pytest.fixture
def offline_assets(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve the fake tarball instead of GitHub; records each download."""
    downloads: list[str] = []

    class FakeResponse:
        content = _assets_tarball()

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, **_kwargs: Any) -> FakeResponse:
        downloads.append(url)
        return FakeResponse()

    monkeypatch.setattr("httpx.get", fake_get)
    return downloads


def test_ensure_assets_takes_the_stacks_the_style_asks_for(
    offline_assets: list[str], tmp_path: Path
) -> None:
    """And not the dozen it does not: Devanagari is most of the download."""
    target = ensure_assets(tmp_path)

    assert sorted(path.name for path in (target / "fonts").iterdir()) == sorted(FONT_STACKS)
    assert (target / SPRITE_SET / "light.json").exists()
    assert (target / SPRITE_SET / "black.json").exists()
    assert not (target / "README.md").exists()


def test_ensure_assets_does_nothing_the_second_time(
    offline_assets: list[str], tmp_path: Path
) -> None:
    ensure_assets(tmp_path)
    ensure_assets(tmp_path)

    assert len(offline_assets) == 1


def test_ensure_assets_redownloads_when_forced(offline_assets: list[str], tmp_path: Path) -> None:
    ensure_assets(tmp_path)
    ensure_assets(tmp_path, force=True)

    assert len(offline_assets) == 2


def test_ensure_assets_refuses_an_archive_it_does_not_recognise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Silence would be worse: a map with no labels and no reason given."""
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("basemaps-assets-main/README.md")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"hi"))

    class FakeResponse:
        content = buffer.getvalue()

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("httpx.get", lambda _url, **_k: FakeResponse())

    with pytest.raises(BasemapError, match="layout has changed"):
        ensure_assets(tmp_path)
    assert not (tmp_path / "assets").exists()
