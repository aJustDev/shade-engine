"""Automated PNOA LiDAR downloads from the CNIG download center.

The download center (https://centrodedescargas.cnig.es) publishes no API;
its viewer calls internal endpoints that worked without session, cookies or
captcha when verified on 2026-07-11:

- ``GET archivosSerie?numPagina=N&codSerie=...&coordenadas=<GeoJSON 4326>``
  returns an HTML fragment listing files (name + a ``linkDescDir_<sec>``
  download id) 20 per page, with the total in an ``id="totalArchivos"``
  input. The ``sec`` id is not derivable from the file name.
- ``POST descargaDir`` with form field ``secDescDirLA=<sec>`` streams the
  LAZ. GET is rejected with 403.

Those endpoints have no stability contract, so this driver is built to
break loudly: any parse or validation failure raises :class:`CnigError`
with manual-download instructions. Downloads are resumable -- every tile
validated into the cache directory survives interruptions (the center
documents a ~20 downloads/session limit for anonymous users) and rerunning
the same command picks up where it left off. After downloading, file
selection and coverage validation are delegated to the already-tested
:class:`~shade_pipeline.sources.LocalDirectory` over the cache directory.

Tile naming encodes the 1x1 km (third coverage) or 2x2 km (first/second)
tile's NW corner in km of the local UTM zone: ``PNOA-2024-AND-343-4195-H30-
NPC01.laz`` covers easting 343-344 km, northing 4194-4195 km. The catalog
lists names with hyphens while the download's Content-Disposition uses
underscores; the catalog spelling is canonical here.
"""

import json
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx
from pyproj import Transformer

from shade_core.config import Bbox
from shade_pipeline.sources import CoverageError, LocalDirectory

SEARCH_URL: Final = "https://centrodedescargas.cnig.es/CentroDescargas/archivosSerie"
DOWNLOAD_URL: Final = "https://centrodedescargas.cnig.es/CentroDescargas/descargaDir"
PAGE_SIZE: Final = 20
LAZ_MAGIC: Final = b"LASF"

# Tile side in km per catalog series (LIDAR = 1st coverage, LIDA2 = 2nd, LIDA3 = 3rd).
TILE_KM: Final = {"LIDAR": 2, "LIDA2": 2, "LIDA3": 1}

_MANUAL_FALLBACK = (
    "as a fallback, download the tiles by hand from "
    "https://centrodedescargas.cnig.es (LiDAR series) into the cache "
    "directory and rerun, or pass --lidar-dir with a directory of tiles"
)

# NW corner in km: a 3-digit easting and a 4-digit northing between separators.
_TILE_NAME_RE = re.compile(r"[-_](\d{3})[-_](\d{4})[-_]")
_CATALOG_NAME_RE = re.compile(r"PNOA[-_][\w.-]+?\.laz", re.IGNORECASE)
_CATALOG_SEC_RE = re.compile(r'id="linkDescDir_(\d+)"')
_CATALOG_TOTAL_RE = re.compile(r'id="totalArchivos"[^>]*value="(\d+)"')


class CnigError(RuntimeError):
    """Talking to the CNIG download center failed; the message says how to recover."""


def expected_tiles(bbox: Bbox, buffer_m: float, tile_km: int = 1) -> set[tuple[int, int]]:
    """(easting_km, northing_km) NW-corner keys of the tiles covering the padded bbox.

    The upper/right edge is half open: a bbox edge exactly on a km multiple
    does not pull in the next tile. ``LocalDirectory``'s 1 m coverage
    tolerance absorbs that boundary, matching how LAS header extents bound
    points rather than nominal tiles.
    """
    size = tile_km * 1000.0
    min_x, min_y, max_x, max_y = bbox
    e_range = range(math.floor((min_x - buffer_m) / size), math.ceil((max_x + buffer_m) / size))
    n_range = range(math.floor((min_y - buffer_m) / size), math.ceil((max_y + buffer_m) / size))
    return {(e * tile_km, (n + 1) * tile_km) for e in e_range for n in n_range}


