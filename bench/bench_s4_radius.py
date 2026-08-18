"""S4, step 4: at equal error, which lever is cheaper?

geometric500 buys 3.25x for 1.199% of wrong verdicts (open sky). exact250 buys
1.81x for 0.801%. Neither dominates, so the comparison has to be made at equal
error rather than at equal speed: if simply cutting the radius further reaches
geometric's error in less time, then thinning the step contributes nothing at
all and the mode can go.

Sweeps exact at 125 m -- roughly where the radius lever should land near
geometric's error -- and judges it against the same 500 m arbiter as everything
else.
"""

import json
import shutil
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio

from shade_core.artifacts import load_metadata
from shade_core.config import load_city
from shade_core.shade import NO_BLOCKER
from shade_core.solar import sun_position
from shade_pipeline.arbiter import shade_bracket
from shade_pipeline.cog import write_cog
from shade_pipeline.grid import transform_from_bbox
from shade_pipeline.horizon import ANGLE_MAX_DEG, HorizonParams, compute_horizon_tiled
from shade_pipeline.shade_raster import STATE_SUN, compute_state_raster
from shade_pipeline.tiles import bounds_wgs84, season_preset_instants

PUBLISHED = Path("data/cities/montilla-test/v1")
RADII = (125.0, 187.0)
ARBITER_DISTANCE_M = 500.0
OBSERVER_H = 1.6

config = load_city("cities/montilla-test.yaml")
meta = json.loads((PUBLISHED / "metadata.json").read_text())["horizon"]
data = np.load("data/bench/montilla-test-padded.npz")
dsm, dtm, landcover = data["dsm"], data["dtm"], data["landcover"]
inner = tuple(int(v) for v in data["inner"])
res = float(data["resolution_m"][0])

dirs: dict[str, Path] = {}
seconds: dict[str, float] = {}

for radius in RADII:
    name = f"exact{int(radius)}"
    out_dir = Path(f"data/bench/montilla-test-s4-{name}/v1")
    out_dir.mkdir(parents=True, exist_ok=True)
    params = HorizonParams(
        sectors=meta["sectors"],
        max_distance_m=radius,
        observer_height_m=meta["observer_height_m"],
        tile_size=meta["tile_size"],
        workers=1,
    )
    with tempfile.TemporaryDirectory(prefix="s4r-") as scratch:
        start = time.monotonic()
        result = compute_horizon_tiled(
            dsm,
            dtm,
            landcover,
            res,
            params,
            inner,
            scratch_dir=Path(scratch),
            progress=lambda m: None,
        )
        seconds[name] = time.monotonic() - start
        print(f"{name}: swept in {seconds[name]:.1f}s", flush=True)

        transform = transform_from_bbox(config.bbox, res)
        common = {"city_id": config.id}
        horizon_tags = {
            **common,
            "angle_max_deg": str(ANGLE_MAX_DEG),
            "sectors": str(params.sectors),
            "max_distance_m": str(radius),
            "observer_height_m": str(params.observer_height_m),
        }
        for filename, cube, tags in (
            ("horizon.tif", np.asarray(result.angles_q), horizon_tags),
            (
                "blocker_class.tif",
                np.asarray(result.blocker_class),
                {**common, "no_blocker": str(NO_BLOCKER)},
            ),
            (
                "horizon_noveg.tif",
                np.asarray(result.angles_noveg_q),
                {**horizon_tags, "surface": "vegetation lowered to terrain"},
            ),
        ):
            write_cog(out_dir / filename, cube, transform, config.crs, tags=tags)
    for filename in ("canopy.tif", "metadata.json"):
        shutil.copy2(PUBLISHED / filename, out_dir / filename)
    dirs[name] = out_dir

with rasterio.open(PUBLISHED / "canopy.tif") as src:
    OPEN = ~(src.read()[0] != 0)

metadata = load_metadata(PUBLISHED)
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0

wrong = dict.fromkeys(dirs, 0)
wrong_open = dict.fromkeys(dirs, 0)
total = open_total = 0

for when in season_preset_instants(ZoneInfo("Europe/Madrid")):
    sun = sun_position(center_lat, center_lon, when)
    if not sun.is_up:
        continue
    most, _ = shade_bracket(
        dsm,
        dtm,
        inner,
        sun,
        resolution_m=res,
        max_distance_m=ARBITER_DISTANCE_M,
        observer_height_m=OBSERVER_H,
    )
    total += most.size
    open_total += int(OPEN.sum())
    for name, path in dirs.items():
        bad = (compute_state_raster(path, sun) != STATE_SUN) != most
        wrong[name] += int(bad.sum())
        wrong_open[name] += int(bad[OPEN].sum())

previous = json.loads(Path("data/bench/s4-verdict.json").read_text())
table = {
    name: {"seconds": previous["seconds"][name], "open": previous["wrong_pct_open_sky"][name]}
    for name in previous["seconds"]
}
for name in dirs:
    table[name] = {"seconds": seconds[name], "open": 100.0 * wrong_open[name] / open_total}

print(f"\n  {'variant':>13}  {'seconds':>8}  {'speedup':>8}  {'wrong (open sky)':>17}")
base = table["exact500"]["seconds"]
for name, entry in sorted(table.items(), key=lambda item: item[1]["open"]):
    print(
        f"  {name:>13}  {entry['seconds']:8.1f}  {base / entry['seconds']:7.2f}x  "
        f"{entry['open']:16.3f}%"
    )

Path("data/bench/s4-radius.json").write_text(json.dumps(table, indent=2))
print("\nwrote data/bench/s4-radius.json")
