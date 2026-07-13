# Shade Engine

Open-source urban shade engine. Given a point and a moment in time (or a time
range), it answers: is this spot in the shade, what casts that shade (a
building or vegetation), and for how long will it stay shaded.

First city: Cordoba, Spain. First use case: finding street parking in the
shade. The same engine is designed to later power shaded pedestrian routing
and climate-shelter maps.

## How it works

Instead of precomputing shadow maps for every date and hour (a combinatorial
explosion), the offline pipeline computes a single **horizon raster** per
city: for every pixel, the elevation angle that blocks the skyline in N
azimuth sectors (64 by default), derived from aerial LiDAR (PNOA, Spain's
national coverage). At query time the engine computes the sun's position with
pvlib and compares it against the stored horizon: if the sun sits below the
horizon angle for its azimuth, the point is in shade. One precomputation per
city, millisecond queries, valid for any instant.

The observer is placed at street level (terrain elevation + 1.6 m) with
obstacles taken from the surface model, so points under tree canopies and
next to buildings behave correctly.

## Repository layout

| Path        | Contents                                                    |
| ----------- | ----------------------------------------------------------- |
| `core/`     | Shared domain: solar geometry, horizon queries, city config |
| `pipeline/` | Offline CLI: LiDAR to raster artifacts (DSM, DTM, horizon)  |
| `api/`      | Public FastAPI service reading the precomputed artifacts    |
| `cities/`   | One YAML per city; adding a city means adding one file      |
| `docs/`     | Spec, phased plan and learning notes (in Spanish)           |

## Status

Live at [shade.ajustino.dev](https://shade.ajustino.dev/docs) with real data
for Cordoba, Spain, and an interactive shade map at
[ajustino.dev/case-studies/shade-engine](https://ajustino.dev/case-studies/shade-engine)
(precomputed PMTiles overlays for the 2026 solstices and equinoxes). The spec
lives in [docs/shade-engine-mvp.md](docs/shade-engine-mvp.md) and the phased
implementation plan in [docs/plan.md](docs/plan.md) (both in Spanish).

## Roadmap (post-MVP)

- Shaded pedestrian routing (A\* over an OSM graph with a solar weight)
- Climate shelters / thermal comfort index
- Canopy porosity and a seasonal factor for deciduous trees
- Real-time parking availability
- More cities: the design already allows it (one YAML + one pipeline run,
  see [docs/adding-a-city.md](docs/adding-a-city.md))

## Data sources and attribution

Raster artifacts are derived from PNOA LiDAR point clouds (third coverage,
2022-2025) distributed by Spain's CNIG under CC-BY 4.0. The required
derived-work attribution is:

    Obra derivada de PNOA-cob3 2022-2025 CC-BY 4.0 scne.es

Any service built on these artifacts must retain it; the API returns each
city's attribution in an `attribution` field, sourced from the artifacts'
build metadata.

## License

Code is MIT licensed. The data attribution requirements above still apply.