def parse_tile_name(filename: str) -> tuple[int, int] | None:
    """Extract the (easting_km, northing_km) key from a PNOA tile name; None if absent."""
    match = _TILE_NAME_RE.search(filename)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_catalog_page(html: str) -> tuple[list[tuple[str, str]], int]:
    """(filename, sec) pairs and the catalog total from one ``archivosSerie`` page.

    Each download id is paired with the closest preceding file name, which
    tolerates the name appearing more than once per row (text, title
    attribute). Raises :class:`CnigError` when the fragment does not look
    like the catalog at all -- the signal that the internal endpoint changed.
    """
    total_match = _CATALOG_TOTAL_RE.search(html)
    if total_match is None:
        raise CnigError(
            "could not parse the CNIG catalog page (no totalArchivos field); "
            "the download center's internal endpoints may have changed; " + _MANUAL_FALLBACK
        )
    names = [(m.start(), m.group(0)) for m in _CATALOG_NAME_RE.finditer(html)]
    entries: list[tuple[str, str]] = []
    for sec_match in _CATALOG_SEC_RE.finditer(html):
        preceding = [name for pos, name in names if pos < sec_match.start()]
        if not preceding:
            raise CnigError(
                "could not pair a CNIG download link with a file name; "
                "the catalog page layout may have changed; " + _MANUAL_FALLBACK
            )
        entries.append((preceding[-1], sec_match.group(1)))
    return entries, int(total_match.group(1))


def bbox_polygon_wgs84(bbox: Bbox, crs: str) -> str:
    """The bbox corners as a GeoJSON FeatureCollection in EPSG:4326 (lon/lat order).

    GeoJSON mandates lon/lat; the transformer is built with ``always_xy``
    so the projected (x, y) input maps straight onto it. Corners only, no
    edge densification: at the few-km scale of a city bbox the curvature of
    a straight UTM edge in lat/lon is far below the tile grid.
    """
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    min_x, min_y, max_x, max_y = bbox
    corners = [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y), (min_x, min_y)]
    ring = []
    for x, y in corners:
        lon, lat = to_wgs84.transform(x, y)
        ring.append([round(lon, 6), round(lat, 6)])
    geometry = {"type": "Polygon", "coordinates": [ring]}
    feature = {"type": "Feature", "properties": {}, "geometry": geometry}
    return json.dumps({"type": "FeatureCollection", "features": [feature]}, separators=(",", ":"))


