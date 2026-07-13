# Shade Engine - Plan de implementacion por fases

Documento vivo. Cada sesion de trabajo toma items de la fase activa, los marca al
completarlos y anota decisiones en el registro del final. El spec de referencia es
[shade-engine-mvp.md](shade-engine-mvp.md).

## Estado global

| Fase | Nombre                             | Estado    |
| ---- | ---------------------------------- | --------- |
| 0    | Bootstrap del repo                 | hecha     |
| 1    | core/: modelo solar + horizonte    | hecha     |
| 2    | pipeline/: de LAZ a artefactos COG | hecha     |
| 3    | api/: consulta de sombra (sin DB)  | hecha     |
| 4    | Cordoba real + validacion de campo | hecha     |
| 5    | Parking                            | hecha     |
| 6    | Despliegue en cartagena            | hecha     |
| 7    | Visualizacion + integracion Astro  | hecha     |
| 8    | Rutas peatonales a la sombra       | pendiente |

Estados: pendiente / en curso / hecha.

## Apuntes tecnicos incorporados al plan

Salidos del analisis inicial del spec (sesion 2026-07-10). Los items de fase ya los reflejan;
se listan aqui para no perder el porque.

1. **DTM + altura de observador.** El horizonte se calcula con el observador a nivel de calle
   (DTM + ~1.6 m) y los obstaculos desde el DSM. Calcularlo desde el DSM da error en pixeles
   bajo copa (el observador quedaria encima del arbol) y sobre tejados. El PNOA da el DTM
   gratis (clase LiDAR 2 = suelo). Regla para pixel bajo copa: landcover=vegetacion encima ->
   sombra vegetal siempre que el sol este sobre el horizonte astronomico (coherente con el
   supuesto de copa opaca).
2. **max_distance en el barrido de horizonte.** Con sol bajo las sombras son muy largas (un
   edificio de 30 m a 5 grados de elevacion proyecta ~340 m). El barrido lleva radio maximo
   configurable (500 m - 1 km) y tiling con buffer de ese tamano, disenado desde el principio:
   son ~10^8 pixeles x 64 sectores. Consecuencia documentada: se truncan angulos de horizonte
   muy bajos (irrelevante para el caso de uso de aparcamiento).
3. **Clasificacion del tipo de sombra: decision abierta (Fase 2).** (a) ray-march en runtime
   sobre el landcover en direccion al sol (default del spec, barato en disco) vs (b) segundo
   raster de 64 bandas con la clase del bloqueador dominante por sector (duplica almacenamiento,
   runtime trivial). Elegir al implementar y anotar en el registro de decisiones.
4. **Postgres se pospone a Fase 5.** Core, pipeline y API de sombra no necesitan DB:
   `/v1/cities` sale de los YAML + metadatos de artefactos y la sombra sale de los COGs.
5. **Pipeline contenerizado.** PDAL es C++ con bindings que viven en conda-forge; pip no es
   fiable. El pipeline corre en Docker (o pixi/conda) desde el principio.

Para el roadmap, no MVP: el GPS urbano tiene error de 5-10 m y a 1 m/pixel la respuesta puede
cambiar pixel a pixel; considerar agregado de vecindario o campo de confianza en la respuesta.

---

## Fase 0 - Bootstrap del repo

Objetivo: esqueleto trabajable con CI verde.

- [x] git init + LICENSE (MIT) + README con vision y roadmap (seccion 11 del spec)
- [x] Estructura monorepo: `api/`, `pipeline/`, `core/`, `cities/`, `tests/`, `docs/learning/` (`docker/` llegara con su primera pieza)
- [x] Tooling: uv (workspace con 3 paquetes src layout), ruff, mypy strict, pytest, pre-commit
- [x] CLAUDE.md del repo con las instrucciones didacticas de la seccion 10 del spec
- [x] Verificar wheels Python 3.14: rasterio/shapely/pyproj/numpy publican cp314 -> 3.14 en todo el workspace; PDAL solo sdist -> contenedor en Fase 2
- [x] CI GitHub Actions: lint + format + mypy + pytest (setup-uv pineado a tag completo, no hay major flotante)
- [x] docker-compose dev minimo -> APLAZADO a la fase que lo necesite (Fase 2/5): sin DB ni servicios aun, un compose vacio es ruido
- [x] Incorporar al spec los apuntes aceptados (DTM/observador, max_distance)
- [x] Extra no planificado: modelo `CityConfig` (pydantic) en core + `cities/cordoba.yaml` + 4 tests, para que el test de CI sea real y no un placeholder

Criterio de salida: CUMPLIDO 2026-07-10. CI verde en https://github.com/aJustDev/shade-engine (run 29122034951).

## Fase 1 - core/: modelo solar + consulta de horizonte

Objetivo: motor de sombra correcto sobre rasteres sinteticos.

- [x] Modulo solar sobre pvlib (`core/solar.py`): azimut 0=N horario, elevacion APARENTE (refraccion); datetime naive -> ValueError; barrido vectorizado del dia
- [x] Lectura de horizonte (`core/horizon.py`): `HorizonGrid` en memoria, interpolacion azimutal lineal circular, muestreo espacial nearest
- [x] `is_shaded` (`core/shade.py`): observador DTM+1.6m; pixel bajo copa -> sombra vegetal si es de dia; estados sun/shade/night
- [x] Timeline diario: barrido con paso configurable (default 5 min), fusion de intervalos contiguos, solo horas de luz
- [x] Golden test: cubo de 20 m, solsticios via formula geometrica independiente (sombra = 18.4/tan(elev)); 29 tests en total
- [x] Segundo sintetico con "arbol": bajo copa -> vegetation; sombra proyectada clasificada por ray-march
- [x] docs/learning: solar-geometry.md, horizon-algorithm.md, dsm-dtm-chm.md + crs.md ampliado con el porque de cada proyeccion
- [x] Extra no planificado: `compute_horizon_reference` (fuerza bruta) en core como oraculo de la version vectorizada de Fase 2

Criterio de salida: CUMPLIDO 2026-07-10. 29 tests verdes en CI; timeline de invierno reproduce sol -> sombra(building) -> sol con amanecer/atardecer correctos.

## Fase 2 - pipeline/: de LAZ a artefactos COG

Objetivo: `shade-engine build <city>` produce artefactos validos desde LiDAR PNOA.

