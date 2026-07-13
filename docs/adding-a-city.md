# Como anadir una ciudad

Anadir una ciudad al motor es un fichero YAML y ejecuciones de la pipeline;
no hay cambios de codigo. Este documento cubre el fichero de configuracion,
el formato de las capas vectoriales y el ciclo completo hasta produccion.

## 1. El fichero `cities/<id>.yaml`

Una ciudad es una unidad de despliegue descrita por un YAML en `cities/`
(schema validado: `core/src/shade_core/config.py::CityConfig`).

```yaml
id: cordoba # identificador (nombre del fichero y de la URL)
name: Cordoba
country: ES
timezone: Europe/Madrid # IANA; se valida al cargar
crs: EPSG:25830 # CRS PROYECTADO local (UTM): aqui se calcula todo
bbox: [341000, 4192000, 349000, 4199000] # min_x, min_y, max_x, max_y (METROS en el CRS local)
resolution_m: 1.0 # tamano de pixel de los artefactos
horizon_sectors: 64 # sectores de azimut del barrido
horizon_max_distance_m: 500 # radio del barrido; tambien acolcha el bbox
observer_height_m: 1.6 # ojos del observador sobre el terreno
sources:
  lidar: pnoa # driver de descarga (CnigSource); omitir si se
  pnoa_series: LIDA3 #   aportan LAZ locales con --lidar-dir
layers:
  parking: cities/cordoba/parking.geojson # rutas relativas al CWD del CLI
attribution:
  - Obra derivada de PNOA-cob3 2022-2025 CC-BY 4.0 scne.es
```

Puntos que suelen morder:

- `bbox` va en el CRS local (metros), no en lat/lon. Elegir la zona UTM que
  cubre la ciudad (ver `docs/learning/crs.md`).
- `attribution` es obligatorio moralmente: los artefactos derivados de PNOA
  exigen la atribucion CC-BY, y la API la sirve en cada respuesta.

## 2. Formato de capas: `parking.geojson`

Cada capa declarada en `layers:` es un GeoJSON editable en el repo (fuente
de verdad) que se importa a PostGIS. Para `parking`
(`pipeline/src/shade_pipeline/layers.py`):

- `FeatureCollection` de features con geometria `LineString` o
  `MultiLineString` en WGS84 (orden lon-lat, como manda GeoJSON).
- Properties requeridas (el import falla en alto si faltan): `name`,
  `zone_type` (`blue_zone` | `free` | `lot`), `schedule` (lista de
  `{days, from, to}`; vacia = siempre).
- Properties opcionales (NULL si faltan): `orientation`, `capacity`,
  `max_minutes`, `tariff_eur_hour`, `notes`, `source`, `last_verified`.

Re-importar reemplaza las filas de la ciudad en una transaccion: es
idempotente.

## 3. Construir los artefactos

```sh
uv run shade-engine build <id>              # descarga LiDAR (pnoa) o --lidar-dir
```

Deja en `data/cities/<id>/v1/`: `dsm.tif`, `dtm.tif`, `landcover.tif`,
`canopy.tif`, `horizon.tif`, `blocker_class.tif` (COGs) y `metadata.json`.
Los rasteres nunca van a git.

Artefactos construidos antes de que existiera la mascara de copa se
actualizan sin re-barrer el horizonte:

```sh
uv run shade-engine canopy <id>             # deriva canopy.tif de dsm/dtm/landcover
```

## 4. Tiles de visualizacion (opcional, Fase 7)

```sh
uv run shade-engine tiles <id>              # preset de estaciones 2026
uv run shade-engine tiles <id> --at 2026-08-01T19:30   # o instantes sueltos
```

Escribe `data/cities/<id>/v1/tiles/`: un `shade-<instante>.pmtiles` por
instante (overlay raster, zooms 12-17) y el manifest `index.json` que la web
consume. Instantes nocturnos se rechazan. Ver
`docs/learning/map-tiles-pmtiles.md`.

El basemap NO lo genera el CLI: es un extract de OSM via Protomaps, una
operacion manual unica por ciudad (CLI go de
github.com/protomaps/go-pmtiles):

```sh
pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles \
  data/cities/<id>/v1/tiles/basemap.pmtiles --bbox=<w>,<s>,<e>,<n>
```

(bbox en grados WGS84 con algo de margen; para cordoba se uso el build
20260712 con `--bbox=-4.83,37.84,-4.70,37.95`, 3 MB). Los glyphs y sprites
del estilo viven una sola vez en `data/cities/assets/` (copiados de
github.com/protomaps/basemaps-assets: fuentes Noto Sans y sprites `black`);
se sirven bajo `/tiles/assets/` como si "assets" fuera una ciudad mas.

## 5. Publicar en el servidor

Los DATOS van fuera de la pipeline de CI (convencion de Fase 6): rsync al
VPS y, si hay capas vectoriales, import dentro del contenedor.

```sh
rsync -a data/cities/<id>/ cartagena:/opt/shade/data/cities/<id>/
ssh cartagena "cd /opt/shade && docker compose run --rm api shade-engine import-layer <id> parking"
```

No hay que tocar nada mas:

- La API descubre la ciudad sola al ver su `metadata.json` (y su YAML, que
  llega por git con el deploy normal).
- Caddy tampoco cambia: `/tiles/*` replica el arbol de `data/cities`, asi
  que `https://shade.ajustino.dev/tiles/<id>/v1/tiles/index.json` funciona
  en cuanto el rsync termina.

Verificacion rapida: `/v1/cities` lista la ciudad; un `curl -I` al manifest
devuelve 200 con `access-control-allow-origin: *`; un GET con
`Range: bytes=0-16383` a un `.pmtiles` devuelve 206 `immutable`.

## 6. Regenerar tiles mas tarde

Re-ejecutar `shade-engine tiles` y repetir el rsync. Los `.pmtiles` se
cachean con `immutable`, pero el manifest lleva `?v=<epoch>` en cada URL y
caduca a los 5 minutos: los clientes ven los tiles nuevos sin purgar nada.