def _is_valid_laz(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == LAZ_MAGIC
    except OSError:
        return False


class CnigSource:
    """:class:`~shade_pipeline.sources.LidarSource` that downloads PNOA tiles on demand.

    ``client`` is injectable for tests (never closed here when injected);
    ``throttle_s`` spaces downloads out of courtesy to a free public
    service; ``progress`` receives one human-readable line per download.
    """

    def __init__(
        self,
        cache_dir: Path,
        crs: str,
        *,
        cod_serie: str = "LIDA3",
        client: httpx.Client | None = None,
        throttle_s: float = 1.0,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.crs = crs
        self.cod_serie = cod_serie
        self.tile_km = TILE_KM.get(cod_serie, 1)
        self.throttle_s = throttle_s
        self._client = client
        self._progress = progress if progress is not None else lambda _message: None

    def files_covering(self, bbox: Bbox, buffer_m: float) -> list[Path]:
        """Ensure the padded bbox's tiles are cached, then select and validate them."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        missing = self._missing_tiles(bbox, buffer_m)
        if missing:
            if self._client is not None:
                self._download(self._client, bbox, buffer_m, missing)
            else:
                timeout = httpx.Timeout(30.0, read=300.0)
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    self._download(client, bbox, buffer_m, missing)
        return LocalDirectory(self.cache_dir).files_covering(bbox, buffer_m)

    def _missing_tiles(self, bbox: Bbox, buffer_m: float) -> set[tuple[int, int]]:
        """Expected tile keys minus those already cached as valid LAZ.

        Cached files that match an expected tile but fail validation are
        deleted here so the download pass replaces them.
        """
        cached: set[tuple[int, int]] = set()
        expected = expected_tiles(bbox, buffer_m, self.tile_km)
        for path in [*self.cache_dir.glob("*.laz"), *self.cache_dir.glob("*.las")]:
            key = parse_tile_name(path.name)
            if key is None or key not in expected:
                continue
            if path.stat().st_size > 0 and _is_valid_laz(path):
                cached.add(key)
            else:
                path.unlink()
        return expected - cached

    def _download(
        self, client: httpx.Client, bbox: Bbox, buffer_m: float, missing: set[tuple[int, int]]
    ) -> None:
        found = self._catalog_lookup(client, bbox, buffer_m, missing)
        absent = missing - found.keys()
        if absent:
            patterns = ", ".join(f"PNOA-*-{e}-{n}-*.laz" for e, n in sorted(absent))
            raise CoverageError(
                f"the CNIG catalog (serie {self.cod_serie}) lists no tiles matching "
                f"{patterns} for bbox {bbox} plus a {buffer_m} m buffer; " + _MANUAL_FALLBACK
            )
        ordered = [found[key] for key in sorted(found)]
        for index, (name, sec) in enumerate(ordered, start=1):
            self._progress(f"[{index}/{len(ordered)}] {name}")
            self._download_one(client, name, sec)
            if self.throttle_s and index < len(ordered):
                time.sleep(self.throttle_s)

    def _catalog_lookup(
        self, client: httpx.Client, bbox: Bbox, buffer_m: float, missing: set[tuple[int, int]]
    ) -> dict[tuple[int, int], tuple[str, str]]:
        """Map missing tile keys to (filename, sec) by paging through the catalog."""
        min_x, min_y, max_x, max_y = bbox
        padded = (min_x - buffer_m, min_y - buffer_m, max_x + buffer_m, max_y + buffer_m)
        coordinates = bbox_polygon_wgs84(padded, self.crs)
        found: dict[tuple[int, int], tuple[str, str]] = {}
        page = 1
        while True:
            params: dict[str, str | int] = {
                "numPagina": page,
                "codSerie": self.cod_serie,
                "coordenadas": coordinates,
            }
            try:
                response = client.get(SEARCH_URL, params=params)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise CnigError(
                    f"CNIG catalog search failed ({exc}); rerun to retry; " + _MANUAL_FALLBACK
                ) from exc
            entries, total = parse_catalog_page(response.text)
            for name, sec in entries:
                key = parse_tile_name(name)
                if key in missing and key not in found:
                    found[key] = (name, sec)
            if len(found) == len(missing) or page * PAGE_SIZE >= total:
                return found
            page += 1

    def _download_one(self, client: httpx.Client, name: str, sec: str) -> None:
        dest = self.cache_dir / name
        part = dest.with_name(dest.name + ".part")
        try:
            with (
                client.stream("POST", DOWNLOAD_URL, data={"secDescDirLA": sec}) as response,
                part.open("wb") as fh,
            ):
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    fh.write(chunk)
        except httpx.HTTPError as exc:
            part.unlink(missing_ok=True)
            raise CnigError(
                f"downloading {name} failed ({exc}); tiles already validated under "
                f"{self.cache_dir} are kept and rerunning resumes from them; " + _MANUAL_FALLBACK
            ) from exc
        if not _is_valid_laz(part):
            part.unlink(missing_ok=True)
            raise CnigError(
                f"CNIG returned something that is not a LAZ for {name} (anonymous "
                f"session download limit?); tiles already validated under "
                f"{self.cache_dir} are kept and rerunning resumes from them; " + _MANUAL_FALLBACK
            )
        part.replace(dest)


__all__ = [
    "DOWNLOAD_URL",
    "PAGE_SIZE",
    "SEARCH_URL",
    "TILE_KM",
    "CnigError",
    "CnigSource",
    "bbox_polygon_wgs84",
    "expected_tiles",
    "parse_catalog_page",
    "parse_tile_name",
]