- [x] CLI con typer; carga de config YAML de ciudad (`cities/cordoba.yaml` como en spec seccion 4)
- [x] Driver de descarga PNOA -> CAMBIADO: interfaz `LidarSource` + driver de directorio local con verificacion de cobertura del bbox acolchado; el downloader CNIG se MUEVE a Fase 4 (el centro de descargas no documenta API; endpoints internos fragiles, verificado 2026-07-11)
- [x] DSM (primeros retornos) + DTM (clase 2 + fillnodata), 1 m/pixel configurable -> con laspy+lazrs y binning numpy, SIN PDAL (decision revertida, ver registro)
- [x] Raster landcover (building/vegetation/ground) desde clases LiDAR (clase del punto que fija el DSM de cada celda)
- [x] Raster de horizonte: 64 bandas uint8 cuantizado (90/255 deg, escala en tag), observador en DTM+1.6m, obstaculos DSM, tiling con buffer ceil(max_distance/res), bit-identico al oraculo de core en modo exact
- [x] DECISION apunte 3 RESUELTA: raster de clase de bloqueador por sector (`blocker_class.tif`, 255 = cielo), generado por el argmax del mismo barrido; el ray-march queda como oraculo de paridad en tests
- [x] Export COG (deflate) + `metadata.json` versionado (`data/cities/<id>/v1/`); disco local en dev; loader en `shade_core.artifacts`
- [x] Fixture LAZ generado en test con laspy (sin binarios en git) + e2e en CI: LAZ -> build -> COGs -> golden queries desde disco
- [x] Dockerfile del pipeline -> APLAZADO: sin PDAL el pipeline es pip puro (uv lo instala entero); Dockerfile cuando el despliegue lo pida
- [x] docs/learning: lidar.md (retornos y clases), cog.md; ampliados horizon-algorithm.md (produccion) y dsm-dtm-chm.md (binning)

Criterio de salida: CUMPLIDO 2026-07-11. `shade-engine build cube` sobre LAZ sintetico produce los 5 COGs + metadata que core carga y responde los golden tests; 68 tests verdes.

## Fase 3 - api/: consulta de sombra (sin DB)

Objetivo: API publica de sombra leyendo COGs.

- [x] FastAPI + settings por env (`SHADE_API_*`, pydantic-settings); sin Postgres (apunte 4); `create_app(settings)` como factory testeable
- [x] `GET /v1/cities` (solo ciudades CON artefactos; YAML sin build se salta con warning) + `GET /v1/cities/{id}` con el BuildMetadata completo
- [x] `GET /v1/shade` y `GET /v1/shade/timeline` (con `shaded_until` si la fecha es hoy, fusionando rachas de sombra contiguas)
- [x] `/healthz` + endpoint de metadatos de artefactos cargados (es `/v1/cities/{id}`)
- [x] Lectura COG por ventana con cache LRU acotado por config -> `shade_core.artifacts.SceneReader`: bloques alineados de 64 px como ShadeScene locales, snap a centro de pixel (ver registro)
- [x] CORS por env, rate limiting, campo `attribution` (desde metadata.json), versionado `/v1` -> slowapi DESCARTADO en ejecucion: incompatible con fastapi >= 0.139 (ver registro); middleware propio sobre `limits`
- [x] Semantica de timezone: ISO 8601, sin offset -> TZ de la ciudad (`resolve_at`, un unico punto de resolucion; core sigue rechazando naive)
- [x] Cache-Control: `at` explicito y fechas no-hoy -> public max-age=86400; "ahora" implicito -> no-store; timeline de hoy -> max-age=60 (shaded_until se mueve con el reloj)
- [x] Tests de integracion contra artefactos del fixture (movido a coordenadas UTM reales de Cordoba para que lat/lon funcione de verdad); OpenAPI como doc publica

Criterio de salida: CUMPLIDO 2026-07-11. API respondiendo sobre los artefactos del fixture (goldens invierno/verano/noche via lat/lon reales, timeline coherente, 429 y CORS verificados tambien con uvicorn+curl); 103 tests verdes.

## Fase 4 - Cordoba real + validacion de campo

Objetivo: la mejor demo posible: prediccion vs realidad.

- [x] Driver de descarga PNOA (movido desde Fase 2): envolver los endpoints internos del centro de descargas CNIG tras la interfaz `LidarSource`, con fallback documentado de descarga manual al directorio local -> `shade_pipeline.cnig` (CnigSource): resumible, probado en vivo (16 tiles, 965 MB, cero incidencias)
- [x] Ejecutar pipeline con bbox urbano de Cordoba; medir tamano/tiempos (validar estimacion seccion 3 del spec; fallback 2 m/pixel o 32 sectores si excesivo; probar el modo geometric del barrido) -> HECHO 2026-07-12: build completo exact en 11h21m (90 tiles, 738M puntos); artefactos 2.4 GB (horizon 1.8 GB); verificado con `predict` (hoja coherente para los 10 puntos) y API en vivo (/v1/cities lista cordoba, /v1/shade responde); probe y modo geometric en el registro
- [x] Validacion de campo: puntos conocidos, fotos con hora vs prediccion; material para README -> kit completo (docs/validacion-cordoba.md, 10 puntos afinados + `shade-engine predict` funcionando sobre artefactos reales); el paseo con fotos se DIFIERE fuera de la fase (ver seccion Diferido)
- [x] Ajustar precision segun lo detectado (interpolacion, snapping de puntos que caen sobre edificio) -> lo detectable sin campo esta hecho (filtrado de ruido/solape/withheld, costuras mm de PNOA, pins afinados con OSM+landcover); ajustes adicionales quedan ligados al paseo diferido

Criterio de salida: predicciones correctas en la mayoria de puntos de contraste, documentado. -> CERRADA 2026-07-12 con el criterio REDEFINIDO: el contraste de campo se difiere (el paseo se retrasa semanas y no bloquea nada); la fase cierra con el motor verificado sobre datos reales (build 11h21m, hoja de predicciones fisicamente coherente, API en vivo). El contraste foto-vs-prediccion se documentara al ejecutar la tarea diferida.

## Diferido: validacion de campo de Cordoba (cola de Fase 4)

Sin fase asignada; idealmente tras el deploy de Fase 6 (validar con el movil
contra shade.ajustino.dev mejora el protocolo). No bloquea ninguna fase.

- [ ] Paseo de validacion: protocolo y hoja de docs/validacion-cordoba.md (regenerar la hoja con `shade-engine predict` para la fecha real); fotos con hora + tabla de resultados; material para el README
- [ ] Ajustes de precision que salgan del contraste (interpolacion, snapping, altura de observador)

## Fase 5 - Parking

Objetivo: caso de uso aparcamiento completo.

- [x] PostGIS en compose + SQLAlchemy 2 + Alembic (primera migracion); verificar compat PostGIS<->Postgres antes de fijar imagen -> HECHO 2026-07-12: `postgis/postgis:18-3.6` (tag verificado en Docker Hub, publicado 2026-07-06), modelo `ParkingZone` en `shade_core.db` tras extra `shade-core[db]`, migracion 0001 a mano, fixture de DB scratch + service container en CI
- [x] `shade-engine import-layer <city> parking` -> HECHO 2026-07-12: resuelve la capa via `layers:` del YAML (bloque nuevo en cordoba.yaml), EWKT + delete/insert transaccional idempotente; probado contra la DB dev (21 zonas)
- [x] Generar `parking.geojson` del centro de Cordoba (schema seccion 5.1 del spec) ->
      HECHO adelantado 2026-07-12: `scripts/parse_cordoba_parking.py` parsea el visor
      municipal archivado (21 zonas, 51 tramos, 1152 plazas; ver nota de fuentes)
