"""Response models: the public wire format of the API."""

from pydantic import BaseModel

from shade_core.artifacts import BuildMetadata


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


class HealthOut(BaseModel):
    status: str
    cities: int
