"""S4, question 0c: the corner tie judged by the arbiter, in verdict.

Three readings of what a ray does when it crosses an exact cell corner, which
in the sweep happens only in sectors 8/24/40/56 and there happens at every
single step:

- ``one``    what the engine does today: emit one of the two orthogonal
             neighbours, whichever the last bit of sin/cos favours.
- ``both``   emit both, which is what "grazing a column's corner blocks" says
             if you apply it to both columns the ray grazes.
- ``none``   emit neither, keeping only the diagonal cells the ray crosses with
             non-zero thickness.
- ``mirror`` emit the OTHER one. Not a model: this is what a libm whose sin/cos
             rounded the other way would produce, so the gap between ``one`` and
             ``mirror`` is how much of this cube rests on a single bit.

The arbiter can judge this because it does NOT share the defect: it walks the
ray at the sun's real azimuth, which is never exactly 45 degrees, so it never
hits the degenerate case. Only the sweep does, because it samples the 64 sector
azimuths exactly.

Only four bands change, so the other 60 are copied from the S3 cube; the ``one``
variant is rebuilt the same way and must come out byte-identical to it, which is
the check that this bench reproduces the engine.
"""

import json
import math
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio

from shade_core.artifacts import load_metadata
from shade_core.raycast import ray_cells
from shade_core.shade import NO_BLOCKER, Landcover
from shade_core.solar import sun_position
from shade_pipeline.arbiter import shade_bracket
from shade_pipeline.cog import write_cog
from shade_pipeline.horizon import ANGLE_MAX_DEG, height_datum, quantize_angles
from shade_pipeline.shade_raster import STATE_SUN, compute_state_raster
from shade_pipeline.tiles import bounds_wgs84, season_preset_instants

DIAGONALS = (8, 24, 40, 56)
SECTORS = 64
MAX_DISTANCE_M = 500.0
OBSERVER_H = 1.6
BASE = Path("data/bench/montilla-test-s3/v1")

data = np.load("data/bench/montilla-test-padded.npz")
DSM, DTM, LANDCOVER = data["dsm"], data["dtm"], data["landcover"]
INNER = tuple(int(v) for v in data["inner"])
RES = float(data["resolution_m"][0])
ROW0, ROW1, COL0, COL1 = INNER
HEIGHT, WIDTH = ROW1 - ROW0, COL1 - COL0
ROWS, COLS = DSM.shape

DATUM = np.float32(height_datum(DTM))
OBSERVER = (DTM[ROW0:ROW1, COL0:COL1] - DATUM) + np.float32(OBSERVER_H)
SURFACE = DSM - DATUM
SURFACE_NOVEG = np.where(LANDCOVER == Landcover.VEGETATION, DTM, DSM) - DATUM


def corner_variant(cells: list[tuple[int, int, float, float]], mode: str) -> list:
    """The ray's cells under each reading of a corner crossing."""
    if mode == "one":
        return cells
    out = []
    index = 0
    while index < len(cells):
        cell = cells[index]
        nxt = cells[index + 1] if index + 1 < len(cells) else None
        if nxt is not None and math.isclose(cell[2], nxt[2], rel_tol=1e-9):
            previous = out[-1][:2] if out else (0, 0)
            mirror_delta = (nxt[0] - cell[0], nxt[1] - cell[1])
            mirror = (
                previous[0] + mirror_delta[0],
                previous[1] + mirror_delta[1],
                cell[2],
                cell[3],
            )
            if mode == "both":
                out.append(cell)
                out.append(mirror)
            elif mode == "mirror":
                out.append(mirror)
            # "none" drops the zero-thickness orthogonal entirely.
            out.append(nxt)
            index += 2
        else:
            out.append(cell)
            index += 1
    return out


