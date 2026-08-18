"""S4, follow-up: does the far field move sunset boundaries?

The other half of the radius question, and the one that was never measured with
a temporal metric. muestreo-del-horizonte says 500 m against 2000 m "does not
change a single pixel" except 0.011% with the sun at 1.26 degrees -- but that is
an *instantaneous* figure, and this session showed those say nothing about
interval boundaries: cutting to 250 m left the instant verdict identical above
10 degrees of elevation while moving shade_timeline edges below it.

If shortening 500 -> 250 moves boundaries, there is no reason to assume
950 -> 500 does not. And it matters beyond curiosity: S7 walks the street, and
the scenario where someone with a phone notices the engine is wrong is exactly
a long late-afternoon shadow the model truncated. If the far field does move
sunset edges, the dawn and dusk points of the walk stop being decoration and
become its most informative part.

**Reachable radius.** 2000 m would need 6x6 LAZ tiles around montilla-test and
the cache holds 4x3, leaving 986 m of uniform pad. This measures 950 against
500 on the wide stack, which doubles the radius rather than quadrupling it. A
null result here does not clear 2000 m; a positive one settles the question.
"""

import json
import random
import shutil
import tempfile
import time
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio

from shade_core.artifacts import load_metadata, load_scene
from shade_core.config import load_city
from shade_core.shade import NO_BLOCKER, shade_timeline
from shade_core.solar import sun_position
from shade_pipeline.cog import write_cog
from shade_pipeline.grid import transform_from_bbox
from shade_pipeline.horizon import ANGLE_MAX_DEG, HorizonParams, compute_horizon_tiled
from shade_pipeline.shade_raster import STATE_SUN, compute_state_raster
from shade_pipeline.tiles import bounds_wgs84, season_preset_instants

PUBLISHED = Path("data/cities/montilla-test/v1")
RADII = (500.0, 950.0)
TIMELINE_PIXELS = 400
STEP_MINUTES = 5
MADRID = ZoneInfo("Europe/Madrid")

config = load_city("cities/montilla-test.yaml")
meta = json.loads((PUBLISHED / "metadata.json").read_text())["horizon"]
data = np.load("data/bench/montilla-test-wide.npz")
dsm, dtm, landcover = data["dsm"], data["dtm"], data["landcover"]
inner = tuple(int(v) for v in data["inner"])
res = float(data["resolution_m"][0])
print(f"wide stack {dsm.shape}, inner {inner}")

dirs: dict[str, Path] = {}
seconds: dict[str, float] = {}
for radius in RADII:
    name = f"far{int(radius)}"
    out_dir = Path(f"data/bench/montilla-test-{name}/v1")
    out_dir.mkdir(parents=True, exist_ok=True)
    params = HorizonParams(
        sectors=meta["sectors"],
        max_distance_m=radius,
        observer_height_m=meta["observer_height_m"],
        tile_size=meta["tile_size"],
        workers=1,
    )
    with tempfile.TemporaryDirectory(prefix="s4f-") as scratch:
        start = time.monotonic()
        result = compute_horizon_tiled(
            dsm,
            dtm,
            landcover,
            res,
            params,
            inner,
            scratch_dir=Path(scratch),
            progress=lambda message: None,
        )
        seconds[name] = time.monotonic() - start
        print(f"{name}: swept in {seconds[name]:.1f}s", flush=True)
        transform = transform_from_bbox(config.bbox, res)
        common = {"city_id": config.id}
        tags = {
            **common,
            "angle_max_deg": str(ANGLE_MAX_DEG),
            "sectors": str(params.sectors),
            "max_distance_m": str(radius),
            "observer_height_m": str(params.observer_height_m),
        }
        for filename, cube, extra in (
            ("horizon.tif", np.asarray(result.angles_q), tags),
            (
                "blocker_class.tif",
                np.asarray(result.blocker_class),
                {**common, "no_blocker": str(NO_BLOCKER)},
            ),
            (
                "horizon_noveg.tif",
                np.asarray(result.angles_noveg_q),
                {**tags, "surface": "vegetation lowered to terrain"},
            ),
        ):
            write_cog(out_dir / filename, cube, transform, config.crs, tags=extra)
    for filename in ("canopy.tif", "metadata.json", "landcover.tif", "dsm.tif", "dtm.tif"):
        shutil.copy2(PUBLISHED / filename, out_dir / filename)
    dirs[name] = out_dir

# --- what the extra 450 m adds to the cube --------------------------------------

scale = ANGLE_MAX_DEG / 255.0
raised = raised_over_1 = raised_over_5 = 0
biggest = 0
with (
    rasterio.open(dirs["far500"] / "horizon.tif") as near,
    rasterio.open(dirs["far950"] / "horizon.tif") as far,
):
    total_cells = 0
    for band in range(1, near.count + 1):
        delta = far.read([band])[0].astype(np.int16) - near.read([band])[0].astype(np.int16)
        total_cells += delta.size
        raised += int((delta > 0).sum())
        raised_over_1 += int((delta * scale > 1.0).sum())
        raised_over_5 += int((delta * scale > 5.0).sum())
        biggest = max(biggest, int(delta.max()))
        assert delta.min() >= 0, "a longer radius can only raise the horizon"
