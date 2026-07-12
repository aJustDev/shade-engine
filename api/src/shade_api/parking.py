"""``GET /v1/parking/nearby``: zones around a point with their shade state.

Division of labor: PostGIS answers "which zones are near" (``ST_DWithin``
on geography, in meters); the shade engine answers "are they shaded".

A zone is a polyline, so it rarely has ONE shade state. We resample its
geometry every ``SAMPLE_SPACING_M`` meters -- in the city's projected CRS,
never in degrees -- and report the shaded fraction plus a majority verdict
(``SHADE_THRESHOLD``). ``shaded_until`` sweeps the day's remaining sun
positions (one vectorized pvlib call per request, shared by every zone;
per-instant calls would cost milliseconds of pandas overhead each) until
the fraction drops below the threshold or daylight ends, mirroring how
``shade_timeline`` closes its intervals.
"""

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Annotated, Any, cast

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pyproj import Transformer
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from shade_api.registry import CityRuntime
from shade_api.routes import _AT_DESCRIPTION, Lat, Lon, Registry, _locate, resolve_at
from shade_api.schemas import (
    ParkingNearbyOut,
    ParkingShadeOut,
    ParkingZoneOut,
    ScheduleEntryOut,
)
from shade_core.db import ParkingZone
from shade_core.shade import ShadeScene, ShadeState, is_shaded
from shade_core.solar import SunPosition, sun_position, sun_positions_for_day

router = APIRouter(prefix="/v1")

SAMPLE_SPACING_M = 10.0
SHADE_THRESHOLD = 0.5
MAX_ZONES = 50

# (block-local scene, pixel-center x, pixel-center y) per sample point.
SampleScenes = list[tuple[ShadeScene, float, float]]


def get_sessionmaker(request: Request) -> sessionmaker[Session]:
    maker = cast("sessionmaker[Session] | None", request.app.state.db_sessionmaker)
    if maker is None:
        raise HTTPException(status_code=503, detail="parking layer not configured (no database)")
    return maker


SessionFactory = Annotated[sessionmaker[Session], Depends(get_sessionmaker)]


