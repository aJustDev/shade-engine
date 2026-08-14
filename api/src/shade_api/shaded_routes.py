"""``GET /v1/routes/shaded``: A-to-B walking route minimizing sun exposure.

Division of labor: the frozen graph artifact answers "how sunny is each
street at that instant" (precomputed fractions, resolved through the
declination ladder), :mod:`shade_api.routing` answers "cheapest path"
(A* over ``length * (1 + alpha * sun_fraction)``, between points snapped
onto edges rather than to junctions), and this router only parses, snaps,
and serializes. The response always carries the shortest (alpha = 0) route
next to the shaded one: "1.42 km at 12% sun vs 1.31 km at 54%" is the
answer people actually want.

``beta`` adds the second preference: tree shade cools far better than a
building's does (docs/learning/vegetation-cooling.md), so the weight is a
ladder of penalties rather than a bonus -- sun ``+alpha``, shade cast by
buildings or terrain ``+beta``, canopy free::

    cost = length * (1 + alpha * sun + beta * (1 - sun - canopy))

Every term is non-negative, so ``cost >= length`` holds and A* stays
optimal. Note the pair invariant weakens when ``beta > 0``: the shaded
route may take slightly more sun in exchange for walking under trees,
because that is what was asked for.

At night there is no sun to avoid: one A* over bare lengths, both legs
identical, ``status: "night"``.
"""

import math
from datetime import datetime
from typing import Annotated, Any, Final

import numpy as np
import numpy.typing as npt
from fastapi import APIRouter, HTTPException, Query, Response

from shade_api.registry import CityRuntime
from shade_api.routes import _AT_DESCRIPTION, Registry, _locate, resolve_at
from shade_api.routing import EdgePoint, EdgeSpan, RouteGraph, RouteLeg
from shade_api.schemas import (
    RouteAlternativeOut,
    RouteLegOut,
    RoutePointOut,
    ShadedRouteOut,
    SunOut,
)
from shade_core.solar import sun_position

router = APIRouter(prefix="/v1")

SNAP_MAX_M = 400.0
DEFAULT_ALPHA = 1.0
DEFAULT_BETA = 0.0
ALTERNATIVE_ALPHAS: Final = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0)
"""The alpha sweep behind ``alternatives=true``; see ``_sweep_alternatives``."""
MEANINGFUL_SUN_GAIN: Final = 0.05
"""An alternative must cut this share of the incumbent's sun to be offered."""


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
        veg_shade_length_m=round(leg.veg_shade_length_m, 1),
    )


def _sweep_alternatives(
    graph: RouteGraph,
    src: EdgePoint,
    dst: EdgePoint,
    edge_len: npt.NDArray[np.float64],
    fractions: npt.NDArray[np.float32],
    veg_fractions: npt.NDArray[np.float32],
    alpha: float,
    beta: float,
) -> list[tuple[float, RouteLeg]]:
    """Run the router across a spread of alphas and keep the distinct routes.

    Each alpha is one taste in "how far would you walk to dodge the sun",
    and its optimum is one point of the length/sun trade-off. Beta rides
    along at the same ratio the caller asked for, so tree preference does
    not drift as alpha grows. Routes repeat across neighboring alphas, so
    identical span sequences collapse, keeping the smallest alpha that
    produced them.
    """
    ratio = beta / alpha if alpha > 0.0 else 0.0
    other_shade = np.clip(1.0 - fractions - veg_fractions, 0.0, 1.0)
    seen: dict[tuple[tuple[int, float, float], ...], tuple[float, list[EdgeSpan]]] = {}
    for step in sorted({*ALTERNATIVE_ALPHAS, alpha}):
        cost = edge_len * (1.0 + step * fractions + (ratio * step) * other_shade)
        spans = graph.astar_points(src, dst, cost)
        if not spans:
            continue
        key = tuple((span.edge, round(span.s_from, 3), round(span.s_to, 3)) for span in spans)
        seen.setdefault(key, (step, spans))
    return [
        (step, graph.assemble_spans(spans, fractions, veg_fractions))
        for step, spans in seen.values()
    ]