- [x] `GET /v1/parking/nearby` con estado de sombra en `at` y `shaded_until` -> HECHO 2026-07-12: ST_DWithin/ST_Distance en metros (geography) + muestreo de cada zona cada 10 m en el CRS proyectado; fraccion de sombra + veredicto por mayoria (umbral 0.5) y `shaded_until` por barrido solar compartido por request; sin DB configurada la API arranca igual y el endpoint responde 503

Criterio de salida: consulta nearby devuelve tramos con sombra correcta contra timeline. -> CUMPLIDO 2026-07-12: test de paridad zona-vs-/v1/shade sobre capa sintetica del fixture (y re-consulta en `at=shaded_until` cambia el veredicto); en vivo, 14 zonas reales alrededor de Tendillas con estados coherentes al atardecer (interiores arbolados en sombra vegetal, cruces abiertos al sol; shaded_until = ocaso 21:45) contrastados punto a punto con /v1/shade.

## Fase 6 - Despliegue en cartagena

Objetivo: `shade.ajustino.dev` en produccion.

- [x] Dockerfile api multi-stage; compose prod: api + postgis + volumen local de COGs (sin minio)
      HECHO: Dockerfile en raiz (uv workspace, --all-packages, imagen unica api+CLI);
      compose.yml prod con db + migrate one-shot + api; COGs por rsync + bind mount :ro.
- [x] uvicorn con --proxy-headers y --forwarded-allow-ips; limites de workers/RAM/cache por config
      HECHO: workers via WEB_CONCURRENCY (2), mem_limit por servicio, cache por env si hace falta.
- [x] Caddy: subdominio + TLS; verificar cache CDN (Cloudflare, no CloudFront) con los Cache-Control de Fase 3
      HECHO: /etc/caddy/sites/shade.caddy, cert Let's Encrypt emitido; DNS-only (sin CDN
      delante): verificado que las cabeceras llegan intactas (86400 / no-store); el cacheo
      CDN queda condicionado a activar proxied en Cloudflare.
- [x] CORS prod: https://ajustino.dev y https://\*.ajustino.dev
      HECHO: SHADE_API_CORS_ORIGIN_REGEX anclada; apex y subdominio verificados en vivo.

Criterio de salida: API publica respondiendo con datos reales de Cordoba desde el VPS.
CUMPLIDO 2026-07-12: https://shade.ajustino.dev sirve /healthz, /v1/cities, /v1/shade
(cache correcto), /v1/parking/nearby (14 zonas en Tendillas radius 700, paridad
zona-vs-punto contrastada) y rate limit activo. Push-to-deploy via GH Actions operativo.

## Fase 7 - Visualizacion + integracion Astro

Objetivo: mapa de sombra consumible desde la web.

- [x] PMTiles estaticos de sombra a horas clave (o tiles PNG dinamicos; decidir y documentar)
      HECHO: decidido PMTiles estaticos (registro). `shade-engine tiles cordoba` genera 16
      instantes (solsticios + equinoccios 2026, 4 horas locales cada uno, ~10 MB por
      instante, zooms 12-17) + manifest index.json; Caddy los sirve estaticos bajo
      /tiles/\* con CORS, Range y cache immutable. Basemap Protomaps autoalojado
      (extract OSM 3 MB + glyphs/sprites, sin API keys)
- [x] Integracion en la web Astro externa
      HECHO: caso de estudio en ajustino.dev/case-studies/shade-engine (en/es) con
      consola MapLibre: overlay conmutable por estacion/hora, click -> estado +
      timeline del dia contra la API en vivo, capa de parking coloreada por
      shade_fraction; todo con fallback a fixtures (el build nunca toca la red)
- [x] docs: como anadir una ciudad, formato de capas
      HECHO: docs/adding-a-city.md (YAML campo a campo, schema del parking.geojson,
      build/import-layer/tiles, basemap manual, rsync y verificacion)

Criterio de salida: mapa de sombra visible en ajustino.dev.

CUMPLIDO 2026-07-13: https://ajustino.dev/case-studies/shade-engine pinta el mapa con
basemap OSM autoalojado y overlay de sombra por instante; tiles verificados por HTTPS
(manifest 200 + ACAO \*, Range 206 immutable sin content-encoding, preflight OPTIONS 204) y API consultada en vivo desde la pagina (CORS apex verificado).

## Fase 8 - Rutas peatonales a la sombra (boceto)

Objetivo: "quiero pasear por esta zona a tal hora: dame el recorrido con mas
sombra". En el spec era roadmap (seccion 11: A\* sobre grafo OSM con peso
solar); se adelanta aqui como boceto para planificarla en su propia sesion.
La parte cara ya existe: `SceneReader` responde punto+instante barato, asi
que la fase es "solo" un grafo con un peso solar.

- [ ] Grafo peatonal de Cordoba desde OSM (footway, pedestrian, path, steps,
      living_street, residential): extraccion con osmnx o pyrosm, cacheado
      como artefacto por ciudad (el grafo del casco cabe en memoria de sobra)
- [ ] Coste solar por arista: muestrear cada arista cada ~5 m contra los
      rasteres; coste = longitud \* (1 + alfa \* fraccion_al_sol(hora_salida))
- [ ] A\* con ese peso; endpoint `GET /v1/routes/shaded?from&to&at` (y quiza
      modo paseo: zona + duracion -> circuito)
- [ ] MVP evalua la sombra a la hora de salida: en un paseo de 30 min el sol
      se mueve ~7 grados; el coste variable durante el propio recorrido queda
      para despues si el error molesta

Datos medidos (sondeo 2026-07-12, osmnx sobre el bbox de artefactos 8x7 km,
network_type=walk): 12,951 nodos / 39,042 aristas, un solo componente conexo;
descarga+construccion 18.3 s; +223 MiB de RSS; GraphML 15.6 MiB; A\* con
networkx puro y heuristica de linea recta: mediana 10.1 ms, p90 39.1 ms
(rutas mediana 2.8 km); 1976 km de aristas -> ~395k puntos de muestreo a 5 m
para el precalculo solar.

Decisiones abiertas (para la sesion de planificacion):

- Motor de rutas: RESUELTA por el sondeo -- A\* en proceso (networkx). Con
  10 ms por ruta y el grafo entero en ~200 MiB no hay caso para pgRouting ni
  router externo. PostGIS (Fase 5) sigue siendo el sitio natural para
  persistir el grafo y las fracciones de sol si se precalculan.
