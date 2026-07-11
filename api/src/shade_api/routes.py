"""HTTP endpoints.

All endpoints are sync ``def``: the engine is synchronous (rasterio reads
under a lock), so FastAPI runs them in its threadpool and the event loop
never blocks.
"""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from shade_api.registry import CityRegistry, CityRuntime
from shade_api.schemas import CityDetail, CityOut, HealthOut

router = APIRouter(prefix="/v1")
health_router = APIRouter()


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


@health_router.get("/healthz", summary="Liveness probe")
def healthz(registry: Registry) -> HealthOut:
    return HealthOut(status="ok", cities=len(registry.all()))
