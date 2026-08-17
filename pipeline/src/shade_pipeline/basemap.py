"""The backdrop the shade overlay is drawn on: streets, labels, buildings.

The shade tiles are a *transparent overlay*. They carry one field per pixel --
sun, shade from a building, shade from a crown -- and nothing else: no street,
no name, no outline. Everything a person uses to recognise where they are comes
from underneath, and underneath is a vector basemap: an extract of OpenStreetMap
in PMTiles form, styled in the browser.

A vector style needs three things, and all three have to be on disk or the map
is not merely plainer, it is unreadable:

- **the tiles** (``basemap.pmtiles``), an extract of the Protomaps planet build
  clipped to this city's bbox. Vector, so the client tints it to match the site
  theme without anything being re-rendered here.
- **the glyphs**, one PBF per font stack and Unicode range. Without them there
  are no labels at all -- MapLibre cannot draw text it has no glyphs for.
- **the sprites**, one sheet per flavor. Without them the fill patterns the
  style references (``park``, ``forest``) are missing and the console fills with
  "Image park could not be loaded".

The glyphs and sprites are the same bytes for every city, so they live once at
``data/cities/assets/`` and are served as though "assets" were a city of its own.

Until this module existed, all of it was a block of shell in a runbook, done by
hand and remembered by one person. Montalban was published without it, and what
you saw was the overlay floating on black -- which at low zoom reads as a haze
over nothing. That is the whole reason this is a step and not a note.

shade-docs: learning/basemap-glyphs-sprites.md
"""

import io
import json
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import httpx

from shade_core.config import CityConfig
from shade_pipeline.tiles import (
    BASEMAP_FILENAME,
    MANIFEST_FILENAME,
    PARTIAL_SUFFIX,
    TILES_DIRNAME,
    bounds_wgs84,
)

BUILD_URL = "https://build.protomaps.com/{stamp}.pmtiles"
"""Where the daily planet build lives. 137 GB, and read with Range requests."""

BUILD_LOOKBACK = 10
"""How many days back to look for a build before giving up.

There is no "latest" URL, only dated ones, and **the retention is about a
week**: the build Cordoba was cut from (20260712) is already a 404. A constant
here would have been a bug with an expiry date, so the date is discovered.
"""

ASSETS_URL = "https://github.com/protomaps/basemaps-assets/archive/refs/heads/main.tar.gz"
ASSETS_DIRNAME = "assets"
FONT_STACKS: tuple[str, ...] = ("Noto Sans Regular", "Noto Sans Medium", "Noto Sans Italic")
"""The stacks the style actually asks for, out of the dozen in the repository.

The rest (Devanagari and friends) are the bulk of the download and nothing
references them; taking only these keeps the directory at 14 MB and matches, byte
for byte, what has been on the server since Cordoba.
"""
SPRITE_SET = "sprites/v4"
"""Both flavors of it: the sprite follows the theme and they differ in content.

``black`` carries 18 icons and ``light`` 53, including the patterns the light
style references. Shipping one of them is how parks lose their texture.
"""

DEFAULT_MARGIN_M = 2000.0
"""How far past the city's bbox the extract reaches.

The bbox is where shade is *computed*, not where somebody looks. Clipped to it
exactly, the map turns black the moment you pan a street beyond the edge, and
the edge itself is a hard line across the screen. Two kilometres of rural vector
tiles cost a few tens of kilobytes.
"""

_PROGRESS_BAR = re.compile(r"\d+% \|")
"""``pmtiles extract`` draws its progress with carriage returns.

In a terminal that is one line being redrawn; captured into a log it is one
line a hundred kilobytes long. The four timestamped lines around it are the
ones worth keeping.
"""


class BasemapError(RuntimeError):
    """The basemap or its assets could not be produced."""


def newest_build(
    today: date, *, back: int = BUILD_LOOKBACK, probe: Callable[[str], bool] | None = None
) -> str:
    """The most recent Protomaps planet build that still exists, as ``YYYYMMDD``.

    Walks backwards from ``today`` asking for a single byte of each candidate --
    a Range request, so the answer costs a header and not 137 GB. The first one
    that answers is the newest, because they are published in order.
    """
    ask = probe if probe is not None else _build_exists
    for offset in range(back):
        stamp = (today - timedelta(days=offset)).strftime("%Y%m%d")
        if ask(stamp):
            return stamp
    raise BasemapError(
        f"no Protomaps planet build in the {back} days up to {today.isoformat()}; "
        f"builds live at {BUILD_URL.format(stamp='YYYYMMDD')} and are kept about a week"
    )