def sample_polyline(
    parts: Sequence[Sequence[Sequence[float]]], to_projected: Transformer
) -> list[tuple[float, float]]:
    """Points every ``SAMPLE_SPACING_M`` along a MultiLineString, projected.

    Arc-length resampling: project the lon/lat vertices to the city CRS
    (degrees do not measure length), accumulate segment lengths, and
    ``np.interp`` evenly spaced positions back to coordinates. Endpoints
    are always included; a degenerate zero-length part yields one point.
    """
    samples: list[tuple[float, float]] = []
    for part in parts:
        lons = np.array([vertex[0] for vertex in part])
        lats = np.array([vertex[1] for vertex in part])
        xs, ys = to_projected.transform(lons, lats)
        cum = np.concatenate(([0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))))
        total = float(cum[-1])
        if total == 0.0:
            samples.append((float(xs[0]), float(ys[0])))
            continue
        positions = np.linspace(0.0, total, max(int(total // SAMPLE_SPACING_M) + 2, 2))
        samples.extend(
            (float(x), float(y))
            for x, y in zip(
                np.interp(positions, cum, xs), np.interp(positions, cum, ys), strict=True
            )
        )
    return samples


def _shaded_fraction(scenes: SampleScenes, sun: SunPosition) -> float:
    shaded = sum(
        1 for scene, x, y in scenes if is_shaded(scene, x, y, sun).state is ShadeState.SHADE
    )
    return shaded / len(scenes)


def _shaded_until(
    scenes: SampleScenes, future_sun: Sequence[tuple[datetime, SunPosition]]
) -> datetime | None:
    """First future step where the zone stops being majority-shaded.

    A step with the sun below the horizon also ends the run (the shade run
    ends when daylight does), matching ``shade_timeline``'s closing rule.
    """
    for stamp, sun in future_sun:
        if not sun.is_up or _shaded_fraction(scenes, sun) < SHADE_THRESHOLD:
            return stamp
    return None


def _zone_shade(
    runtime: CityRuntime,
    samples: Sequence[tuple[float, float]],
    sun: SunPosition,
    future_sun: Callable[[], Sequence[tuple[datetime, SunPosition]]],
) -> ParkingShadeOut | None:
    """Aggregate verdict for one zone; None when it falls off the rasters.

    Scenes are resolved once per sample (they are time-independent), so
    each sweep step below only costs a horizon interpolation on an
    LRU-cached block, not a rasterio read.
    """
    scenes: SampleScenes = [
        runtime.reader.scene_for(x, y) for x, y in samples if runtime.reader.contains(x, y)
    ]
    if not scenes:
        return None
    if not sun.is_up:
        return ParkingShadeOut(
            state=ShadeState.NIGHT, in_shade=False, shade_fraction=None, shaded_until=None
        )
    fraction = _shaded_fraction(scenes, sun)
    in_shade = fraction >= SHADE_THRESHOLD
    return ParkingShadeOut(
        state=ShadeState.SHADE if in_shade else ShadeState.SUN,
        in_shade=in_shade,
        shade_fraction=round(fraction, 3),
        shaded_until=_shaded_until(scenes, future_sun()) if in_shade else None,
    )


@router.get("/parking/nearby", summary="Parking zones near a point, with shade state")
def parking_nearby(
    registry: Registry,
    session_factory: SessionFactory,
    response: Response,
    city: str,
    lat: Lat,
    lon: Lon,
    radius: Annotated[float, Query(gt=0, le=1000, description="Search radius, meters")] = 300.0,
    at: Annotated[datetime | None, Query(description=_AT_DESCRIPTION)] = None,
) -> ParkingNearbyOut:
    """Zones within ``radius`` meters, nearest first, with shade at ``at``."""
    runtime, _, _ = _locate(registry, city, lat, lon)
    when = resolve_at(at, runtime.tz)
    # One sun position serves every zone: within a <=1 km radius the
    # difference is far below the horizon raster's angular resolution.
    sun = sun_position(lat, lon, when)

    point = func.ST_GeogFromText(f"SRID=4326;POINT({lon} {lat})")
    distance = func.ST_Distance(ParkingZone.geom, point).label("distance_m")
    statement = (
        select(ParkingZone, distance, func.ST_AsGeoJSON(ParkingZone.geom).label("geometry"))
        .where(
            ParkingZone.city_id == runtime.config.id,
            func.ST_DWithin(ParkingZone.geom, point, radius),
        )
        .order_by(distance)
        .limit(MAX_ZONES)
    )
    try:
        with session_factory() as session:
            rows = session.execute(statement).all()
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail="parking database unavailable") from exc

    # The day's remaining sun positions, computed lazily at most once per
    # request and shared by every shaded zone's shaded_until sweep.
    sweep: list[tuple[datetime, SunPosition]] | None = None

    def future_sun() -> Sequence[tuple[datetime, SunPosition]]:
        nonlocal sweep
        if sweep is None:
            sweep = [
                (stamp, position)
                for stamp, position in sun_positions_for_day(lat, lon, when.date(), runtime.tz)
                if stamp > when
            ]
        return sweep

    zones: list[ParkingZoneOut] = []
    for zone, distance_m, geometry_json in rows:
        geometry: dict[str, Any] = json.loads(geometry_json)
        samples = sample_polyline(geometry["coordinates"], runtime.to_projected)
        zones.append(
            ParkingZoneOut(
                name=zone.name,
                zone_type=zone.zone_type,
                orientation=zone.orientation,
                capacity=zone.capacity,
                schedule=[
                    ScheduleEntryOut(days=entry["days"], from_=entry["from"], to=entry["to"])
                    for entry in zone.schedule
                ],
                max_minutes=zone.max_minutes,
                tariff_eur_hour=zone.tariff_eur_hour,
                notes=zone.notes,
                source=zone.source,
                last_verified=zone.last_verified,
                distance_m=round(float(distance_m), 1),
                geometry=geometry,
                shade=_zone_shade(runtime, samples, sun, future_sun),
            )
        )
    # Explicit instants are stable but the layer can be re-imported, hence
    # the short TTL; implicit "now" moves with the clock.
    response.headers["Cache-Control"] = "public, max-age=60" if at is not None else "no-store"
    return ParkingNearbyOut(
        city=runtime.config.id,
        at=when,
        radius_m=radius,
        zones=zones,
        attribution=runtime.metadata.attribution,
    )