- Precalculo de fraccion de sol por arista y franja de 15-30 min (395k
  consultas de pixel por franja, asumible) vs calculo perezoso por peticion
  con LRU (una ruta toca cientos de aristas). Decidir al implementar.
- DuckDB NO entra: PostGIS cubre los vectores en runtime y para extraer OSM
  de una ciudad bastan osmnx/Overpass. Reevaluar solo si algun dia se ingiere
  Overture/GeoParquet multi-ciudad (ahi si brilla duckdb-spatial).

Criterio de salida (provisional): entre dos puntos del casco a media tarde,
la ruta sombreada evita visiblemente las calles al sol frente al camino mas
corto, comprobable sobre el mapa de Fase 7.

## Transversal (todas las fases)

- Cada concepto geo nuevo: nota corta en `docs/learning/` en el mismo commit (spec seccion 10)
- Docstrings didacticos en `core/` (formulas, unidades, convenciones de signo)
- Decisiones tecnicas con alternativas: exponer opciones y porque, y anotarlas en el registro

---

## Registro de decisiones

| Fecha      | Decision                                                                                                                                                                                     | Porque                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-07-10 | Horizonte con observador en DTM+1.6m, obstaculos DSM                                                                                                                                         | Evita error bajo copa y sobre tejado (apunte 1)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 2026-07-10 | Postgres pospuesto a Fase 5                                                                                                                                                                  | Fases 0-4 no necesitan DB (apunte 4)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 2026-07-10 | Pipeline contenerizado desde el inicio                                                                                                                                                       | PDAL solo fiable via conda-forge (apunte 5)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 2026-07-10 | Licencia MIT                                                                                                                                                                                 | Eleccion del usuario; permisiva y minima                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| 2026-07-10 | README en ingles; docs/ y docs/learning/ en castellano                                                                                                                                       | Alcance OSS vs objetivo didactico personal                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 2026-07-10 | Python 3.14 en todo el workspace                                                                                                                                                             | Wheels cp314 verificados en PyPI para rasterio 1.5.0, shapely 2.1.2, pyproj 3.7.2, numpy 2.5.1; pvlib puro                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 2026-07-10 | Repo publico ya: github.com/aJustDev/shade-engine                                                                                                                                            | Unica forma de verificar el criterio "CI verde"                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 2026-07-10 | Commits en ingles (convencion en CLAUDE.md)                                                                                                                                                  | Coherencia con repo OSS publico en ingles                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 2026-07-10 | docker-compose aplazado a Fase 2/5                                                                                                                                                           | Sin DB ni servicios que orquestar todavia                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 2026-07-10 | Elevacion solar APARENTE (con refraccion)                                                                                                                                                    | Es el sol que se ve; relevante al amanecer/atardecer (~0.5 grados en el horizonte)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| 2026-07-10 | Horizonte: interpolacion azimutal lineal circular                                                                                                                                            | Nearest erraria hasta medio sector (~2.8 grados con 64), metros de frontera de sombra                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 2026-07-10 | Horizonte: muestreo espacial nearest, no bilinear                                                                                                                                            | Promediar perfiles a traves de una pared mezcla tejado con calle: angulos sin sentido fisico                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| 2026-07-10 | `compute_horizon_reference` (fuerza bruta) en core                                                                                                                                           | Oraculo para validar la version vectorizada/tileada del pipeline (Fase 2) sobre los mismos fixtures                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| 2026-07-10 | Tipo de sombra: ray-march a medio pixel + fallback al sector contribuyente                                                                                                                   | La interpolacion azimutal difumina bordes de obstaculo ~medio sector; en esa banda el tipo se atribuye al sector que aporto el angulo. Paso de medio pixel: uno entero salta esquinas                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 2026-07-11 | laspy + lazrs (pip puro) en vez de PDAL; REVIERTE "pipeline contenerizado" (2026-07-10)                                                                                                      | lazrs publica wheel cp314 y laspy es Python puro: pipeline entero instalable con uv, smoke test e2e en CI sin Docker. Solo necesitabamos binning, que numpy hace en pocas lineas. Dockerfile aplazado                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 2026-07-11 | Tipo de sombra en produccion: raster de clase por sector (cierra apunte 3)                                                                                                                   | El argmax del barrido ya sabe que celda bloquea cada sector: guardar su landcover cuesta casi nada y la consulta pasa a 1 lectura de pixel (vs 3 ventanas COG del ray-march). Deflate comprime clases casi gratis (24K el cubo). Ray-march conservado como oraculo de paridad                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 2026-07-11 | Driver PNOA aplazado a Fase 4; Fase 2 usa driver de directorio local                                                                                                                         | CNIG sin API documentada (visor con endpoints internos jQuery, fragiles). No bloquea el criterio de salida de la fase; en Fase 4 se intenta el scraper con fallback manual                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 2026-07-11 | Horizonte cuantizado a uint8 (90/255 deg) con la escala en tag del GeoTIFF                                                                                                                   | Error <= ~0.18 deg, muy por debajo del medio pixel del barrido; mitad de disco que uint16; el fichero es autodescriptivo                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| 2026-07-11 | Barrido de produccion: dedupe de offsets + tiling con buffer ceil(max_d/res)                                                                                                                 | Exacto tras el floor a 0 (prueba en docstring): bit-identico al oraculo en modo exact, memoria acotada por tile. El modo geometric (paso creciente) queda como knob para Fase 4, validado solo por cuantil                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 2026-07-11 | Lector por ventana en core (`SceneReader`), no en api                                                                                                                                        | Cada bloque LRU es una ShadeScene local: `is_shaded`/`shade_timeline` se reutilizan sin duplicar nada. Bloques de 64 px (dividen el tile COG de 512), ~1.3 MiB/bloque, techo por config                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 2026-07-11 | `scene_for` devuelve el centro del pixel como punto de consulta                                                                                                                              | El motor recalcula rowcol contra el origen LOCAL del bloque; en el borde el redondeo float puede dar indice -1 o fuera del bloque (500 en un punto valido). Con muestreo espacial nearest el snap es semanticamente gratis                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 2026-07-11 | Rate limiting: middleware propio sobre `limits`; slowapi DESCARTADO                                                                                                                          | slowapi 0.1.10 resuelve el handler buscando `.endpoint` en app.routes y fastapi >= 0.139 envuelve los routers en `_IncludedRouter` sin ese atributo: exime TODAS las rutas en silencio (lo cazo el test de 429). El middleware propio son ~15 lineas sobre el mismo motor                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 2026-07-11 | Fixture `built_city` movido a coordenadas UTM reales de Cordoba                                                                                                                              | La API recibe lat/lon: con origen (0,0) ningun lat/lon real cae en el fixture. Coordenadas ~4e6 ademas destapan bugs de georef que el origen cero enmascara. Los goldens solares de Fase 1 siguen valiendo (~37.87N)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 2026-07-11 | `/v1/cities` lista solo ciudades con artefactos; attribution desde metadata.json                                                                                                             | "Disponible" = consultable; un YAML sin build se salta con warning (cordoba hasta Fase 4). La atribucion sale del artefacto construido, no del YAML vivo: es la del dato que responde                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 2026-07-11 | CORS origins como CSV en env con `NoDecode`                                                                                                                                                  | pydantic-settings decodifica los campos lista como JSON ANTES de los validators; CSV es lo menos sorprendente para ops y NoDecode permite el validator before que lo trocea                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 2026-07-12 | Cordoba con PNOA 3a cobertura (LIDA3, vuelo 2024) y atribucion CC-BY de obra derivada                                                                                                        | 5 pt/m2 vs 1.5 de la 2a y un vuelo 2024 que refleja la ciudad que se validara en campo en 2026; formula abreviada del IGN (Obra derivada de PNOA-cob3 2022-2025 CC-BY 4.0 scne.es) en YAML, README y metadata                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 2026-07-12 | Driver CNIG: scraping de archivosSerie + POST descargaDir, resumible                                                                                                                         | Endpoints internos verificados en vivo (sin sesion ni captcha) pero sin contrato: fallo ruidoso con instrucciones de fallback manual; cache validado por magic LASF sobrevive cortes (limite documentado ~20/sesion anonima); seleccion y cobertura delegadas en LocalDirectory                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 2026-07-12 | Filtrado LiDAR: clases 7/18/12 y flags withheld/overlap fuera; synthetic se conserva                                                                                                         | El DSM es un max por celda: un punto espurio alto crea un obstaculo fantasma en el horizonte de 500 m a la redonda; synthetic marca puntos validos (suelo hidro-aplanado del Guadalquivir) y tirarlo agujerearia el DTM del rio                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 2026-07-12 | RAM del build: cubos de horizonte memmapped + COG por banda + sin copias float64                                                                                                             | Los cubos (~6.7 GB a escala ciudad) eran el pico; con respaldo en fichero el kernel pagina bajo presion. Probe: pico 1.26 GiB; ciudad estimada ~4.5 GiB, cabe en 11 GiB. Bit-identico. Descartados: COG incremental (driver CreateCopy-only) y bajar resolucion/sectores                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| 2026-07-12 | Cobertura: footprints bufferizados con mitre antes de la union (hallazgo del probe)                                                                                                          | Los tiles reales de PNOA cuantizan a mm y dejan costuras de 1 mm entre extents de puntos que unary_union nunca cierra; la tolerancia aplicada solo al perimetro del target no podia absorber huecos internos                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| 2026-07-12 | Probe 2x2 km: exact 48.7 min / geometric 16.8 min (2.9x); build canonico v1 en exact                                                                                                         | Geometric valido para iterar (p50/p90 identicos, p99 0.35 deg, 0.13% > 2 deg, blocker 99.4% igual; outliers por roce de esquina) pero la validacion de campo debe testear la fisica, no el atajo de muestreo. Extrapolado ciudad: exact ~11-12 h, geometric ~4 h                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 2026-07-12 | Fase 4 CERRADA sin el paseo; la validacion de campo pasa a tarea diferida                                                                                                                    | El paseo se retrasa semanas y mantener la fase abierta solo por el bloqueaba la lectura del plan. El motor quedo verificado sobre datos reales (build completo + predict coherente + API en vivo); el contraste foto-vs-prediccion mejora ademas tras el deploy de Fase 6 (movil contra la API publica). Seccion "Diferido" con los 2 items para que no se pierdan                                                                                                                                                                                                                                                                                                                                                                       |
| 2026-07-12 | Parking en PostGIS como `geography(MultiLineString, 4326)`; modelos en `shade_core.db` tras extra opcional `shade-core[db]`                                                                  | geography acepta METROS en ST_DWithin/ST_Distance (geometry 4326 filtraria en grados: la trampa de crs.md en SQL) y una tabla sirve N ciudades sin fijar un CRS local por fila. El extra mantiene el core base libre de DB (apunte 4). Indice GiST explicito (spatial_index=True de geoalchemy2 duplica DDL bajo alembic) y primera migracion a mano (autogenerate con geoalchemy2 exige helpers extra). Geometrias entran como EWKT (geoalchemy2 envuelve binds en ST_GeogFromText; GeoJSON crudo reventaria)                                                                                                                                                                                                                           |
| 2026-07-12 | compose en raiz (solo servicio db) y tests de DB contra database scratch: skip local sin server, raise si CI                                                                                 | El docker/ del spec llega en Fase 6 con el Dockerfile de la api; un dir para un yaml es ruido. La scratch (nombre unico + upgrade head + DROP FORCE) aisla pytest de los datos dev del compose y prueba las migraciones en cada corrida; el raise con CI seteado evita que un service container roto convierta los tests de DB en skips verdes. OJO postgres:18: el volumen va en /var/lib/postgresql, sin /data                                                                                                                                                                                                                                                                                                                         |
| 2026-07-12 | Sombra por zona en /v1/parking/nearby: muestreo cada 10 m en CRS proyectado + fraccion + veredicto por mayoria (0.5); sin shade_type a nivel de zona                                         | Una calle de 300 m no tiene UN estado: la fraccion informa y el umbral da un veredicto accionable; el shade_type agregado quedaba mal definido con muestras mixtas (edificio+arbol) y el detalle punto a punto ya lo da /v1/shade. shaded_until barre las posiciones solares restantes del dia (UNA llamada pvlib por request, compartida entre zonas; por-instante costaria ms de pandas cada una) y cierra tambien al acabar la luz, como shade_timeline. radius con tope 1000 m + LIMIT 50                                                                                                                                                                                                                                            |
| 2026-07-12 | SHADE_DATABASE_URL unica para API y CLI (validation_alias en ApiSettings + populate_by_name)                                                                                                 | Dos nombres para el mismo valor era ruido de ops. El alias puentea el prefijo SHADE*API*; populate_by_name mantiene la construccion por kwargs de los tests y de paso hace funcionar el nombre prefijado como fallback (el alias gana). Sin URL la API arranca igual y solo /v1/parking responde 503                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 2026-07-12 | Deploy: imagen construida EN el VPS desde clone en /opt/shade (sin registry); Dockerfile en RAIZ, no docker/ del spec; una sola imagen con TODO el workspace (--all-packages)                | Patron ya establecido en el VPS para apsis/geohazard (compose.yml prod en raiz + build local + tag :prod); un registry anadiria credenciales y latencia sin beneficio a esta escala. El root del workspace uv es virtual (package=false): sin --all-packages el sync no instala NADA. Incluir pipeline mete el CLI shade-engine en la imagen: el import de parking en prod es `docker compose run --rm api shade-engine import-layer ...`, sin uv en el VPS ni tuneles. Trampa real cazada: python:3.14-slim no trae libexpat1 y la wheel de rasterio lo enlaza sin vendorizarlo (unico apt-get de la imagen)                                                                                                                            |
| 2026-07-12 | Prod en cartagena: puertos loopback 8003 (api) / 5437 (db), migrate one-shot antes de servir, workers via WEB_CONCURRENCY, --forwarded-allow-ips "\*", COGs por rsync + bind mount :ro       | Todo se publica solo en 127.0.0.1: Caddy es el unico cliente, lo que hace seguro el "\*" (la IP origen dentro del contenedor es la gateway del bridge, no fijable). migrate corre como servicio con service_completed_successfully: la api nunca arranca contra un schema viejo. WEB_CONCURRENCY (default uvicorn de --workers) cumple "limites por config" sin rebuild; OJO: el rate limit es por worker (60/min x 2), y en la practica las conexiones secuenciales las gana casi siempre el mismo worker (accept race), asi que un cliente solo ve ~60/min. compose.yml GANA la precedencia a docker-compose.yml: el flujo dev lleva -f docker-compose.yml SIEMPRE (el ${VAR:?} del prod falla en seco sin .env como red de seguridad) |
| 2026-07-12 | DNS: Cloudflare DNS-only (nube gris), NO CloudFront (error del spec) ni proxied                                                                                                              | La infra real es Cloudflare; el spec se escribio pensando en AWS. DNS-only replica apsis/geohazard y deja a Caddy emitir Let's Encrypt sin interferencias. Sin CDN delante el item "verificar cache" se cumplio sobre las cabeceras (86400/no-store llegan intactas); si algun dia se activa proxied: cache real en el edge, pero Caddy vera la IP del edge de CF (trusted_proxies para el rate limit) y el cert inicial conviene emitirlo en gris                                                                                                                                                                                                                                                                                       |
| 2026-07-12 | Push-to-deploy con GH Actions calcado de la convencion del VPS: workflow_run sobre CI verde en main + gate SHADE_DEPLOY_ENABLED + clave ssh con forced command a /usr/local/bin/deploy_shade | La clave dedicada (restrict, no-pty, ...) solo puede ejecutar el script de deploy (fuente de verdad: deploy/deploy.sh; se instala con sudo install en el aprovisionamiento): fetch+reset a origin/main, build, migrate bloqueante, up api, smoke local; el workflow remata con smoke publico. Los DATOS quedan fuera de la pipeline (rsync de COGs e import-layer son operaciones manuales): la pipeline mueve codigo, no gigas                                                                                                                                                                                                                                                                                                          |
| 2026-07-13 | Visualizacion: PMTiles raster ESTATICOS por instante clave, no tiles PNG dinamicos; preset = solsticios + equinoccios 2026 x 4 horas locales (16 instantes)                                  | La sombra de un instante fijo es inmutable: cacheable para siempre y servible como fichero por Caddy, cero carga en la API del VPS compartido (el spec 9.1 ya inclinaba aqui; TiTiler/dinamico queda como roadmap si el servido crece). El raster de estado se calcula vectorizado con UN sol en el centro del bbox (variacion en 8 km ~0.07 deg, bajo el medio-quantum de 0.176) leyendo solo las 2 bandas de horizonte adyacentes al azimut (el cubo float32 entero serian ~14 GB); paridad pixel a pixel con is_shaded testeada (float64 en la comparacion, empates de sector en uint8 crudo). Equinoccios comparten horas a proposito: declinacion ~0 en ambos, el mapa lo hace visible                                              |
| 2026-07-13 | Piramide 12-17 (z17 = 0.94 m/px a lat 37.9 ~ nativo), PNG paleta con sol transparente, tiles en blanco omitidos salvo en min_zoom, tile_compression NONE                                     | z18 seria upsampling (el cliente ya overzooma). Trampas reales del writer pmtiles: finalize revienta con 0 entries (por eso min_zoom se escribe siempre; el dedupe guarda el PNG en blanco una vez), el orden ascendente de tileid mantiene clustered=True, y marcar GZIP en tiles PNG haria a los clientes "descomprimir" bytes que no lo estan. ~10 MB por instante, 158 MB los 16                                                                                                                                                                                                                                                                                                                                                     |
| 2026-07-13 | Servido de tiles: Caddy file_server bajo /tiles/\* replicando el arbol de data/cities (sin v1 hardcodeado), CORS \* con handler explicito de preflight OPTIONS, immutable + ?v= en manifest  | file_server da Range y ETag nativos, que es todo lo que un cliente PMTiles necesita. La trampa que justifica el handler: fetch() con cabecera Range NO es peticion CORS simple y dispara preflight, asi que sin OPTIONS el mapa funciona same-origin y falla SOLO cross-origin. Los .pmtiles se cachean un ano immutable; regenerar tiles no purga nada porque el manifest (max-age 300) lleva ?v=<epoch> en cada URL                                                                                                                                                                                                                                                                                                                    |
| 2026-07-13 | Basemap: extract Protomaps (OSM) autoalojado + glyphs/sprites self-hosted en /tiles/assets; en la web, @protomaps/basemaps tema black (protomaps-themes-base esta deprecado)                 | A escala de ciudad hacen falta calles y nombres; el land-110m de apsis/geohazard es escala mundial. El extract (build 20260712, bbox cordoba + margen, 3 MB) mantiene la regla de la web: sin tile servers de terceros ni API keys en runtime, atribucion OSM en el mapa. Vector y no raster para tenirlo con la estetica del sitio sin regenerar nada. Operacion manual unica por ciudad, documentada en adding-a-city.md                                                                                                                                                                                                                                                                                                               |

