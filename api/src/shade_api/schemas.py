"""Response models: the public wire format of the API."""

from datetime import date, datetime

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


class HealthOut(BaseModel):
    status: str
    cities: int
