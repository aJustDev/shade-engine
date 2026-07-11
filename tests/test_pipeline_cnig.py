"""CNIG driver: tile math, catalog parsing, resumable downloads. No network."""

from pathlib import Path
from urllib.parse import parse_qs

import httpx
import numpy as np
import pytest

import laz_fixture
from shade_pipeline.cnig import (
    CnigError,
    CnigSource,
    bbox_polygon_wgs84,
    expected_tiles,
    parse_tile_name,
)
from shade_pipeline.rasterize import LIDAR_CLASS_GROUND
from shade_pipeline.sources import CoverageError

# Padded by BUFFER this spans exactly 2x2 tiles of 1 km:
# E {341, 342} x N {4194, 4195} (NW-corner naming).
BBOX = (342200.0, 4194200.0, 342700.0, 4194700.0)
BUFFER = 300.0
TILES = {(341, 4194), (341, 4195), (342, 4194), (342, 4195)}


def _tile_name(e: int, n: int) -> str:
    return f"PNOA-2024-AND-{e}-{n}-H30-NPC01.laz"


def _tile_bytes(tmp_path: Path, e: int, n: int) -> bytes:
    """A real (tiny) LAZ whose header extent is the tile's 1 km square."""
    path = tmp_path / f"src-{e}-{n}.laz"
    x0, y0 = e * 1000.0, (n - 1) * 1000.0
    laz_fixture.write_laz(
        path,
        np.array([x0, x0 + 1000.0, x0, x0 + 1000.0]),
        np.array([y0, y0, y0 + 1000.0, y0 + 1000.0]),
        np.zeros(4),
        np.full(4, LIDAR_CLASS_GROUND, dtype=np.uint8),
    )
    return path.read_bytes()


def _catalog_html(entries: list[tuple[str, str]], total: int) -> str:
    rows = "".join(
        f'<tr><td title="{name}">{name}</td><td>77 MB</td>'
        f'<td><a id="linkDescDir_{sec}" href="./detalleArchivo?sec={sec}">x</a></td></tr>'
        for name, sec in entries
    )
    return f'<input type="hidden" id="totalArchivos" value="{total}"/><table>{rows}</table>'


class FakeCnig:
    """MockTransport handler mimicking the two verified CNIG endpoints."""

    def __init__(self, catalog: list[str], files: dict[str, bytes], page_size: int = 20) -> None:
        self.catalog = catalog
        self.files = files
        self.page_size = page_size
        self.secs = {name: str(1000 + i) for i, name in enumerate(catalog)}
        self.pages_requested: list[int] = []
        self.downloads: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("archivosSerie"):
            page = int(request.url.params["numPagina"])
            self.pages_requested.append(page)
            window = self.catalog[(page - 1) * self.page_size : page * self.page_size]
            entries = [(name, self.secs[name]) for name in window]
            return httpx.Response(200, text=_catalog_html(entries, len(self.catalog)))
        sec = parse_qs(request.content.decode())["secDescDirLA"][0]
        name = next(n for n, s in self.secs.items() if s == sec)
        self.downloads.append(name)
        return httpx.Response(200, content=self.files[name])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _source(fake: FakeCnig, cache_dir: Path) -> CnigSource:
    return CnigSource(cache_dir, "EPSG:25830", client=fake.client(), throttle_s=0.0)


def _full_fake(tmp_path: Path, extra_catalog: list[str] | None = None) -> FakeCnig:
    files = {_tile_name(e, n): _tile_bytes(tmp_path, e, n) for e, n in TILES}
    catalog = (extra_catalog or []) + sorted(files)
    return FakeCnig(catalog, files)


def test_expected_tiles_mapping() -> None:
    assert expected_tiles(BBOX, BUFFER) == TILES


def test_expected_tiles_exact_km_edges_are_half_open() -> None:
    assert expected_tiles((342000.0, 4194000.0, 343000.0, 4195000.0), 0.0) == {(342, 4195)}