Pendientes de decidir:

- Motor de rutas y estrategia de precalculo solar (Fase 8): boceto en su seccion

## Notas entre sesiones

- 2026-07-10: Fase 0 completada y pusheada. El dato `name: Cordoba` en cities/cordoba.yaml
  va sin tilde (regla ASCII); si se quiere tilde de cara a la API, cambiarlo entonces.
- 2026-07-10: Fase 1 completada. Siguiente: Fase 2 (pipeline/). Notas para entonces:
  - rasterio NO se anadio aun a shade-core (Fase 1 quedo todo en memoria); anadirlo cuando
    core tenga que leer COGs, junto con la variante de `HorizonGrid` respaldada por fichero.
  - La version vectorizada del horizonte debe validarse contra `compute_horizon_reference`
    sobre los fixtures de tests/synthetic.py (tolerancia: discretizacion de medio pixel).
  - Trampa descubierta: fixtures sinteticos con numeros redondos crean geometrias de medida
    cero (rayo que roza justo la esquina del cubo) donde dos muestreos correctos discrepan;
    los puntos de consulta de test van desplazados del eje de simetria (synthetic.QUERY_X).
  - La decision abierta del apunte 3 (ray-march vs bandas por sector) tiene ya un dato: el
    ray-march runtime funciona pero necesito DSM+DTM+landcover en memoria/ventana; para la
    API sobre COGs eso son 3 lecturas extra por consulta. Evaluar en Fase 2 con I/O real.
