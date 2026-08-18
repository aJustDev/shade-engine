"""Desk check: what does Sierra Morena subtend from Cordoba, and does 500 m see it?

S4 established that the radius bounds its error by atan(h/R): whatever falls
outside radius R can raise the horizon by at most that. On montilla-test, flat
country, 950 m of radius bounds the far field to 1.81 degrees and the measured
flips duly sat at 1.30-1.59 degrees of solar elevation.

Cordoba is not flat, and it is the city S6 rebuilds. Sierra Morena rises north
of the city, permanently outside any 500 m radius. If it subtends more than the
0.353-degree quantization step -- let alone the 2-3 degrees a glance at a
contour map suggests -- then Cordoba has a **systematic, city-wide** error at
sunset in the months when the sun sets north of west, not the edge shimmer that
a sampling choice produces.

This needs no sweep and no LiDAR: a public MDT25 from the IGN WCS, a few
profiles from the old town toward the June sunset azimuths, and trigonometry.

Reads the horizon exactly as the engine defines it: observer at DTM + 1.6 m,
each cell blocking from the distance the ray *enters* it (ADR-027), angles from
atan(dz / horizontal distance).
"""

import json
import math
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import rasterio

from shade_core.solar import sun_positions_for_day
from shade_pipeline.tiles import bounds_wgs84

MDT = Path("data/bench/cordoba-mdt25.tif")
"""MDT25 del IGN, ventana 330-350 km E x 4188-4212 km N en EPSG:25830. Se baja con:

    curl -o data/bench/cordoba-mdt25.tif \
      "https://servicios.idee.es/wcs-inspire/mdt?service=WCS&version=2.0.1&request=GetCoverage\
&coverageId=Elevacion25830_25&subset=x(330000,350000)&subset=y(4188000,4212000)&format=image/tiff"
"""
CITY_BBOX = (338415, 4189828, 347432, 4200642)  # cities/cordoba.yaml, EPSG:25830
OBSERVER_H = 1.6
ENGINE_RADIUS_M = 500.0
MAX_PROFILE_M = 14000.0
MADRID = ZoneInfo("Europe/Madrid")

with rasterio.open(MDT) as src:
    dtm = src.read(1).astype(np.float64)
    transform = src.transform
    res = src.res[0]
    crs = src.crs
    bounds = src.bounds

west, south, east, north = bounds_wgs84(str(crs), CITY_BBOX)
center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0
print(f"Cordoba centre: {center_lat:.4f} N, {center_lon:.4f} E")


def elevation_at(x: float, y: float) -> float:
    col, row = ~transform * (x, y)
    row, col = int(row), int(col)
    if not (0 <= row < dtm.shape[0] and 0 <= col < dtm.shape[1]):
        return math.nan
    return float(dtm[row, col])


def horizon_along(x: float, y: float, azimuth_deg: float) -> tuple[float, float, float]:
    """(angle within 500 m, angle beyond it, distance of the far winner in m).

    Walks the profile at the MDT's own step. The observer's eye is at
    terrain + 1.6 m, and a cell at distance d raises the horizon by
    atan((z - eye) / d) -- the same definition the sweep uses.
    """
    eye = elevation_at(x, y) + OBSERVER_H
    azimuth = math.radians(azimuth_deg)
    step_east, step_north = math.sin(azimuth), math.cos(azimuth)
    near_max = far_max = -90.0
    far_distance = 0.0
    distance = res
    while distance <= MAX_PROFILE_M:
        z = elevation_at(x + step_east * distance, y + step_north * distance)
        if math.isnan(z):
            break
        angle = math.degrees(math.atan((z - eye) / distance))
        if distance <= ENGINE_RADIUS_M:
            near_max = max(near_max, angle)
        elif angle > far_max:
            far_max = angle
            far_distance = distance
        distance += res
    return near_max, far_max, far_distance


# --- the June sunset azimuths, from the engine's own solar code -----------------