def _build_exists(stamp: str) -> bool:
    """Whether ``stamp``'s build is there, asked with a one-byte Range request."""
    try:
        response = httpx.get(
            BUILD_URL.format(stamp=stamp),
            headers={"Range": "bytes=0-0"},
            timeout=20.0,
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return False
    return response.status_code in (200, 206)


def extract_bbox(config: CityConfig, margin_m: float = DEFAULT_MARGIN_M) -> tuple[float, ...]:
    """The city's bbox, padded by ``margin_m``, in WGS84 degrees.

    Padded in *metres* and only then reprojected, which is the whole point of
    doing it in this order: a margin in degrees is a different distance at every
    latitude, and at 37 N a degree of longitude is 20% shorter than a degree of
    latitude. The projected CRS (EPSG:25830 here) is where a metre is a metre.

    ``bounds_wgs84`` transforms all four corners and takes the envelope, because
    a projected rectangle is not a rectangle in lon/lat -- its edges curve, and
    transforming two opposite corners clips the extent.

    shade-docs: learning/crs.md
    """
    min_x, min_y, max_x, max_y = config.bbox
    padded = (min_x - margin_m, min_y - margin_m, max_x + margin_m, max_y + margin_m)
    return bounds_wgs84(config.crs, padded)


def build_basemap(
    config: CityConfig,
    artifact_dir: Path,
    *,
    margin_m: float = DEFAULT_MARGIN_M,
    stamp: str | None = None,
    today: date | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Cut this city out of the planet build and write ``tiles/basemap.pmtiles``.

    ``pmtiles extract`` reads the remote archive over HTTP Range: it fetches the
    directory, works out which tiles fall in the bbox, and downloads only those.
    A town is a few hundred kilobytes and under a minute against a 137 GB file.

    Written to a ``.partial`` and renamed, the same discipline the tile renderer
    uses: *existing* has to mean *complete*, because everything downstream --
    ``check_ready``, the manifest, the viewer -- treats the file's presence as
    the answer and never looks inside it.
    """
    echo = progress if progress is not None else lambda _message: None
    tiles_dir = artifact_dir / TILES_DIRNAME
    tiles_dir.mkdir(parents=True, exist_ok=True)
    target = tiles_dir / BASEMAP_FILENAME
    partial = target.with_name(target.name + PARTIAL_SUFFIX)
    partial.unlink(missing_ok=True)

    build = stamp if stamp is not None else newest_build(today or date.today())
    west, south, east, north = extract_bbox(config, margin_m)
    argv = [
        "pmtiles",
        "extract",
        BUILD_URL.format(stamp=build),
        str(partial),
        f"--bbox={west:.5f},{south:.5f},{east:.5f},{north:.5f}",
    ]
    echo(f"cutting {config.id} out of the {build} planet build")
    echo(f"  bbox {west:.5f},{south:.5f},{east:.5f},{north:.5f} ({margin_m:g} m margin)")
    echo("  " + " ".join(argv))
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        raise BasemapError(
            "pmtiles is not on PATH; it is the Go CLI from "
            "github.com/protomaps/go-pmtiles and this is the only step that needs it"
        ) from error
    for line in re.split(r"[\r\n]+", result.stdout + result.stderr):
        line = line.strip()
        if line and not _PROGRESS_BAR.search(line):
            echo("  " + line)
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        raise BasemapError(f"pmtiles extract failed with exit {result.returncode}")

    partial.replace(target)
    echo(f"{target} ({target.stat().st_size / 1e6:.1f} MB)")
    declare_in_manifest(tiles_dir)
    return target


def ensure_assets(
    output_root: Path,
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Put the glyphs and sprites under ``<output_root>/assets/``, once per machine.

    Not per city: these are the same bytes everywhere, which is why they sit
    beside the cities rather than inside one. The server has had them since
    Cordoba; a fresh working copy has not, and without them a perfectly good
    basemap draws with no labels and no fill patterns.
    """
    echo = progress if progress is not None else lambda _message: None
    target = output_root / ASSETS_DIRNAME
    if (target / SPRITE_SET).is_dir() and not force:
        echo(
            f"{target} is already there ({len(FONT_STACKS)} font stacks, sprites); --force to redo"
        )
        return target

    echo(f"downloading {ASSETS_URL}")
    try:
        response = httpx.get(ASSETS_URL, timeout=120.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise BasemapError(f"could not download the basemap assets: {error}") from error

    wanted = (*(f"fonts/{stack}/" for stack in FONT_STACKS), f"{SPRITE_SET}/")
    staging = target.with_name(target.name + PARTIAL_SUFFIX)
    shutil.rmtree(staging, ignore_errors=True)
    written = 0
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        for member in archive:
            # The tarball is one directory deep (basemaps-assets-main/...), and
            # only a fraction of it is referenced by the style.
            _, _, relative = member.name.partition("/")
            if not member.isfile() or not relative.startswith(wanted):
                continue
            source = archive.extractfile(member)
            if source is None:  # pragma: no cover - isfile() already said otherwise
                continue
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source, destination.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            written += 1
    if not written:
        shutil.rmtree(staging, ignore_errors=True)
        raise BasemapError(
            f"the assets archive held none of {', '.join(wanted)}; its layout has changed"
        )

    shutil.rmtree(target, ignore_errors=True)
    staging.replace(target)
    echo(f"{target}: {written} files")
    return target


def declare_in_manifest(tiles_dir: Path) -> bool:
    """Make ``index.json`` agree with what is on disk; returns whether it changed.

    ``basemap_url`` is a promise to the client, and the client believes it: given
    the key it declares a PMTiles source, and given a source that 404s it draws
    black rather than falling back. So the key is present exactly when the file
    is.

    Separate from the tile render because the real order is not always the
    chain's. Montalban had its pyramid rendered and its basemap missing, and
    re-rendering fourteen minutes of tiles to write one key would be absurd.
    """
    manifest_path = tiles_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    present = (tiles_dir / BASEMAP_FILENAME).exists()
    if present == ("basemap_url" in manifest):
        return False
    if present:
        manifest["basemap_url"] = BASEMAP_FILENAME
    else:
        del manifest["basemap_url"]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return True