- 2026-07-11: Fase 2 completada. Siguiente: Fase 3 (api/). Notas para entonces:
  - `shade_core.artifacts.load_scene` hace lecturas COMPLETAS de los COGs; la API necesita
    la variante por ventana (1 pixel de horizon + blocker_class + landcover por consulta)
    con cache LRU acotado por config. El contrato georef ya esta validado en el loader.
  - La clasificacion via `ShadeScene.sector_classes` (sector contribuyente) ya vive en core
    y tiene test de paridad contra el ray-march; la API no necesita DSM/DTM para clasificar.
  - `shade-engine build` existe como entry point instalado; para la API basta apuntar
    ARTIFACTS_ROOT a `data/cities/<id>/v1`. Los tests e2e muestran el patron de fixture.
  - Trampa nueva documentada: dos discretizaciones correctas del mismo barrido (exact vs
    geometric) discrepan decenas de grados en pixeles sueltos por roce de esquina; el modo
    geometric se valida por cuantil, nunca contra el oraculo con tolerancia estricta.
  - El campo `sources.lidar: pnoa` del YAML de Cordoba es informativo todavia: el unico
    driver real es el directorio local (--lidar-dir). El scraper CNIG queda para Fase 4.
- 2026-07-11: Fase 3 completada. Siguiente: Fase 4 (Cordoba real). Notas para entonces:
  - Para servir Cordoba basta el build: la API ya la listara sola cuando exista
    `data/cities/cordoba/v1/metadata.json` (el registry salta YAMLs sin artefactos).
    Con el bbox real de 8x7 km habra que medir el coste del build y probar el modo
    geometric del barrido (item ya en Fase 4).
  - Defaults del cache de la API pensados para el fixture: `SHADE_API_BLOCK_SIZE=64` y
    `SHADE_API_MAX_CACHED_BLOCKS=64` (~84 MiB/ciudad de techo). Revisar con la ciudad
    real y la RAM del VPS (Fase 6 los baja por env si hace falta).
  - Para Fase 6 (deploy): el rate limiting es en memoria y por worker, y la key es la IP
    directa del socket -- detras de Caddy hace falta uvicorn --proxy-headers y
    --forwarded-allow-ips (item ya en Fase 6). /healthz comparte el limite por defecto
    (key por IP y path); si el monitoreo aprieta, eximirlo entonces.
  - fastapi >= 0.139 rompio la integracion de slowapi (ver registro); si algun dia se
    quiere slowapi de vuelta, verificar antes que su middleware encuentra los endpoints.
  - Snapping de puntos que caen sobre edificio (item de Fase 4): la API responde hoy la
    verdad del pixel (un lat/lon sobre tejado da el horizonte del tejado). El agregado
    de vecindario/confianza sigue en roadmap, no MVP.
