"""Response models: the public wire format of the API."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from shade_core.artifacts import BuildMetadata
from shade_core.shade import ShadeState, ShadeType


class CityOut(BaseModel):
    """One city with built artifacts, ready to answer shade queries."""

    id: str
    name: str
    country: str
    timezone: str
    bbox_wgs84: tuple[float, float, float, float]
    attribution: list[str]


class CityDetail(CityOut):
    """A city plus the build metadata of its loaded artifacts."""

    artifacts: BuildMetadata


class SunOut(BaseModel):
    """Sun position in the local sky; azimuth 0 = North, clockwise, degrees."""

    azimuth_deg: float
    elevation_deg: float


class ShadeOut(BaseModel):
    """Shade verdict for one point at one instant."""

    city: str
    at: datetime = Field(description="The instant answered for, in the city's timezone")
    state: ShadeState = Field(
        description=(
            "'night' when the sun is below the astronomical horizon -- a state "
            "the in_shade flag alone cannot express"
        )
    )
    in_shade: bool = Field(description="True exactly when state is 'shade'")
    shade_type: ShadeType | None = Field(
        description="What casts the shade; null in sun, at night or when unknown"
    )
    sun: SunOut
    attribution: list[str]


class TimelineIntervalOut(BaseModel):
    """A [from, to) stretch of constant state during daylight, local HH:MM."""

    from_: str = Field(serialization_alias="from", description="Local start time, HH:MM")
    to: str = Field(description="Local end time, HH:MM")
    state: ShadeState
    in_shade: bool
    shade_type: ShadeType | None


class TimelineOut(BaseModel):
    """Sun/shade intervals across one local calendar day."""

    city: str
    date: date
    timezone: str
    intervals: list[TimelineIntervalOut]
    shaded_until: datetime | None = Field(
        description=(
            "Only when the requested date is today and the point is currently "
            "shaded: the instant the current shaded run ends"
        )
    )
    attribution: list[str]


class ScheduleEntryOut(BaseModel):
    """One regulated stretch; days is a compact range like 'mo-fr' or 'sa'."""

    days: str
    from_: str = Field(serialization_alias="from", description="Local start time, HH:MM")
    to: str = Field(description="Local end time, HH:MM")


class ParkingShadeOut(BaseModel):
    """Aggregate shade verdict of a zone, sampled along its geometry."""

    state: ShadeState = Field(
        description="Majority verdict: 'shade' when at least half the samples are shaded"
    )
    in_shade: bool = Field(description="True exactly when state is 'shade'")
    shade_fraction: float | None = Field(
        description="Fraction of sampled points in shade; null at night"
    )
    shaded_until: datetime | None = Field(
        description=(
            "Only while in_shade: when the zone drops below majority shade "
            "(or daylight ends, whichever comes first)"
        )
    )


class ParkingZoneOut(BaseModel):
    """One parking zone near the query point."""

    name: str
    zone_type: str
    orientation: str | None
    capacity: int | None
    schedule: list[ScheduleEntryOut]
    max_minutes: int | None
    tariff_eur_hour: float | None
    notes: str | None
    source: str | None
    last_verified: str | None
    distance_m: float = Field(description="Distance from the query point, meters")
    geometry: dict[str, Any] = Field(description="GeoJSON MultiLineString, WGS84 lon-lat")
    shade: ParkingShadeOut | None = Field(
        description="Null when the zone lies outside the city's raster coverage"
    )


class ParkingNearbyOut(BaseModel):
    """Parking zones within a radius, nearest first, with their shade state."""

    city: str
    at: datetime = Field(description="The instant answered for, in the city's timezone")
    radius_m: float
    zones: list[ParkingZoneOut]
    attribution: list[str]


class RoutePointOut(BaseModel):
    """A route endpoint: as requested, and where it landed on the graph."""

    lat: float
    lon: float
    snapped_lat: float = Field(description="Where the point met the network, WGS84")
    snapped_lon: float = Field(description="Where the point met the network, WGS84")
    snap_distance_m: float = Field(
        description="Distance from the requested point to the snapped point on the network, meters"
    )


class RouteLegOut(BaseModel):
    """One walking route with its sun exposure accounting."""

    geometry: dict[str, Any] = Field(description="GeoJSON LineString, WGS84 lon-lat")
    length_m: float
    sun_length_m: float = Field(description="Meters of the route walked in the sun")
    sun_fraction: float = Field(
        description="Length-weighted fraction of the route in the sun; 0 at night"
    )


class ShadedRouteOut(BaseModel):
    """Shade-optimized walking route plus the shortest route as reference."""

    city: str
    at: datetime = Field(description="The instant answered for, in the city's timezone")
    status: str = Field(description="'ok', or 'night' (no sun: both routes are the shortest)")
    alpha: float = Field(
        description=(
            "Detour appetite used: an edge fully in the sun costs (1 + alpha) times its length"
        )
    )
    sun: SunOut
    origin: RoutePointOut
    destination: RoutePointOut
    shaded: RouteLegOut
    shortest: RouteLegOut = Field(description="The alpha = 0 route, for comparison")
    attribution: list[str]


class HealthOut(BaseModel):
    status: str
    cities: int
