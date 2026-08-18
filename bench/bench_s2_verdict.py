"""S2 measurement, step 2: does float32 change the verdict the product ships?

Runs the real ``compute_state_raster`` over both artifact dirs -- published
(float64) and the float32 sweep -- for all 83 instants of the declination
ladder, the ones the product actually renders. Counts two things, in this
order:

1. verdict: sun <-> shade flips. The only thing the product promises.
2. attribution: state changes that keep the verdict (who casts the shade).

The 14-instant battery of learning/muestreo-del-horizonte lives in a
data/analisis-horizonte that no longer exists on this machine; the full ladder
is a superset of it and is defined in code (tiles.LADDER_PRESET_2026).
"""

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from shade_core.artifacts import load_metadata
from shade_core.solar import sun_position
from shade_pipeline.shade_raster import STATE_SUN, compute_state_raster
from shade_pipeline.tiles import bounds_wgs84, season_preset_instants

PUBLISHED = Path("data/cities/montilla-test/v1")
import os

VARIANT = os.environ.get("S2_VARIANT", "C1")
F32 = Path(f"data/bench/montilla-test-{VARIANT.lower()}/v1")

metadata = load_metadata(PUBLISHED)
west, south, east, north = bounds_wgs84(metadata.crs, metadata.bbox)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0
instants = season_preset_instants(ZoneInfo("Europe/Madrid"))
print(f"{len(instants)} instants, {instants[0].isoformat()} .. {instants[-1].isoformat()}")

rows = []
verdict_total = attribution_total = pixels_total = 0
for when in instants:
    sun = sun_position(center_lat, center_lon, when)
    if not sun.is_up:
        print(f"  skip {when.isoformat()} (elevation {sun.elevation_deg:.2f})")
        continue
    a = compute_state_raster(PUBLISHED, sun)
    b = compute_state_raster(F32, sun)
    changed = a != b
    verdict = ((a == STATE_SUN) != (b == STATE_SUN)) & changed
    attribution = changed & ~verdict
    n_verdict, n_attr = int(verdict.sum()), int(attribution.sum())
    verdict_total += n_verdict
    attribution_total += n_attr
    pixels_total += a.size
    rows.append(
        {
            "when": when.isoformat(),
            "elevation_deg": round(sun.elevation_deg, 2),
            "azimuth_deg": round(sun.azimuth_deg, 2),
            "verdict_flips": n_verdict,
            "attribution_changes": n_attr,
            "shaded_pct": round(100.0 * float((a != STATE_SUN).mean()), 2),
        }
    )
    if n_verdict or n_attr:
        print(
            f"  {when.isoformat()} elev {sun.elevation_deg:5.1f} "
            f"verdict {n_verdict:3d}  attribution {n_attr:3d}",
            flush=True,
        )

worst = max(rows, key=lambda r: r["verdict_flips"])
print(
    f"\nverdict flips: {verdict_total:,} of {pixels_total:,} pixel-instants "
    f"({100.0 * verdict_total / pixels_total:.3e}%)"
)
print(f"worst instant: {worst['when']} with {worst['verdict_flips']} flips")
print(
    f"attribution changes: {attribution_total:,} ({100.0 * attribution_total / pixels_total:.3e}%)"
)
print(
    f"instants with any change: {sum(1 for r in rows if r['verdict_flips'] or r['attribution_changes'])}"
    f" of {len(rows)}"
)

Path(f"data/bench/s2-verdict-{VARIANT.lower()}.json").write_text(
    json.dumps(
        {
            "instants": len(rows),
            "pixels_per_instant": int(pixels_total / len(rows)),
            "verdict_flips": verdict_total,
            "attribution_changes": attribution_total,
            "rows": rows,
        },
        indent=2,
    )
)
print(f"wrote data/bench/s2-verdict-{VARIANT.lower()}.json")
