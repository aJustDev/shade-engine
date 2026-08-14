"""``GET /v1/routes/shaded``: A-to-B walking route minimizing sun exposure.

Division of labor: the frozen graph artifact answers "how sunny is each
street at that instant" (precomputed fractions, resolved through the
declination ladder), :mod:`shade_api.routing` answers "cheapest path"
(A* over ``length * (1 + alpha * sun_fraction)``, between points snapped
onto edges rather than to junctions), and this router only parses, snaps,
and serializes. The response always carries the shortest (alpha = 0) route
next to the shaded one: "1.42 km at 12% sun vs 1.31 km at 54%" is the
answer people actually want.

At night there is no sun to avoid: one A* over bare lengths, both legs
identical, ``status: "night"``.
"""

from datetime import datetime
from typing import Annotated, Any

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Response

from shade_api.registry import CityRuntime
from shade_api.routes import _AT_DESCRIPTION, Registry, _locate, resolve_at
from shade_api.routing import EdgePoint, RouteGraph, RouteLeg
from shade_api.schemas import RouteLegOut, RoutePointOut, ShadedRouteOut, SunOut
from shade_core.solar import sun_position

router = APIRouter(prefix="/v1")

SNAP_MAX_M = 400.0
DEFAULT_ALPHA = 1.0


def _parse_point(value: str, name: str) -> tuple[float, float]:
    """'lat,lon' -> (lat, lon), with the same bounds Lat/Lon enforce."""
    parts = value.split(",")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail=f"{name} must be 'lat,lon'")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be 'lat,lon'") from exc
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise HTTPException(status_code=400, detail=f"{name} out of lat/lon range")
    return lat, lon


def _snap(graph: RouteGraph, x: float, y: float, name: str) -> EdgePoint:
    point = graph.snap_point(x, y)
    if point.distance_m > SNAP_MAX_M:
        raise HTTPException(
            status_code=400,
            detail=f"no walkable path within {SNAP_MAX_M:.0f} m of {name}",
        )
    return point


def _point_out(runtime: CityRuntime, lat: float, lon: float, point: EdgePoint) -> RoutePointOut:
    """The requested point plus where it actually landed on the network."""
    snapped_lon, snapped_lat = runtime.to_projected.transform(point.x, point.y, direction="INVERSE")
    return RoutePointOut(
        lat=lat,
        lon=lon,
        snapped_lat=round(float(snapped_lat), 6),
        snapped_lon=round(float(snapped_lon), 6),
        snap_distance_m=round(point.distance_m, 1),
    )


def _leg_out(runtime: CityRuntime, leg: RouteLeg) -> RouteLegOut:
    lons, lats = runtime.to_projected.transform(leg.xs, leg.ys, direction="INVERSE")
    coordinates: list[list[float]] = [
        [float(lon), float(lat)] for lon, lat in zip(lons, lats, strict=True)
    ]
    geometry: dict[str, Any] = {"type": "LineString", "coordinates": coordinates}
    return RouteLegOut(
        geometry=geometry,
        length_m=round(leg.length_m, 1),
        sun_length_m=round(leg.sun_length_m, 1),
        sun_fraction=round(leg.sun_fraction, 3),
    )


@router.get("/routes/shaded", summary="Shaded vs shortest walking route")
def shaded_route(
    registry: Registry,
    response: Response,
    city: str,
    from_: Annotated[str, Query(alias="from", description="Origin as 'lat,lon', WGS84")],
    to: Annotated[str, Query(description="Destination as 'lat,lon', WGS84")],
    at: Annotated[datetime | None, Query(description=_AT_DESCRIPTION)] = None,
    alpha: Annotated[
        float,
        Query(
            ge=0.0,
            le=10.0,
            description=(
                "Detour appetite: an edge fully in the sun costs (1 + alpha) times "
                "its length, so alpha = 1 accepts up to a 2x detour for full shade; "
                "0 returns the shortest route"
            ),
        ),
    ] = DEFAULT_ALPHA,
) -> ShadedRouteOut:
    from_lat, from_lon = _parse_point(from_, "from")
    to_lat, to_lon = _parse_point(to, "to")
    runtime, from_x, from_y = _locate(registry, city, from_lat, from_lon)
    _, to_x, to_y = _locate(registry, city, to_lat, to_lon)
    graph = runtime.route_graph
    if graph is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"no pedestrian graph for {city}: run `shade-engine graph {city}` "
                "and redeploy its artifacts"
            ),
        )
    src = _snap(graph, from_x, from_y, "origin")
    dst = _snap(graph, to_x, to_y, "destination")

    when = resolve_at(at, runtime.tz)
    sun = sun_position(from_lat, from_lon, when)
    edge_len = graph.artifact.edge_len.astype(np.float64)
    if sun.is_up:
        fractions = graph.fractions_at(when)
        status = "ok"
    else:
        fractions = np.zeros(len(edge_len), dtype=np.float32)
        status = "night"

    shaded_spans = graph.astar_points(src, dst, edge_len * (1.0 + alpha * fractions))
    if shaded_spans is None:
        raise HTTPException(status_code=400, detail="no route between origin and destination")
    if not shaded_spans:  # both pins snapped to the same spot
        shaded_leg = shortest_leg = graph.point_leg(src)
    else:
        shaded_leg = graph.assemble_spans(shaded_spans, fractions)
        if alpha > 0.0 and status == "ok":
            shortest_spans = graph.astar_points(src, dst, edge_len)
            assert shortest_spans is not None  # same endpoints just proved reachable
            shortest_leg = graph.assemble_spans(shortest_spans, fractions)
        else:  # alpha = 0 or night: the shaded run already minimized length
            shortest_leg = shaded_leg

    response.headers["Cache-Control"] = "public, max-age=86400" if at is not None else "no-store"
    return ShadedRouteOut(
        city=runtime.config.id,
        at=when,
        status=status,
        alpha=alpha,
        sun=SunOut(
            azimuth_deg=round(sun.azimuth_deg, 2), elevation_deg=round(sun.elevation_deg, 2)
        ),
        origin=_point_out(runtime, from_lat, from_lon, src),
        destination=_point_out(runtime, to_lat, to_lon, dst),
        shaded=_leg_out(runtime, shaded_leg),
        shortest=_leg_out(runtime, shortest_leg),
        attribution=list(
            dict.fromkeys([*runtime.metadata.attribution, *graph.artifact.meta.attribution])
        ),
    )
