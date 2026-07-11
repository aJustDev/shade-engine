"""HTTP endpoints.

All endpoints are sync ``def``: the engine is synchronous (rasterio reads
under a lock), so FastAPI runs them in its threadpool and the event loop
never blocks.
"""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Annotated, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from shade_api.registry import CityRegistry, CityRuntime
from shade_api.schemas import (
    CityDetail,
    CityOut,
    HealthOut,
    ShadeOut,
    SunOut,
    TimelineIntervalOut,
    TimelineOut,
)
from shade_core.shade import ShadeInterval, ShadeState, is_shaded, shade_timeline
from shade_core.solar import sun_position

router = APIRouter(prefix="/v1")
health_router = APIRouter()

Lat = Annotated[float, Query(ge=-90, le=90, description="Latitude, WGS84 degrees")]
Lon = Annotated[float, Query(ge=-180, le=180, description="Longitude, WGS84 degrees")]

_AT_DESCRIPTION = (
    "ISO 8601 instant. Without a UTC offset it is interpreted in the city's "
    "timezone; omitted means now. URL-encode a '+' offset as %2B (a literal "
    "'+' in a query string is a space)."
)


def get_registry(request: Request) -> CityRegistry:
    # app.state is untyped; the cast keeps mypy strict happy.
    return cast(CityRegistry, request.app.state.registry)


Registry = Annotated[CityRegistry, Depends(get_registry)]


def _runtime_or_404(registry: CityRegistry, city_id: str) -> CityRuntime:
    try:
        return registry.get(city_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown city: {city_id}") from exc


def _city_out(runtime: CityRuntime) -> CityOut:
    return CityOut(
        id=runtime.config.id,
        name=runtime.config.name,
        country=runtime.config.country,
        timezone=runtime.config.timezone,
        bbox_wgs84=runtime.bbox_wgs84,
        attribution=runtime.metadata.attribution,
    )


@router.get("/cities", summary="Cities with built artifacts")
def list_cities(registry: Registry, response: Response) -> list[CityOut]:
    """Cities this deployment can answer for, with WGS84 bounds and attribution."""
    response.headers["Cache-Control"] = "public, max-age=3600"
    return [_city_out(runtime) for runtime in registry.all()]


@router.get("/cities/{city_id}", summary="One city plus its artifact build metadata")
def city_detail(city_id: str, registry: Registry, response: Response) -> CityDetail:
    """The city entry plus the full metadata of the loaded artifacts."""
    runtime = _runtime_or_404(registry, city_id)
    response.headers["Cache-Control"] = "public, max-age=3600"
    return CityDetail(
        **_city_out(runtime).model_dump(),
        artifacts=runtime.metadata,
    )


def _locate(
    registry: CityRegistry, city: str, lat: float, lon: float
) -> tuple[CityRuntime, float, float]:
    """City runtime plus the point in its projected CRS; 400 outside coverage.

    The containment check also absorbs the non-finite coordinates pyproj
    yields for points outside the projection's domain.
    """
    runtime = _runtime_or_404(registry, city)
    x, y = runtime.to_projected.transform(lon, lat)
    if not runtime.reader.contains(x, y):
        raise HTTPException(status_code=400, detail="point outside city coverage")
    return runtime, x, y


def resolve_at(at: datetime | None, tz: ZoneInfo) -> datetime:
    """The instant to answer for, expressed in the city's timezone.

    None means "now". A naive datetime is interpreted in the city's timezone
    -- the API-boundary rule the core solar module refuses to guess. An
    aware one keeps its instant and is converted for the response.
    """
    if at is None:
        return datetime.now(tz)
    if at.tzinfo is None:
        return at.replace(tzinfo=tz)
    return at.astimezone(tz)


def shaded_until(intervals: Sequence[ShadeInterval], now: datetime) -> datetime | None:
    """End of the shaded run containing ``now``; None if not shaded right now.

    Consecutive SHADE intervals (building shade rolling into vegetation
    shade) count as one run: the caller wants the instant the sun returns,
    not the instant the shade changes flavor.
    """
    for index, interval in enumerate(intervals):
        if interval.start <= now < interval.end:
            if interval.state is not ShadeState.SHADE:
                return None
            end = interval.end
            for later in intervals[index + 1 :]:
                if later.state is not ShadeState.SHADE or later.start != end:
                    break
                end = later.end
            return end
    return None


@router.get("/shade", summary="Shade verdict for a point at an instant")
def shade(
    registry: Registry,
    response: Response,
    city: str,
    lat: Lat,
    lon: Lon,
    at: Annotated[datetime | None, Query(description=_AT_DESCRIPTION)] = None,
) -> ShadeOut:
    """Is this point in the sun, in shade (and cast by what), or in the night?"""
    runtime, x, y = _locate(registry, city, lat, lon)
    when = resolve_at(at, runtime.tz)
    sun = sun_position(lat, lon, when)
    scene, center_x, center_y = runtime.reader.scene_for(x, y)
    result = is_shaded(scene, center_x, center_y, sun)
    # A verdict for an explicit instant never changes; "now" is not cacheable.
    response.headers["Cache-Control"] = "public, max-age=86400" if at is not None else "no-store"
    return ShadeOut(
        city=runtime.config.id,
        at=when,
        state=result.state,
        in_shade=result.state is ShadeState.SHADE,
        shade_type=result.shade_type,
        sun=SunOut(azimuth_deg=sun.azimuth_deg, elevation_deg=sun.elevation_deg),
        attribution=runtime.metadata.attribution,
    )


@router.get("/shade/timeline", summary="Sun/shade intervals across one local day")
def timeline(
    registry: Registry,
    response: Response,
    city: str,
    lat: Lat,
    lon: Lon,
    day: Annotated[
        date | None,
        Query(alias="date", description="Local calendar day; omitted means today"),
    ] = None,
) -> TimelineOut:
    """Daylight intervals of constant state, plus shaded_until when date is today."""
    runtime, x, y = _locate(registry, city, lat, lon)
    now = datetime.now(runtime.tz)
    resolved_day = day if day is not None else now.date()
    scene, center_x, center_y = runtime.reader.scene_for(x, y)
    intervals = shade_timeline(scene, center_x, center_y, lat, lon, resolved_day, runtime.tz)
    is_today = resolved_day == now.date()
    # Past and future days are deterministic; today carries shaded_until,
    # which moves with the clock.
    response.headers["Cache-Control"] = (
        "public, max-age=60" if is_today else "public, max-age=86400"
    )
    return TimelineOut(
        city=runtime.config.id,
        date=resolved_day,
        timezone=runtime.config.timezone,
        intervals=[
            TimelineIntervalOut(
                from_=interval.start.strftime("%H:%M"),
                to=interval.end.strftime("%H:%M"),
                state=interval.state,
                in_shade=interval.state is ShadeState.SHADE,
                shade_type=interval.shade_type,
            )
            for interval in intervals
        ],
        shaded_until=shaded_until(intervals, now) if is_today else None,
        attribution=runtime.metadata.attribution,
    )


@health_router.get("/healthz", summary="Liveness probe")
def healthz(registry: Registry) -> HealthOut:
    return HealthOut(status="ok", cities=len(registry.all()))