samples = sun_positions_for_day(center_lat, center_lon, date(2026, 6, 21), MADRID, step_minutes=1)
evening = [(when, sun) for when, sun in samples if sun.azimuth_deg > 180.0]
by_elevation = {}
for target in (5.0, 3.0, 2.0, 1.0, 0.0):
    match = min(evening, key=lambda item: abs(item[1].elevation_deg - target))
    by_elevation[target] = match
    print(
        f"  21 jun, elevacion {target:4.1f} deg -> {match[0].strftime('%H:%M')} "
        f"azimut {match[1].azimuth_deg:.1f}"
    )

# How fast the sun drops near the horizon, which turns degrees into minutes.
low = [(w, s) for w, s in evening if 0.0 < s.elevation_deg < 6.0]
drop_per_minute = (
    abs(low[0][1].elevation_deg - low[-1][1].elevation_deg) / max(len(low) - 1, 1) if low else 0.0
)
print(f"  the sun falls {drop_per_minute:.3f} deg/min near sunset")

# --- profiles from the old town -------------------------------------------------

points = {
    "Mezquita": (342700, 4192800),
    "Tendillas": (343200, 4194100),
    "Ciudad Jardin": (341600, 4194600),
    "Norte del casco": (343000, 4196500),
    "Poniente": (340500, 4194000),
}
azimuths = {label: match[1].azimuth_deg for label, match in by_elevation.items()}

rows = []
print(f"\n{'punto':>16}  {'elev m':>7}  {'azimut':>7}  {'<500 m':>8}  {'>500 m':>8}  {'gana a':>9}")
for name, (x, y) in points.items():
    ground = elevation_at(x, y)
    for target, azimuth in azimuths.items():
        near, far, far_d = horizon_along(x, y, azimuth)
        rows.append(
            {
                "point": name,
                "ground_m": ground,
                "sun_elevation_deg": target,
                "azimuth_deg": round(azimuth, 2),
                "horizon_within_500m_deg": round(near, 3),
                "horizon_beyond_500m_deg": round(far, 3),
                "far_winner_distance_m": far_d,
                "hidden_deg": round(max(far - max(near, 0.0), 0.0), 3),
            }
        )
        print(
            f"{name:>16}  {ground:7.1f}  {azimuth:7.1f}  {near:7.2f}  {far:7.2f}  "
            f"{far_d / 1000:6.1f} km"
        )

# --- what it means --------------------------------------------------------------

hidden = [r["hidden_deg"] for r in rows]
worst = max(rows, key=lambda r: r["hidden_deg"])
quantum = 90.0 / 255.0
print(f"\nhorizonte que un radio de 500 m no ve, hacia el ocaso de junio:")
print(f"  mediana {np.median(hidden):.2f} deg, maximo {max(hidden):.2f} deg")
print(
    f"  peor caso: {worst['point']} a {worst['azimuth_deg']} deg, "
    f"{worst['hidden_deg']} deg desde {worst['far_winner_distance_m'] / 1000:.1f} km"
)
print(f"  pasos de cuantizacion (0,353 deg): {max(hidden) / quantum:.0f}")
if drop_per_minute:
    print(f"  el ocaso efectivo se adelanta hasta {max(hidden) / drop_per_minute:.0f} min")

Path("data/bench/s4-cordoba-sierra.json").write_text(
    json.dumps(
        {
            "mdt": str(MDT),
            "mdt_bounds": list(bounds),
            "engine_radius_m": ENGINE_RADIUS_M,
            "sun_drop_deg_per_min": drop_per_minute,
            "quantum_deg": quantum,
            "median_hidden_deg": float(np.median(hidden)),
            "max_hidden_deg": float(max(hidden)),
            "minutes_of_early_sunset": float(max(hidden) / drop_per_minute)
            if drop_per_minute
            else 0.0,
            "profiles": rows,
        },
        indent=2,
    )
)
print("\nwrote data/bench/s4-cordoba-sierra.json")