- 2026-07-12: Fase 4 en curso. Hecho: driver CNIG, filtrado de puntos espurios, cubos
  memmapped, --step-mode y progreso en el CLI, comando predict + kit de validacion,
  probe del casco medido (registro de decisiones). Para cerrar la fase:
  - Lanzar el build completo: `uv run shade-engine build cordoba` (exact por defecto;
    ~11-12 h, mejor de noche; descarga ~90 tiles ~7 GB al cache `data/lidar/cordoba`,
    resumible si el limite de ~20/sesion corta: re-ejecutar reanuda; pico RAM ~4.5 GiB;
    ~16 GB de disco en el pico del scratch; artefactos finales ~2.5 GB -- la estimacion
    "cientos de MB" del spec seccion 3 se queda corta con dato real: el horizonte urbano
    comprime peor que el sintetico). La API listara cordoba sola al terminar.
  - Antes del paseo: los pins del kit ya estan afinados (OSM + landcover del probe;
    ver aviso en docs/validacion-cordoba.md con los 3 que quedan por confirmar a mano)
    y la hoja se regenera con
    `uv run shade-engine predict cordoba docs/validacion-cordoba-puntos.csv --day <fecha>`.
  - La fase cierra contrastando fotos con la hoja (tabla de resultados en
    docs/validacion-cordoba.md) y decidiendo los ajustes de precision que salgan.
  - Los 16 tiles del probe quedan en `data/lidar/cordoba` (se reutilizan); los
    artefactos del probe estaban en el scratchpad de la sesion (efimeros, no cuentan).
- 2026-07-12 (decision de secuencia): el paseo de validacion se retrasa unas semanas y
  NO bloquea nada mas. La Fase 4 queda "en curso" solo por su cola de validacion
  (fotos + ajustes de precision) y las fases siguientes arrancan sin esperar, en el
  orden del plan: 5 (parking) -> 6 (deploy) -> 7 (visualizacion). Razones: ninguna
  depende de la validacion, el contrato de la API no cambia aunque la validacion
  fuerce un rebuild de artefactos, y desplegar ANTES del paseo lo mejora (validar
  con el movil contra shade.ajustino.dev en vez de con hojas). Al retomar:
  - El build completo de cordoba quedo lanzado por el usuario el 2026-07-12 (exact,
    ~11-12 h). Verificar al abrir sesion: existe `data/cities/cordoba/v1/metadata.json`,
    tamanos (~2.5 GB, horizon ~2 GB), `shade-engine predict` con el kit responde, y la
    API la lista (`uv run uvicorn shade_api.app:app` + `/v1/cities`).
  - Siguiente sesion: planificar Fase 5 (parking) cuando el usuario lo pida.
