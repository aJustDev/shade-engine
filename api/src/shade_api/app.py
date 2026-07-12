"""Application factory. Run with ``uvicorn shade_api.app:app``.

``create_app`` takes settings explicitly so tests can build isolated apps;
the module-level ``app`` reads them from the environment. No IO happens at
import time -- the city registry is built inside the lifespan, so uvicorn
importing the module stays cheap and failures surface at startup.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import metadata as importlib_metadata

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from limits import parse as parse_rate_limit
from sqlalchemy.orm import sessionmaker

from shade_api.parking import router as parking_router
from shade_api.ratelimit import RateLimitMiddleware
from shade_api.registry import CityRegistry
from shade_api.routes import health_router, router
from shade_api.settings import ApiSettings
from shade_core.db import make_engine

_DESCRIPTION = (
    "Public API of shade-engine: urban shade queries answered from "
    "precomputed per-city horizon artifacts. Times without a UTC offset "
    "are interpreted in the city's timezone."
)


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    app_settings = settings if settings is not None else ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.registry = CityRegistry.load(app_settings)
        # No eager connect: shade endpoints must keep working with the
        # database down or absent; /v1/parking answers 503 in that case.
        engine = make_engine(app_settings.database_url) if app_settings.database_url else None
        app.state.db_sessionmaker = sessionmaker(engine) if engine is not None else None
        yield
        if engine is not None:
            engine.dispose()
        app.state.registry.close()

    app = FastAPI(
        title="shade-engine API",
        version=importlib_metadata.version("shade-api"),
        description=_DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    if app_settings.rate_limit_enabled:
        app.add_middleware(RateLimitMiddleware, limit=parse_rate_limit(app_settings.rate_limit))
    if app_settings.cors_origins or app_settings.cors_origin_regex:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=app_settings.cors_origins,
            allow_origin_regex=app_settings.cors_origin_regex,
            allow_methods=["GET"],
            allow_headers=["*"],
        )
    app.include_router(router)
    app.include_router(parking_router)
    app.include_router(health_router)
    return app


app = create_app()
