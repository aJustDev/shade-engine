"""S4, step 2: which uniform rule should break the corner tie?

Today the tie is broken by ``next_col < next_row``, i.e. by 1-3 ulp of sin
against cos. Transcendentals are not required to round identically across libms
or architectures, so a libm change on the VPS moves 3.67 M cube cells with
nobody touching the code. The fix is a rule; this picks which.

Only two uniform rules exist -- row first or column first -- and today's cube is
neither: sectors 8 and 40 emit the row cell, 24 and 56 the column one. So either
rule moves two of the four diagonal planes.

**The criterion, written before looking:** emitting one cell is the average of
the two lateral limits and both cells are equally legitimate, so the verdict
should tie. If the two rules land within 0.01 points of each other, the winner
is whichever moves today's cube least -- a cube that does not move is a rebuild
that does not have to be explained. If they do not tie, the verdict wins.
"""

import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio

from shade_core.artifacts import load_metadata
from shade_core.raycast import ray_cells
from shade_core.shade import NO_BLOCKER, Landcover
from shade_core.solar import sun_position
from shade_pipeline.cog import write_cog
from shade_pipeline.horizon import height_datum, quantize_angles
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


def with_rule(sector: int, rule: str) -> list[tuple[int, int, float, float]]:
    """The ray's cells with the corner tie broken by a fixed rule.

    ``rule`` is "row" or "col": which axis the grazed orthogonal cell steps
    along. The diagonal cell that follows is the same either way, so only the
    zero-thickness one changes.
    """
    cells = ray_cells(sector * 360.0 / SECTORS, MAX_DISTANCE_M, RES)
    out = []
    index = 0
    while index < len(cells):
        cell = cells[index]
        nxt = cells[index + 1] if index + 1 < len(cells) else None
        if nxt is not None and math.isclose(cell[2], nxt[2], rel_tol=1e-9):
            previous = out[-1][:2] if out else (0, 0)
            steps_row = cell[0] != previous[0]
            wanted_row = rule == "row"
            if steps_row == wanted_row:
                out.append(cell)
            else:
                mirror_delta = (nxt[0] - cell[0], nxt[1] - cell[1])
                out.append(
                    (previous[0] + mirror_delta[0], previous[1] + mirror_delta[1], cell[2], cell[3])
                )
            out.append(nxt)
            index += 2
        else:
            out.append(cell)
            index += 1
    return out


def sweep_sector(cells: list) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
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


def build(rule: str) -> tuple[Path, int]:
    out_dir = Path(f"data/bench/montilla-test-tie-{rule}/v1")
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("canopy.tif", "metadata.json"):
        (out_dir / filename).write_bytes((BASE / filename).read_bytes())

    planes = {}
    for sector in DIAGONALS:
        cells = with_rule(sector, rule)
        angles, blocker, noveg = sweep_sector(cells)
        planes[sector] = (quantize_angles(angles), blocker, quantize_angles(noveg))
        first = cells[0][:2]
        print(f"  {rule}: sector {sector} grazes ({first[0]:+d},{first[1]:+d})", flush=True)

    changed = 0
    for index, filename in enumerate(("horizon.tif", "blocker_class.tif", "horizon_noveg.tif")):
        with rasterio.open(BASE / filename) as src:
            cube = src.read()
            tags, transform, crs = src.tags(), src.transform, src.crs
        for sector, trio in planes.items():
            changed += int((cube[sector] != trio[index]).sum())
            cube[sector] = trio[index]
        write_cog(out_dir / filename, cube, transform, crs, tags=tags)
    print(f"  {rule}: {changed:,} cells differ from today's cube", flush=True)
    return out_dir, changed


dirs: dict[str, Path] = {}
moved: dict[str, int] = {}
for rule in ("row", "col"):
    dirs[rule], moved[rule] = build(rule)

with rasterio.open("data/cities/montilla-test/v1/canopy.tif") as src:
    OPEN = ~(src.read()[0] != 0)

metadata = load_metadata(BASE)
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

judged = {**dirs, "today": BASE}
wrong_open = dict.fromkeys(judged, 0)
open_total = 0

# The arbiter is imported from the module, not reimplemented here.
from shade_pipeline.arbiter import shade_bracket  # noqa: E402

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
    open_total += int(OPEN.sum())
    for name, path in judged.items():
        bad = (compute_state_raster(path, sun) != STATE_SUN) != most
        wrong_open[name] += int(bad[OPEN].sum())

print(f"\n  {'rule':>8}  {'wrong (open sky)':>17}  {'cells moved vs today':>21}")
for name in judged:
    print(f"  {name:>8}  {100.0 * wrong_open[name] / open_total:16.4f}%  {moved.get(name, 0):21,}")

gap = abs(100.0 * wrong_open["row"] / open_total - 100.0 * wrong_open["col"] / open_total)
print(f"\ngap between the two rules: {gap:.4f} points")
if gap <= 0.01:
    winner = min(("row", "col"), key=lambda rule: moved[rule])
    print(f"they tie (<= 0.01), so the winner is the one that moves least: {winner}")
else:
    winner = min(("row", "col"), key=lambda rule: wrong_open[rule])
    print(f"they do not tie, so the verdict decides: {winner}")

Path("data/bench/s4-tiebreak.json").write_text(
    json.dumps(
        {
            "wrong_pct_open_sky": {n: 100.0 * wrong_open[n] / open_total for n in judged},
            "cells_moved": moved,
            "gap_points": gap,
            "winner": winner,
        },
        indent=2,
    )
)
print("\nwrote data/bench/s4-tiebreak.json")