- 2026-07-12 (investigacion fuentes Fase 5): la digitalizacion manual del parking NO es
  necesaria. Tres barridos verificados en vivo (municipal, OSM via Overpass, supra-municipal):
  - Mejor fuente de GEOMETRIA: el visor de trafico municipal retirado
    (movilidad.cordoba.es/informaciontrafico, hoy enlace roto en el CKAN municipal) esta
    archivado en Wayback Machine (captura 2024-09-03) con los datos inline en JS:
    51 LineStrings de zona azul (trazo #007bfe; otros 7 azules son accesos de parkings
    off-street, se distinguen por el icono del marker que cierra cada grupo) en
    EPSG:4326 + 21 markers con calle, plazas, bateria/cordon y horario completo en el
    popup. Verificado, descargado (2.9 MB HTML) y parseado:
    `scripts/parse_cordoba_parking.py` -> `cities/cordoba/parking.geojson` (21 zonas,
    1152 plazas).
  - ATRIBUTOS oficiales: Ordenanza Fiscal 407 ejercicio 2026 (tarifas: no residente
    0.25-1.70 EUR, max 2 h; residente 0.10-0.80) y Ordenanza de Movilidad BOP 17-02-2023
    arts. 91-93 (sin anexo de calles: delega zonas/horarios en acuerdos BOP + senal).
    En Cordoba NO hay zona verde: residentes usan la azul con tarifa reducida.
  - NO existe dataset abierto vivo: el CKAN municipal solo tiene un dataset
    ("trafico-informacion") cuyo unico recurso es el enlace HTML roto al visor, licencia
    sin especificar. Nada en NAP DGT (solo ocupacion off-street y ZBEs), ni DERA/IECA,
    ni Overture (su tema transportation pierde justo los tags parking:\* de OSM), ni apps
    (Parkopedia/Telpark/ElParking: propietarias). En Espana este dato solo lo publican
    como open data Madrid, Pamplona, Vitoria y Zaragoza.
  - OSM (medido, area 3600343207): off-street razonable (192 amenity=parking, mayoria
    con poligono), en calzada ~1.2% del viario (78/6724 ways con parking real), zona
    azul ausente (0 maxstay, 0 zone, 0 fees en calzada). Ojo ODbL: mezclar geometria
    OSM en la capa arrastra share-alike; con la via Wayback no hace falta.
  - Caveats para la sesion de Fase 5: la captura es de sept 2024 (contrastar altas
    posteriores, p.ej. ampliacion Plaza de Toros dic 2025, contra acuerdos BOP y las
    listas de zona-azul.es / ElParking); licencia municipal sin especificar (rellenar
    source/last_verified del schema con la procedencia); zone_type comercial vs
    administrativa se deriva del texto del horario. Ejes oficiales de apoyo si hay que
    retocar geometria: CDAU WFS (cdau:v_tramo, callejerodeandalucia.es) o IGN IGR-RT
    viario urbano (CC-BY). La copia descargada del HTML es efimera (scratchpad); el
    parseo debe re-descargar de Wayback con la URL con timestamp fija.
- 2026-07-12 (build completo verificado): el build de cordoba termino a las 20:05
  (lanzado 08:44, 11h21m, dentro de la extrapolacion 11-12 h del probe). Cifras
  reales: 90 tiles LAZ / 738,284,408 puntos; artefactos 2.4 GB (horizon.tif 1.8 GB,
  dtm 207 MB, blocker_class 206 MB, dsm 184 MB, landcover 8.8 MB). Verificacion:
  metadata.json correcto (exact, 64 sectores, 500 m), `shade-engine predict` responde
  con hoja coherente para los 10 puntos del kit, y la API lista cordoba y responde
  /v1/shade en vivo. La Fase 4 queda "en curso" SOLO por el paseo de validacion
  (fotos + ajustes de precision). Extras adelantados en esta misma sesion:
  parking.geojson de Fase 5 (item marcado) y sondeo del grafo de Fase 8.
- 2026-07-12 (cierre de Fase 4): la fase pasa a "hecha" con el criterio redefinido
  (ver su seccion): el paseo de validacion y sus ajustes viven ahora en la seccion
  "Diferido: validacion de campo de Cordoba", idealmente tras el deploy de Fase 6.
  Siguiente sesion: planificar Fase 5 (parking) cuando el usuario lo pida; el dato
  critico (parking.geojson) ya esta commiteado y testeado.
- 2026-07-12 (Fase 5 completa): tres commits (infra PostGIS, import-layer, endpoint).
  Flujo dev: `docker compose up -d db` -> `uv run alembic upgrade head` ->
  `uv run shade-engine import-layer cordoba parking` (las dos ultimas leen
  SHADE_DATABASE_URL; URL dev en el comentario del compose). Los tests de DB se
  saltan sin server local y corren SIEMPRE en CI (service container). Verificado en
  vivo sobre Cordoba real: 14 zonas alrededor de Tendillas al atardecer con estados
  coherentes (la zona azul mas cercana al centro peatonal queda a 434 m: Gran
  Capitan). Notas para Fase 6 (deploy): el contenedor de la api debe copiar
  alembic.ini + migrations/ y ejecutar `alembic upgrade head` antes de servir;
  anadir SHADE_DATABASE_URL al compose de prod (postgis interno); el endpoint
  /v1/parking/nearby con `at` explicito emite max-age=60 (cachea bien tras
  CloudFront). Roadmap corto anotado en investigacion de fuentes: contrastar la
  captura sept 2024 con altas posteriores (ampliacion Plaza de Toros dic 2025)
  cuando haya fuente; los 486 puntos de carga/descarga del visor siguen fuera de
  alcance.
- 2026-07-12 (Fase 6 completa): https://shade.ajustino.dev en produccion. Cuatro
  commits (CORS regex, imagen+compose+caddy, pipeline deploy, docs). OJO flujo dev
  desde ahora: `docker compose -f docker-compose.yml up -d db` (compose.yml es el de
  PROD y gana la precedencia). Operacion en el VPS: /opt/shade (clone), .env con la
  password de postgres (600), datos en /opt/shade/data/cities (rsync desde local),
  redeploy automatico en cada push a main con CI verde (gate: variable de repo
  SHADE_DEPLOY_ENABLED; apagarla para congelar prod). Operaciones manuales que la
  pipeline NO cubre: rsync de artefactos nuevos y `docker compose run --rm api
shade-engine import-layer <city> <layer>` tras cambiar un geojson. Pendiente
  diferido: el paseo de validacion de campo ya puede hacerse contra la API publica.
  Siguiente: Fase 7 (visualizacion + Astro).
- 2026-07-13 (Fase 7 completa): mapa de sombra en
  https://ajustino.dev/case-studies/shade-engine (en/es). Tres commits aqui
  (pipeline tiles + learning notes, caddy /tiles/\*, docs) y uno en ajustinodev
  (consola + caso de estudio + fixtures). Ops de tiles: regenerar =
  `uv run shade-engine tiles cordoba` (~15 min los 16 instantes) + rsync de
  `data/cities/cordoba/v1/tiles/` al VPS; el manifest lleva ?v= asi que no hay
  que purgar caches. El basemap y los assets (fonts/sprites) NO se regeneran:
  viven en el VPS (`data/cities/{cordoba/v1/tiles/basemap.pmtiles,assets/}`).
  La web se despliega sola al pushear ajustinodev (Cloudflare Pages); sus
  fixtures de fallback en public/data/shade-\*.json se recapturan con curl si
  cambia el contrato de la API. Pendiente diferido: paseo de validacion de
  campo (ahora con el mapa como apoyo visual). Siguiente: Fase 8 (rutas
  peatonales a la sombra), boceto en su seccion.