def sweep_sector(cells: list) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(angles, blocker class, no-vegetation angles) for one sector."""
    best = np.full((HEIGHT, WIDTH), -np.inf, dtype=np.float32)
    best_class = np.full((HEIGHT, WIDTH), NO_BLOCKER, dtype=np.uint8)
    best_noveg = np.full((HEIGHT, WIDTH), -np.inf, dtype=np.float32)
    for d_row, d_col, entry, _ in cells:
        i_lo, i_hi = max(0, -(ROW0 + d_row)), min(HEIGHT, ROWS - ROW0 - d_row)
        j_lo, j_hi = max(0, -(COL0 + d_col)), min(WIDTH, COLS - COL0 - d_col)
        if i_lo >= i_hi or j_lo >= j_hi:
            continue
        sub = (slice(i_lo, i_hi), slice(j_lo, j_hi))
        src = (
            slice(ROW0 + i_lo + d_row, ROW0 + i_hi + d_row),
            slice(COL0 + j_lo + d_col, COL0 + j_hi + d_col),
        )
        distance = np.float32(entry)
        slope = (SURFACE[src] - OBSERVER[sub]) / distance
        improved = slope > best[sub]
        np.copyto(best[sub], slope, where=improved)
        np.copyto(best_class[sub], LANDCOVER[src], where=improved)
        np.maximum(
            best_noveg[sub], (SURFACE_NOVEG[src] - OBSERVER[sub]) / distance, out=best_noveg[sub]
        )
    best_class[best <= 0.0] = NO_BLOCKER
    return (
        np.degrees(np.arctan(best)).astype(np.float32),
        best_class,
        np.degrees(np.arctan(best_noveg)).astype(np.float32),
    )


def build(mode: str) -> Path:
    """A full cube with only the four diagonal bands recomputed."""
    out_dir = Path(f"data/bench/montilla-test-corner-{mode}/v1")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("canopy.tif", "metadata.json"):
        shutil.copy2(BASE / name, out_dir / name)

    planes = {}
    for sector in DIAGONALS:
        cells = corner_variant(ray_cells(sector * 360.0 / SECTORS, MAX_DISTANCE_M, RES), mode)
        angles, blocker, noveg = sweep_sector(cells)
        planes[sector] = (quantize_angles(angles), blocker, quantize_angles(noveg))
        print(f"  {mode}: sector {sector} swept over {len(cells)} cells", flush=True)

    changed = 0
    for index, name in enumerate(("horizon.tif", "blocker_class.tif", "horizon_noveg.tif")):
        with rasterio.open(BASE / name) as src:
            cube = src.read()
            profile_tags = src.tags()
            transform, crs = src.transform, src.crs
        for sector, trio in planes.items():
            changed += int((cube[sector] != trio[index]).sum())
            cube[sector] = trio[index]
        write_cog(out_dir / name, cube, transform, crs, tags=profile_tags)
    print(f"  {mode}: {changed:,} cells differ from the S3 cube", flush=True)
    return out_dir


DIRS = {mode: build(mode) for mode in ("one", "both", "none", "mirror")}

# The control: rebuilding "one" through this bench must reproduce the engine's
# own cube exactly, or none of the comparisons below mean anything.
for name in ("horizon.tif", "blocker_class.tif", "horizon_noveg.tif"):
    with rasterio.open(BASE / name) as a, rasterio.open(DIRS["one"] / name) as b:
        diff = int((a.read() != b.read()).sum())
    assert diff == 0, f"bench does not reproduce the engine: {diff} cells differ in {name}"
print("control passed: the 'one' rebuild is byte-identical to the S3 cube\n")

with rasterio.open("data/cities/montilla-test/v1/canopy.tif") as src:
    CANOPY = src.read()[0] != 0
OPEN = ~CANOPY

metadata = load_metadata(BASE)
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

wrong = {m: 0 for m in DIRS}
wrong_open = {m: 0 for m in DIRS}
shaded = {m: 0 for m in DIRS}
total = open_total = most_total = 0
rows = []

for when in season_preset_instants(ZoneInfo("Europe/Madrid")):
    sun = sun_position(center_lat, center_lon, when)
    if not sun.is_up:
        continue
    most, _ = shade_bracket(
        DSM,
        DTM,
        INNER,
        sun,
        resolution_m=RES,
        max_distance_m=MAX_DISTANCE_M,
        observer_height_m=OBSERVER_H,
    )
    total += most.size
    open_total += int(OPEN.sum())
    most_total += int(most.sum())
    # Distance from the sun's azimuth to the nearest diagonal sector line.
    to_diagonal = min(
        abs(((sun.azimuth_deg - d * 360.0 / SECTORS + 180) % 360) - 180) for d in DIAGONALS
    )
    row = {
        "when": when.isoformat(),
        "elevation_deg": round(sun.elevation_deg, 2),
        "azimuth_deg": round(sun.azimuth_deg, 2),
        "to_diagonal_deg": round(to_diagonal, 2),
    }
    for mode, path in DIRS.items():
        shade = compute_state_raster(path, sun) != STATE_SUN
        bad = shade != most
        wrong[mode] += int(bad.sum())
        wrong_open[mode] += int(bad[OPEN].sum())
        shaded[mode] += int(shade.sum())
        row[mode] = round(100.0 * float(bad[OPEN].mean()), 4)
    rows.append(row)

print(
    f"{len(rows)} instants, {total:,} pixel-instants, arbiter shade {100.0 * most_total / total:.3f}%\n"
)
print(f"  {'reading':>8}  {'wrong (city)':>13}  {'wrong (open sky)':>17}  {'shade':>8}")
for mode in DIRS:
    print(
        f"  {mode:>8}  {100.0 * wrong[mode] / total:12.3f}%  "
        f"{100.0 * wrong_open[mode] / open_total:16.3f}%  {100.0 * shaded[mode] / total:7.3f}%"
    )

print("\nby how close the sun's azimuth falls to a diagonal sector line (open sky):")
print(f"  {'band':>16}  {'instants':>9}  " + "  ".join(f"{m:>8}" for m in DIRS))
for lo, hi, label in (
    (0.0, 1.5, "on the diagonal"),
    (1.5, 3.0, "inside it"),
    (3.0, 90.0, "elsewhere"),
):
    band = [r for r in rows if lo <= r["to_diagonal_deg"] < hi]
    if not band:
        continue
    means = {m: float(np.mean([r[m] for r in band])) for m in DIRS}
    print(f"  {label:>16}  {len(band):9d}  " + "  ".join(f"{means[m]:7.3f}%" for m in DIRS))

Path("data/bench/s4-corner-verdict.json").write_text(
    json.dumps(
        {
            "instants": len(rows),
            "pixel_instants": total,
            "arbiter_shade_pct": 100.0 * most_total / total,
            "wrong_pct": {m: 100.0 * wrong[m] / total for m in DIRS},
            "wrong_pct_open_sky": {m: 100.0 * wrong_open[m] / open_total for m in DIRS},
            "shaded_pct": {m: 100.0 * shaded[m] / total for m in DIRS},
            "per_instant": rows,
        },
        indent=2,
    )
)
print("\nwrote data/bench/s4-corner-verdict.json")