print(
    f"\ncube: {raised:,} of {total_cells:,} cells rise ({100.0 * raised / total_cells:.3f}%), "
    f"{raised_over_1:,} by more than 1 deg, {raised_over_5:,} by more than 5; "
    f"worst {biggest * scale:.2f} deg"
)

# --- the instantaneous verdict, which is what the old figure measured -----------

metadata = load_metadata(PUBLISHED)
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

flips_total = 0
pixel_instants = 0
per_instant = []
ever = None
for when in season_preset_instants(MADRID):
    sun = sun_position(center_lat, center_lon, when)
    if not sun.is_up:
        continue
    near_shade = compute_state_raster(dirs["far500"], sun) != STATE_SUN
    far_shade = compute_state_raster(dirs["far950"], sun) != STATE_SUN
    flipped = near_shade != far_shade
    ever = flipped if ever is None else (ever | flipped)
    flips_total += int(flipped.sum())
    pixel_instants += flipped.size
    if flipped.any():
        per_instant.append(
            {
                "when": when.isoformat(),
                "elevation_deg": round(sun.elevation_deg, 2),
                "flips": int(flipped.sum()),
                "flip_pct": round(100.0 * float(flipped.mean()), 4),
            }
        )

print(f"\ninstantaneous verdict over {pixel_instants:,} pixel-instants:")
print(f"  flips: {flips_total:,} ({100.0 * flips_total / pixel_instants:.4f}%)")
for row in sorted(per_instant, key=lambda r: -r["flips"])[:5]:
    print(
        f"    {row['when']} elev {row['elevation_deg']:5.2f}: {row['flips']:,} ({row['flip_pct']}%)"
    )
ever_pct = 100.0 * float(ever.mean()) if ever is not None else 0.0
print(f"  pixels that flip at any instant: {ever_pct:.3f}% of the city")

# --- the temporal metric, which is the one this session learned to ask for ------

scene_near = load_scene(dirs["far500"])
scene_far = load_scene(dirs["far950"])
with rasterio.open(dirs["far500"] / "horizon.tif") as src:
    affine = src.transform

random.seed(20260818)
rows_all, cols_all = ever.shape
flipped_cells = np.argwhere(ever)
print(f"\n{len(flipped_cells):,} pixels flip at some instant")


def compare(pixels: list[tuple[int, int]], day: date) -> dict[str, float]:
    moved = over_step = compared = 0
    worst = 0.0
    for row, col in pixels:
        x, y = affine * (col + 0.5, row + 0.5)
        try:
            near = shade_timeline(
                scene_near, x, y, center_lat, center_lon, day, MADRID, STEP_MINUTES
            )
            far = shade_timeline(scene_far, x, y, center_lat, center_lon, day, MADRID, STEP_MINUTES)
        except ValueError:
            continue
        compared += 1
        edges_near = [interval.start for interval in near[1:]]
        edges_far = [interval.start for interval in far[1:]]
        if len(edges_near) != len(edges_far):
            moved += 1
            over_step += 1
            continue
        for a, b in zip(edges_near, edges_far, strict=True):
            delta = abs((a - b).total_seconds()) / 60.0
            if delta > 0:
                moved += 1
                worst = max(worst, delta)
                if delta > STEP_MINUTES:
                    over_step += 1
                break
    return {"compared": compared, "moved": moved, "over_step": over_step, "worst_min": worst}


results = {}
for label, day in (("winter solstice", date(2026, 12, 21)), ("equinox", date(2026, 3, 20))):
    random_pixels = [
        (random.randrange(rows_all), random.randrange(cols_all)) for _ in range(TIMELINE_PIXELS)
    ]
    flipping = (
        [
            (int(flipped_cells[i][0]), int(flipped_cells[i][1]))
            for i in random.sample(
                range(len(flipped_cells)), min(TIMELINE_PIXELS, len(flipped_cells))
            )
        ]
        if len(flipped_cells)
        else []
    )
    results[label] = {"random": compare(random_pixels, day)}
    if flipping:
        results[label]["flipping"] = compare(flipping, day)
    print(f"\n{label}, {STEP_MINUTES} min step:")
    for which, entry in results[label].items():
        print(
            f"  {which:>9}: {entry['moved']}/{entry['compared']} move a boundary, "
            f"{entry['over_step']} by more than one step, worst {entry['worst_min']:.0f} min"
        )

Path("data/bench/s4-farfield.json").write_text(
    json.dumps(
        {
            "radii": list(RADII),
            "seconds": seconds,
            "cube_cells_raised": raised,
            "cube_cells_raised_over_1_deg": raised_over_1,
            "cube_cells_raised_over_5_deg": raised_over_5,
            "worst_rise_deg": biggest * scale,
            "instant_flip_pct": 100.0 * flips_total / pixel_instants,
            "city_pct_ever_flipping": ever_pct,
            "per_instant": per_instant,
            "timelines": results,
        },
        indent=2,
    )
)
print("\nwrote data/bench/s4-farfield.json")
