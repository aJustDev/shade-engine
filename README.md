# Shade Engine

Open-source urban shade engine. Given a point and a moment in time (or a time
range), it answers: is this spot in the shade, what casts that shade (a
building or vegetation), and for how long will it stay shaded.

First city: Cordoba, Spain. First use case: finding street parking in the
shade. The same engine now powers shaded pedestrian routing, and is designed
to reach thermal-comfort and climate-shelter maps.

Live at [shade.ajustino.dev](https://shade.ajustino.dev/docs) with real data
for Cordoba and Montilla, plus an interactive map at
[ajustino.dev/case-studies/shade-engine](https://ajustino.dev/case-studies/shade-engine).

## How it works

Instead of precomputing shadow maps for every date and hour (a combinatorial
explosion), the offline pipeline computes a single **horizon raster** per
city: for every pixel, the elevation angle that blocks the skyline in N
azimuth sectors (64 by default), derived from aerial LiDAR (PNOA, Spain's
national coverage). At query time the engine computes the sun's position with
pvlib and compares it against the stored horizon: if the sun sits below the
horizon angle for its azimuth, the point is in shade.

One precomputation per city, millisecond queries, valid for **any** instant,
not just precomputed ones.

The observer sits at street level (terrain elevation + 1.6 m) with obstacles
taken from the surface model, so a point under a tree canopy is judged from
under it and not from above it. Placing the observer on the surface model
instead would put them on top of the tree, or on the roof.

**What casts the shade** comes for free: the sweep already knows which cell
blocks each sector, so its land-cover class is stored alongside the angle. A
query is one pixel read, not a runtime ray-march.

## Repository layout

| Path        | Contents                                                    |
| ----------- | ----------------------------------------------------------- |
| `core/`     | Shared domain: solar geometry, horizon queries, city config |
| `pipeline/` | Offline CLI: LiDAR to raster artifacts (DSM, DTM, horizon)  |
| `api/`      | Public FastAPI service reading the precomputed artifacts    |
| `cities/`   | One YAML per city; adding a city means adding one file      |
| `deploy/`   | Production deploy script, Caddy site and backup job         |

Raster artifacts never live in git: they sit in `data/` (ignored) or in the
deployment's storage.

## Quick start

```bash
uv sync --all-packages          # install the workspace
uv run pytest                   # tests
uv run ruff check . && uv run mypy
```

Building a city needs LiDAR tiles and takes hours. The short version:

```bash
uv run shade-engine area cordoba drawn.geojson   # price a computation area
uv run shade-engine build cordoba     # DSM, DTM, land cover, horizon
uv run shade-engine verify cordoba    # integrity + physical sanity checks
uv run shade-engine tiles cordoba     # PMTiles overlays for the viewer
uv run shade-engine graph cordoba     # pedestrian graph for routing
```

`build` and `tiles` take `--workers N`; the output is identical whatever the
count.

Then point the API at the artifacts:

```bash
SHADE_API_ARTIFACTS_ROOT=data/cities uv run uvicorn shade_api.app:create_app --factory
```

## Adding a city

One YAML in `cities/` plus one pipeline run. The file declares the projected
CRS (UTM for Spain), the bounding box in metres, pixel resolution, the number
of azimuth sectors and the sweep radius.

Cities are not rectangles, so the bounding box can carry an optional `area`
polygon: the box still sets the georeference, the polygon says which pixels
inside it are worth computing. `shade-engine area` prices a drawn polygon
before anything is built -- the sweep tiles it skips at each tile size, the
minutes and memory, the LiDAR tiles still missing -- and the build writes a
`coverage.tif` alongside the rasters, because outside the area the horizon
cube is zeros and zero means open sky. Everything that answers a question
reads it: a point with no data is refused, never called sunny.

The important constraint: **all computation happens in a projected CRS**,
never in degrees. Distances in degrees are meaningless, and Web Mercator
distorts them badly at Spanish latitudes. Serving is a different matter, and
uses EPSG:4326 / 3857.

## API

| Endpoint                 | What it answers                              |
| ------------------------ | -------------------------------------------- |
| `GET /v1/cities`         | Cities with built artifacts                  |
| `GET /v1/shade`          | Shade verdict for a point at an instant      |
| `GET /v1/shade/timeline` | Sun/shade intervals across one local day     |
| `GET /v1/parking/nearby` | Parking zones near a point, with shade state |
| `GET /v1/routes/shaded`  | Shaded vs shortest walking route             |

Expected error statuses are part of the contract, not failures: `400` when a
point falls outside artifact coverage, `404` for an unknown city, `503` when a
city lacks the artifact an endpoint needs. OpenAPI schema at
[`/openapi.json`](https://shade.ajustino.dev/openapi.json).

## Shaded routing

`GET /v1/routes/shaded` returns both the shaded route and the shortest one, so
the trade-off is always visible. Edge cost is:

```
cost = length * (1 + alpha * sun_fraction + beta * non_vegetal_shade_fraction)
```

Tree shade never gets a bonus, only a smaller penalty. That is not a style
choice: costs below the edge length would break the admissibility of the A\*
heuristic and the returned route could stop being optimal.

Sun fractions are precomputed per edge against 83 canonical solar instants, so
routing needs no raster reads at query time.

## Status

Cordoba and Montilla in production. Shade queries, timelines, parking and
routing all live. Field validation against photographs is still pending.

## Known limitations

**Crowns are modelled as opaque all year round.** A pixel under the canopy mask
is reported as shaded whenever the sun is up, and the horizon cube stores the
crown as a solid column. Under a deciduous tree in winter both are wrong in the
same direction: the engine promises shade that the bare branches do not cast.

Measured on the Montilla test crop: on the winter solstice a point under a crown
is reported shaded for all 9.5 hours of daylight, where the same skyline with
the vegetation removed would give 2.7. How much of that gap is real error
depends on the deciduous fraction of a city's canopy, which the engine does not
know -- so this is declared and not corrected. Fixing it needs a second horizon
cube built over a winter surface, and that needs per-tree species attribution
the artifacts do not carry today.

The API says so where it matters: `/v1/shade` and `/v1/shade/timeline` return a
`caveats` field, populated exactly when the vegetation is the only thing holding
the shade (`shade_type: "vegetation"`), and `GET /v1/cities/{id}` always carries
it. Tile clients get the same string in the manifest's `model_caveats`.

## Data sources and attribution

Raster artifacts are derived from PNOA LiDAR point clouds (third coverage,
2022-2025) distributed by Spain's CNIG under CC-BY 4.0. The required
derived-work attribution is:

    Obra derivada de PNOA-cob3 2022-2025 CC-BY 4.0 scne.es

Any service built on these artifacts must retain it; the API returns each
city's attribution in an `attribution` field, sourced from the artifacts'
build metadata. The pedestrian graph is derived from OpenStreetMap data,
licensed under ODbL.

## Documentation

Design decisions, phase-by-phase history, runbooks and the didactic notes on
the geospatial concepts used here live in a separate, private repository. This
README is meant to be enough to understand, build and run the engine on its
own; if something essential is missing from it, that is a bug worth reporting.

## License

Code is MIT licensed. The data attribution requirements above still apply.