def _pareto_front(entries: list[tuple[float, RouteLeg]]) -> list[tuple[float, RouteLeg]]:
    """Keep the routes that are a real choice, cheapest first.

    The sweep scalarizes two objectives into one number, and with beta > 0
    that number is not monotone in (length, sun): a route can come back
    both longer and sunnier than a sibling, which no one would ever pick.
    Sorting by length and keeping only improvements in sun leaves the
    non-dominated set.

    Improvements also have to be worth a row on screen. Neighboring alphas
    routinely differ by a couple of meters of sun over a kilometer, which
    is two identical-looking offers, so a route must cut the incumbent's
    sun by ``MEANINGFUL_SUN_GAIN`` to earn its place.
    """
    ranked = sorted(entries, key=lambda item: (item[1].length_m, item[1].sun_length_m))
    front: list[tuple[float, RouteLeg]] = []
    best_sun = math.inf
    for step, leg in ranked:
        margin = 1e-6 if not front else max(1e-6, MEANINGFUL_SUN_GAIN * best_sun)
        if leg.sun_length_m <= best_sun - margin:
            best_sun = leg.sun_length_m
            front.append((step, leg))
    return front


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
    beta: Annotated[
        float,
        Query(
            ge=0.0,
            le=10.0,
            description=(
                "Tree preference: shade cast by buildings or terrain costs "
                "(1 + beta) times its length while tree shade costs its bare "
                "length, so beta > 0 routes under canopy; must not exceed alpha"
            ),
        ),
    ] = DEFAULT_BETA,
    alternatives: Annotated[
        bool,
        Query(
            description=(
                "Also return the distinct routes an alpha sweep produces, "
                "each with its own length and sun accounting"
            )
        ),
    ] = False,
) -> ShadedRouteOut:
    if beta > alpha:
        raise HTTPException(
            status_code=400,
            detail=(
                f"beta ({beta:g}) must not exceed alpha ({alpha:g}): sun has to stay "
                "at least as unwelcome as building shade"
            ),
        )
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
        veg_fractions = graph.veg_fractions_at(when)
        status = "ok"
    else:
        fractions = np.zeros(len(edge_len), dtype=np.float32)
        veg_fractions = np.zeros(len(edge_len), dtype=np.float32)
        status = "night"

    # Shade cast by anything but a tree. The clip absorbs the 1/255 of
    # quantization error the two matrices can carry into the subtraction.
    other_shade = np.clip(1.0 - fractions - veg_fractions, 0.0, 1.0)
    cost = edge_len * (1.0 + alpha * fractions + beta * other_shade)
    shaded_spans = graph.astar_points(src, dst, cost)
    if shaded_spans is None:
        raise HTTPException(status_code=400, detail="no route between origin and destination")
    if not shaded_spans:  # both pins snapped to the same spot
        shaded_leg = shortest_leg = graph.point_leg(src)
    else:
        shaded_leg = graph.assemble_spans(shaded_spans, fractions, veg_fractions)
        if (alpha > 0.0 or beta > 0.0) and status == "ok":
            shortest_spans = graph.astar_points(src, dst, edge_len)
            assert shortest_spans is not None  # same endpoints just proved reachable
            shortest_leg = graph.assemble_spans(shortest_spans, fractions, veg_fractions)
        else:  # no preference or night: the shaded run already minimized length
            shortest_leg = shaded_leg

    scored: list[RouteAlternativeOut] | None = None
    if alternatives:
        if status == "ok" and shaded_spans:
            found = _pareto_front(
                _sweep_alternatives(
                    graph, src, dst, edge_len, fractions, veg_fractions, alpha, beta
                )
            )
        else:  # at night, or pins on one spot, there is nothing to trade off
            found = [(0.0, shortest_leg)]
        scored = [
            RouteAlternativeOut(alpha=step, **_leg_out(runtime, leg).model_dump())
            for step, leg in found
        ]

    response.headers["Cache-Control"] = "public, max-age=86400" if at is not None else "no-store"
    return ShadedRouteOut(
        city=runtime.config.id,
        at=when,
        status=status,
        alpha=alpha,
        beta=beta,
        sun=SunOut(
            azimuth_deg=round(sun.azimuth_deg, 2), elevation_deg=round(sun.elevation_deg, 2)
        ),
        origin=_point_out(runtime, from_lat, from_lon, src),
        destination=_point_out(runtime, to_lat, to_lon, dst),
        shaded=_leg_out(runtime, shaded_leg),
        shortest=_leg_out(runtime, shortest_leg),
        alternatives=scored,
        attribution=list(
            dict.fromkeys([*runtime.metadata.attribution, *graph.artifact.meta.attribution])
        ),
    )