def test_expected_tiles_2km_series() -> None:
    assert expected_tiles((342100.0, 4194100.0, 343900.0, 4195900.0), 0.0, tile_km=2) == {
        (342, 4196)
    }


def test_parse_tile_name_hyphens_and_underscores() -> None:
    assert parse_tile_name("PNOA-2024-AND-343-4195-H30-NPC01.laz") == (343, 4195)
    assert parse_tile_name("PNOA_2020_AND-C_342-4196_ORT-CLA-IRC.laz") == (342, 4196)
    assert parse_tile_name("readme.txt") is None


def test_bbox_polygon_wgs84_lonlat_order() -> None:
    import json

    geojson = json.loads(bbox_polygon_wgs84(BBOX, "EPSG:25830"))
    lon, lat = geojson["features"][0]["geometry"]["coordinates"][0][0]
    assert -5.0 < lon < -4.5  # Cordoba is at ~4.8 W...
    assert 37.5 < lat < 38.0  # ...and ~37.9 N; swapped axes would fail both


def test_downloads_and_selects_tiles(tmp_path: Path) -> None:
    fake = _full_fake(tmp_path)
    cache = tmp_path / "cache"
    files = _source(fake, cache).files_covering(BBOX, BUFFER)
    assert len(files) == 4
    assert sorted(fake.downloads) == sorted(_tile_name(e, n) for e, n in TILES)
    for path in files:
        assert path.parent == cache
        assert path.read_bytes()[:4] == b"LASF"


def test_pagination_reaches_later_pages(tmp_path: Path) -> None:
    # 22 decoy tiles far away fill page 1; the needed tiles sit on page 2.
    decoys = [_tile_name(600, 4600 + i) for i in range(22)]
    fake = _full_fake(tmp_path, extra_catalog=decoys)
    _source(fake, tmp_path / "cache").files_covering(BBOX, BUFFER)
    assert fake.pages_requested == [1, 2]


def test_cached_valid_file_not_redownloaded(tmp_path: Path) -> None:
    fake = _full_fake(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    kept = _tile_name(341, 4194)
    (cache / kept).write_bytes(fake.files[kept])
    files = _source(fake, cache).files_covering(BBOX, BUFFER)
    assert len(files) == 4
    assert kept not in fake.downloads
    assert len(fake.downloads) == 3


def test_corrupt_cached_file_redownloaded(tmp_path: Path) -> None:
    fake = _full_fake(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    corrupt = _tile_name(341, 4194)
    (cache / corrupt).write_bytes(b"<html>not a laz</html>")
    _source(fake, cache).files_covering(BBOX, BUFFER)
    assert corrupt in fake.downloads
    assert (cache / corrupt).read_bytes()[:4] == b"LASF"


def test_catalog_parse_failure_is_instructive(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = CnigSource(tmp_path / "cache", "EPSG:25830", client=client, throttle_s=0.0)
    with pytest.raises(CnigError, match="--lidar-dir"):
        source.files_covering(BBOX, BUFFER)


def test_download_not_lasf_fails_keeping_cache(tmp_path: Path) -> None:
    fake = _full_fake(tmp_path)
    # Downloads run in sorted tile-key order; poison the second one.
    poisoned = _tile_name(341, 4195)
    fake.files[poisoned] = b"<html>download limit reached</html>"
    cache = tmp_path / "cache"
    with pytest.raises(CnigError, match="rerunning resumes"):
        _source(fake, cache).files_covering(BBOX, BUFFER)
    first = _tile_name(341, 4194)
    assert (cache / first).read_bytes()[:4] == b"LASF"  # completed work survives
    assert not list(cache.glob("*.part"))  # no half-written leftovers


def test_missing_tile_raises_coverage_error(tmp_path: Path) -> None:
    fake = _full_fake(tmp_path)
    absent = _tile_name(342, 4195)
    fake.catalog.remove(absent)
    del fake.files[absent]
    with pytest.raises(CoverageError, match=r"PNOA-\*-342-4195-\*\.laz"):
        _source(fake, tmp_path / "cache").files_covering(BBOX, BUFFER)
