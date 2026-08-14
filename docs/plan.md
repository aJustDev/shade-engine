# Shade Engine - Plan de implementacion por fases

Documento vivo. Cada sesion de trabajo toma items de la fase activa, los marca al
completarlos y anota decisiones en el registro del final. El spec de referencia es
[shade-engine-mvp.md](shade-engine-mvp.md).

## Estado global

| Fase | Nombre                             | Estado   |
| ---- | ---------------------------------- | -------- |
| 0    | Bootstrap del repo                 | hecha    |
| 1    | core/: modelo solar + horizonte    | hecha    |
| 2    | pipeline/: de LAZ a artefactos COG | hecha    |
| 3    | api/: consulta de sombra (sin DB)  | hecha    |
| 4    | Cordoba real + validacion de campo | hecha    |
| 5    | Parking                            | hecha    |
| 6    | Despliegue en cartagena            | hecha    |
| 7    | Visualizacion + integracion Astro  | hecha    |
| 8    | Rutas peatonales a la sombra       | en curso |
| 9    | SVF + exposicion solar acumulada   | boceto   |
| 10   | MRT / UTCI a nivel de peaton       | boceto   |
| 11   | Rutas frescas + diagnostico        | boceto   |

Estados: pendiente / en curso / hecha / boceto.

Las fases 9-11 son post-MVP: reposicionan el motor hacia confort termico (ver
"Vision post-MVP: motor de confort termico"). Sin fechas ni prioridad todavia.

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

## Fase 8 - Rutas peatonales a la sombra (en curso)

Objetivo: "quiero pasear por esta zona a tal hora: dame el recorrido con mas
sombra". Implementada 2026-08-14 (codigo completo, verificado sobre la ciudad
sintetica); queda la operacion de construir el grafo de las ciudades reales
en el VPS (ver "Ops pendiente" abajo). Solo A->B: el modo circuito
(zona + duracion) queda explicitamente fuera, anotado como roadmap.

- [x] Grafo peatonal desde OSM: `shade-engine graph <city>` (osmnx tras la
      interfaz `GraphSource`, espejo de LidarSource/CnigSource; cache
      Overpass en data/cache/osm). El MultiDiGraph se colapsa a arrays numpy
      no dirigidos (gemelos reciprocos deduplicados, paralelas VERDADERAS
      conservadas) y se congela como artefacto aditivo en
      `data/cities/<id>/v1/graph/` (graph.npz + fractions.npz + graph.json),
      con readback tras escribir y loader con chequeos de coherencia en
      `shade_core.routegraph`. Ni PostGIS ni Overpass en runtime.
- [x] Coste solar por arista: muestreo cada 5 m por longitud de arco en CRS
      proyectado y fraccion de sol PRECALCULADA contra los 83 instantes de
      la escalera de declinacion (compute_state_raster + bincount, una
      pasada vectorizada por instante; misma fisica y mismos instantes que
      los tiles). uint8 (fraccion\*255); muestra fuera de dato = sol.
- [x] A\* con ese peso; endpoint `GET /v1/routes/shaded?from&to&at&alpha`:
      motor CSR numpy propio en `shade_api.routing` (sin networkx en la
      API), snap al nodo mas cercano (400 m max), fecha via covers del
      ladder + interpolacion lineal entre horas, respuesta con la ruta
      corta SIEMPRE de referencia, atribucion ODbL, noche -> status night,
      ciudad sin grafo -> 503 accionable. Modo circuito NO (roadmap).
- [x] MVP evalua la sombra a la hora de salida: en un paseo de 30 min el sol
      se mueve ~7 grados; el coste variable durante el propio recorrido queda
      para despues si el error molesta
- [x] Viewer local: modo ruta (boton + 2 clicks A/B), input de alfa,
      re-consulta al mover los sliders de fecha/hora, par de capas
      route-shaded/route-shortest en buildStyle y resumen comparativo
      ("40 m at 40% sun vs 40 m at 70% sun")

Datos medidos (sondeo 2026-07-12, osmnx sobre el bbox de artefactos 8x7 km,
network_type=walk): 12,951 nodos / 39,042 aristas, un solo componente conexo;
descarga+construccion 18.3 s; +223 MiB de RSS; GraphML 15.6 MiB; A\* con
networkx puro y heuristica de linea recta: mediana 10.1 ms, p90 39.1 ms
(rutas mediana 2.8 km); 1976 km de aristas -> ~395k puntos de muestreo a 5 m
para el precalculo solar. El artefacto final elimina el termino de RAM: los
arrays cargados rondan ~10-25 MB por ciudad frente a los 223 MiB del grafo
networkx.

Ops HECHA (2026-08-14): 12 commits (fases 8, 8.5 y 8.6) subidos a main, CI
verde y autodeploy correcto; los grafos schema 2 de montilla y cordoba
viven ya en `/opt/shade/data/cities/<id>/v1/graph/`.

Orden que se siguio y por que: **datos primero, codigo despues**, al reves
de lo que decia el aviso original. Se pudo porque se verifico antes que
prod NO tenia ningun grafo y que `/v1/routes/shaded` devolvia 404: la
imagen desplegada era anterior a la Fase 8 y no tiene cargador de grafos,
asi que ignora el directorio. Y conviene: la API carga los grafos AL
ARRANCAR y el deploy recrea el contenedor, asi que con el orden inverso el
contenedor nuevo habria arrancado sin grafos (503 en rutas) y habria hecho
falta un segundo reinicio.

AVISO vigente para la proxima: cuando prod YA tenga grafos, un cambio de
`ROUTE_GRAPH_SCHEMA_VERSION` obliga a coordinar imagen y datos -- una API
vieja rechaza un artefacto nuevo y viceversa, siempre con error accionable
al cargar (no en silencio), pero con corte.

Roadmap anotado (fuera de la fase): modo circuito zona+duracion. El port
del modo ruta a la consola de ajustinodev esta HECHO (2026-08-14, con
paridad completa: trazo coloreado, bocadillo, presets, arbolado,
alternativas y tabla que adopta el alfa).

Criterio de salida: entre dos puntos del casco a media tarde, la ruta
sombreada evita visiblemente las calles al sol frente al camino mas corto,
comprobable sobre el mapa. -> CUMPLIDO en local sobre las DOS ciudades
reales (2026-08-14):

- Montilla, hoy 18:00 (sol az 262): corta 961 m al 73% de sol vs sombreada
  1.16 km al 35% por calles distintas (route-mode-montilla.png).
- Cordoba, 21-jun 19:00 (sol az 278): cruce este-oeste del casco, corta
  1.41 km al 47% en linea recta de cara al sol vs sombreada 1.57 km al 18%
  subiendo por el callejero de San Andres-San Pablo; +153 m compran 29
  puntos menos de sol (route-mode-cordoba.png).
- Antes, ciudad sintetica: empate de 40 m resuelto por sol (40% vs 70%).
  Falta solo replicarlo contra prod tras la ops de arriba.

## Fase 8.6 - Ruta legible (2026-08-14)

Segunda ronda de feedback del usuario sobre el modo ruta: el trazo era una
linea de color plano que no respondia "donde me va a dar el sol", y el
panel apilaba seis lineas de prosa en una columna de 340 px.

- [x] **Trazo coloreado por tipo de tramo**. El leg `shaded` viaja con su
      descomposicion por arista (`segments`: geometria, longitud y las dos
      fracciones de esa arista); el visor clasifica por argmax (empates a
      favor del sol), fusiona consecutivos de la misma clase (39 -> 21 en
      Cordoba) y pinta con `match` sobre `class` mas un casing oscuro.
      Paleta elegida contra el fondo real (sombra `#24305e`, copa
      `#1f5a4a`, edificios `#3d4350`): sol `#ffdd57`, arbol `#3fbf6f`,
      edificio `#8ea1ff`.
- [x] **Bocadillo por hover**: uno solo a la vez, anclado en el cursor,
      con las cifras del trazo tocado y que clase es ese tramo concreto.
      Throttle por `requestAnimationFrame`, `setHTML` solo al cambiar de
      identidad, y cierre explicito al principio de `render()` porque los
      popups sobreviven a `setStyle`.
- [x] **Tabla en el panel** (grid, no `<table>`: las filas son botones y
      las columnas hay que fijarlas a 316 px utiles) con una fila por
      oferta; pulsar una la ADOPTA fijando su alfa. Debajo, solo la mezcla
      de sombra de la ruta activa y el delta frente a la corta.
- [x] **Leyenda de estilos de trazo** anadida a la del sidebar, con
      swatch de barra, solo cuando hay ruta.

Medido tras el cambio: la respuesta con alternativas pasa de 22,4 KB a
29,4 KB (los segmentos solo viajan en un leg). Verificado en vivo sobre
Cordoba: las tres clases se distinguen sobre el mapa, el bocadillo cambia
por tramo y desaparece al salir, adoptar "max shade" recolorea la ruta y
sincroniza el preset, y la consola queda limpia.

Nota: el caso `status: "night"` es inalcanzable desde los sliders del
visor (la escalera solo tiene instantes diurnos; el peldano de invierno
acaba a las 17:00 con el sol a 10 grados). La rama neutra del visor es
defensiva y quien la fija son los tests de la API.

## Fase 8.5 - Refinamiento del modo ruta (2026-08-14)

Ronda de mejoras pedida por el usuario tras usar el modo ruta en vivo.
Cuatro sesiones, todas hechas y verificadas en local sobre Cordoba.

- [x] **Precision del snap**: origen y destino se pegaban al NODO mas
      cercano (mediana 64 m de error en Cordoba, p90 330 m). Ahora
      `snap_point` proyecta sobre la ARISTA mas cercana (tabla plana de
      segmentos construida en `RouteGraph.build`, argmin vectorizado, 1,8
      ms sobre 46.118 segmentos) y `astar_points` enruta entre puntos
      interiores con extremos virtuales. Error mediano 27 m (-58%).
- [x] **UX del visor** (solo disco, `viewer/` sigue gitignored): pines A/B
      arrastrables con re-consulta al soltar, conector punteado del pin al
      punto de enganche, panel con minutos andando y minutos al sol,
      presets de alfa con nombre (direct/balanced/avoid sun/max shade),
      boton de limpiar pines, retirada del pin rojo de inspeccion al
      entrar en modo ruta, y AbortController (arrastrar generaba rafagas
      cuyas respuestas podian llegar desordenadas).
- [x] **Peso para sombra vegetal**: segunda matriz uint8 por arista e
      instante (`veg_shade_fraction`) en fractions.npz -> schema 2;
      parametro `beta` con escalera de penalizaciones
      `len * (1 + alfa*sol + beta*sombra_no_vegetal)`, validado
      `beta <= alfa`. Los legs reportan `veg_shade_length_m`.
- [x] **Alternativas puntuadas**: `?alternatives=true` barre alfas
      (0, 0.5, 1, 2, 4, 8), deduplica por secuencia de tramos y filtra
      dominadas; en Cordoba devuelve 5 rutas de 1,41 km/41% sol a
      1,91 km/4% en ~40 ms.

Notas didacticas anadidas: `point-segment-projection.md`,
`vegetation-cooling.md`, `pareto-front.md`, y seccion de extremos
virtuales en `a-star.md`.

Fuera de alcance, decidido con el usuario: **routing por acera**. Sondeo
Overpass hecho (2026-08-14): Cordoba tiene 439 aceras mapeadas como way
sobre 4.942 calles (~9%) y 1.718 nodos de cruce; Montilla, cero (0 ways,
1 nodo). Si se retoma, la via no es OSM sino sintetizar las dos aceras por
offset del eje y muestrear el sol por lado (el motor ya soporta aristas
paralelas entre los mismos nodos); penalizar cruces exigiria ademas partir
nodos por lado.

Verificado en local sobre Cordoba (2026-08-14):

- Un pin a 3 m del centro de una calle arranca la ruta ahi, no en el cruce
  a 100 m; el conector punteado dibuja el enganche.
- `beta=0.5` frente a `beta=0` en el cruce este-oeste: +128 m bajo
  arbolado (690 -> 818) a cambio de +42 m de sol y +14 m de recorrido.
  Es el debilitamiento del invariante que documenta la decision de abajo.
- Barrido de alternativas: 5 ofertas distintas, longitud creciente y sol
  estrictamente decreciente.

## Vision post-MVP: motor de confort termico y refugios climaticos

Reencuadre de producto (sesion 2026-07-13, explorado antes de comprometer
fechas; las Fases 9-11 son bocetos). shade-engine no es solo una calculadora de
sombra. El **horizon raster** que ya generamos (64 sectores de angulo de
horizonte por pixel) es, casi literalmente, un **Sky View Factor (SVF)
precomputado**: la fraccion de cielo visible desde cada punto, que es el input
caro de todo modelo de confort termico radiativo. La parte computacionalmente
costosa (la geometria de la escena urbana a 1 m) ya esta hecha; lo que falta para
pasar de "sombra" a "confort" son datos meteo y un modelo de balance radiativo,
no mas geometria.

Cadena de valor del confort al calor (lo que se siente no es la temperatura del
aire, sino la carga radiativa sobre el cuerpo):

    geometria (sombra + SVF) -> carga radiativa onda corta/larga -> MRT ->
    indice de confort (UTCI / PET) -> percepcion

- **MRT** (Mean Radiant Temperature): temperatura radiante media que siente un
  cuerpo; integra sol directo, difuso, reflejado por superficies y onda larga de
  muros y asfalto calientes. Domina en verano (sol vs sombra pueden diferir
  20-30 grados de MRT con la misma temperatura de aire).
- **UTCI / PET**: indices normalizados (MRT + aire + humedad + viento) en grados
  de sensacion; el lenguaje de la literatura y del urbanismo.

Lo que dominamos: la geometria (sombra hoy; SVF casi gratis). Lo que falta para
MRT: meteo (aire, humedad, viento, radiacion global/difusa) de AEMET para un
punto de la ciudad, y propiedades de superficie (albedo/emisividad) aproximables
desde nuestro propio landcover.

Trampa central a no olvidar: **sombra != frescor**. Una calle en sombra con
asfalto recalentado, muros que irradian y sin viento es un horno. Vender un mapa
de sombra como mapa de confort sin MRT lo detecta cualquier tecnico y quema
credibilidad; por eso el salto a MRT (Fase 10) no es cosmetico.

Puente concreto: **SOLWEIG** (modelo de MRT urbano, parte de UMEP, plugin de
QGIS, Universidad de Gotemburgo) toma exactamente nuestros rasteres: DSM, DEM/DTM
y CDSM (canopy height model, que ya derivamos). Estrategia: validar contra
SOLWEIG sobre un barrio de Cordoba para calibrar, y decidir despues si se depende
de el o se reimplementa el nucleo radiativo aprovechando el SVF ya precomputado.

Refugios climaticos: concepto ya institucionalizado en Espana (la red de refugios
climaticos de Barcelona como referencia). NO los designamos nosotros (es decision
municipal); aportamos (a) rutas frescas hacia ellos, (b) evaluacion de la calidad
termica del espacio publico a resolucion de acera, (c) priorizacion de
intervenciones (donde plantar o dar sombra rinde mas confort por euro).

Encaje de negocio (a diferencia del aparcamiento): la adaptacion al calor tiene
mandato y presupuesto (planes municipales de adaptacion al cambio climatico,
fondos europeos/nacionales, olas de calor como agenda politica creciente). Ciclo
B2G lento pero con comprador real. Diferenciacion defendible frente a
shademap.app (sombra global desde footprints OSM extruidos): MRT/SVF a 1 m desde
LiDAR real, con vegetacion y geometria de tejados reales, no prismas OSM.

Tesis de posicionamiento: shade-engine = capa de datos + API + tiles de geometria
radiativa y confort termico a resolucion de peaton; el aparcamiento y las rutas
son demos de consumo encima.

## Fase 9 - SVF + exposicion solar acumulada (boceto)

Objetivo: los dos productos de confort "casi gratis", reutilizando artefactos y
codigo que ya existen. Sin datos meteo todavia. Independiente de la Fase 8.

- [ ] SVF por pixel derivado del horizon raster (integral azimutal de los 64
      angulos de horizonte; no hay que re-barrer nada). Nuevo artefacto svf.tif +
      capa. Valor propio: correlaciona con la isla de calor nocturna (el calor
      atrapado de noche crece con 1 - SVF)
- [ ] Exposicion solar acumulada: sumar minutos de sol vs sombra por pixel en un
      dia tipo de verano barriendo `shade_timeline`; raster de "horas de sol/dia".
      Proxy honesto de estres termico (dice exposicion, no grados)
- [ ] Servir ambos como tiles (mismo patron PMTiles de Fase 7) + endpoint de
      consulta puntual si aporta
- [ ] docs/learning: sky-view-factor.md (que es, formula, por que el horizon
      raster lo da gratis, trampa: SVF de superficie horizontal vs de pared)

Criterio de salida (provisional): mapas de SVF y de exposicion acumulada de
Cordoba visibles y coherentes con la intuicion (canones urbanos = SVF bajo;
plazas abiertas = exposicion alta).

## Fase 10 - MRT / UTCI a nivel de peaton (boceto)

Objetivo: el salto de credibilidad: de exposicion a temperatura radiante y a
indice de confort, validado. Depende de la Fase 9 (SVF).

- [ ] Fuente meteo: AEMET (aire, humedad, viento, radiacion global/difusa) para
      Cordoba; decidir estacion + interpolacion (el viento es el termino mas
      incierto a escala de calle: documentar la asuncion)
- [ ] Propiedades de superficie (albedo/emisividad) por clase de landcover
- [ ] Modelo de balance radiativo -> MRT por pixel a un instante; UTCI/PET desde
      MRT + meteo. Calibrar contra SOLWEIG sobre un barrio
- [ ] Validacion de campo (sensores un dia de verano en Cordoba): la barrera real
      de credibilidad ante un ayuntamiento. La sombra se valida con trigonometria;
      el confort modelado, no
- [ ] docs/learning: mean-radiant-temperature.md, utci-pet.md

Decisiones abiertas (para su sesion de planificacion):

- Motor: integrar SOLWEIG como dependencia (validas rapido contra literatura,
  pero te atas a su implementacion/licencia) vs reimplementar el nucleo radiativo
  sobre nuestro SVF precomputado (mas trabajo, es la diferenciacion, encaja en el
  pipeline de artefactos). Decidir tras la calibracion
- Escenarios "que pasa si planto arboles aqui" (modificar el CDSM y recalcular):
  oro para un ayuntamiento pero es un producto en si; posponer

Criterio de salida (provisional): mapa de MRT/UTCI de un barrio de Cordoba a una
hora de ola de calor, contrastado contra SOLWEIG y contra medidas de campo.

## Fase 11 - Rutas frescas + diagnostico urbanistico (boceto)

Objetivo: los dos casos de uso encima del confort. Las rutas frescas dependen del
grafo de la Fase 8; el diagnostico depende de la Fase 10.

- [ ] Rutas frescas: el A\* de la Fase 8 con peso de arista = exposicion/MRT en
      vez de (o ademas de) distancia. "Como llego al refugio sin cocerme".
      Endpoint `GET /v1/routes/cool?from&to&at`
- [ ] Diagnostico de calidad termica del espacio publico: capa para el
      planificador (donde el espacio de estancia es habitable en verano)
- [ ] Priorizacion de intervenciones: cruzar MRT + SVF + landcover para senalar
      calles-canon expuestas sin vegetacion (mas confort por euro de arbol/toldo)
- [ ] Conexion con refugios climaticos municipales (capa externa si el
      ayuntamiento la publica; nosotros enrutamos y evaluamos, no designamos)

Criterio de salida (provisional): entre dos puntos del casco en ola de calor, la
ruta fresca evita las calles de MRT alto; un mapa de priorizacion senala calles
concretas donde plantar.

## Transversal (todas las fases)

- Cada concepto geo nuevo: nota corta en `docs/learning/` en el mismo commit (spec seccion 10)
- Docstrings didacticos en `core/` (formulas, unidades, convenciones de signo)
- Decisiones tecnicas con alternativas: exponer opciones y porque, y anotarlas en el registro

---

## Registro de decisiones

| Fecha      | Decision                                                                                                                                                                                                                                                | Porque                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-07-10 | Horizonte con observador en DTM+1.6m, obstaculos DSM                                                                                                                                                                                                    | Evita error bajo copa y sobre tejado (apunte 1)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 2026-07-10 | Postgres pospuesto a Fase 5                                                                                                                                                                                                                             | Fases 0-4 no necesitan DB (apunte 4)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 2026-07-10 | Pipeline contenerizado desde el inicio                                                                                                                                                                                                                  | PDAL solo fiable via conda-forge (apunte 5)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 2026-07-10 | Licencia MIT                                                                                                                                                                                                                                            | Eleccion del usuario; permisiva y minima                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| 2026-07-10 | README en ingles; docs/ y docs/learning/ en castellano                                                                                                                                                                                                  | Alcance OSS vs objetivo didactico personal                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 2026-07-10 | Python 3.14 en todo el workspace                                                                                                                                                                                                                        | Wheels cp314 verificados en PyPI para rasterio 1.5.0, shapely 2.1.2, pyproj 3.7.2, numpy 2.5.1; pvlib puro                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 2026-07-10 | Repo publico ya: github.com/aJustDev/shade-engine                                                                                                                                                                                                       | Unica forma de verificar el criterio "CI verde"                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 2026-07-10 | Commits en ingles (convencion en CLAUDE.md)                                                                                                                                                                                                             | Coherencia con repo OSS publico en ingles                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 2026-07-10 | docker-compose aplazado a Fase 2/5                                                                                                                                                                                                                      | Sin DB ni servicios que orquestar todavia                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 2026-07-10 | Elevacion solar APARENTE (con refraccion)                                                                                                                                                                                                               | Es el sol que se ve; relevante al amanecer/atardecer (~0.5 grados en el horizonte)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| 2026-07-10 | Horizonte: interpolacion azimutal lineal circular                                                                                                                                                                                                       | Nearest erraria hasta medio sector (~2.8 grados con 64), metros de frontera de sombra                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 2026-07-10 | Horizonte: muestreo espacial nearest, no bilinear                                                                                                                                                                                                       | Promediar perfiles a traves de una pared mezcla tejado con calle: angulos sin sentido fisico                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| 2026-07-10 | `compute_horizon_reference` (fuerza bruta) en core                                                                                                                                                                                                      | Oraculo para validar la version vectorizada/tileada del pipeline (Fase 2) sobre los mismos fixtures                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| 2026-07-10 | Tipo de sombra: ray-march a medio pixel + fallback al sector contribuyente                                                                                                                                                                              | La interpolacion azimutal difumina bordes de obstaculo ~medio sector; en esa banda el tipo se atribuye al sector que aporto el angulo. Paso de medio pixel: uno entero salta esquinas                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 2026-07-11 | laspy + lazrs (pip puro) en vez de PDAL; REVIERTE "pipeline contenerizado" (2026-07-10)                                                                                                                                                                 | lazrs publica wheel cp314 y laspy es Python puro: pipeline entero instalable con uv, smoke test e2e en CI sin Docker. Solo necesitabamos binning, que numpy hace en pocas lineas. Dockerfile aplazado                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 2026-07-11 | Tipo de sombra en produccion: raster de clase por sector (cierra apunte 3)                                                                                                                                                                              | El argmax del barrido ya sabe que celda bloquea cada sector: guardar su landcover cuesta casi nada y la consulta pasa a 1 lectura de pixel (vs 3 ventanas COG del ray-march). Deflate comprime clases casi gratis (24K el cubo). Ray-march conservado como oraculo de paridad                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 2026-07-11 | Driver PNOA aplazado a Fase 4; Fase 2 usa driver de directorio local                                                                                                                                                                                    | CNIG sin API documentada (visor con endpoints internos jQuery, fragiles). No bloquea el criterio de salida de la fase; en Fase 4 se intenta el scraper con fallback manual                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 2026-07-11 | Horizonte cuantizado a uint8 (90/255 deg) con la escala en tag del GeoTIFF                                                                                                                                                                              | Error <= ~0.18 deg, muy por debajo del medio pixel del barrido; mitad de disco que uint16; el fichero es autodescriptivo                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| 2026-07-11 | Barrido de produccion: dedupe de offsets + tiling con buffer ceil(max_d/res)                                                                                                                                                                            | Exacto tras el floor a 0 (prueba en docstring): bit-identico al oraculo en modo exact, memoria acotada por tile. El modo geometric (paso creciente) queda como knob para Fase 4, validado solo por cuantil                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 2026-07-11 | Lector por ventana en core (`SceneReader`), no en api                                                                                                                                                                                                   | Cada bloque LRU es una ShadeScene local: `is_shaded`/`shade_timeline` se reutilizan sin duplicar nada. Bloques de 64 px (dividen el tile COG de 512), ~1.3 MiB/bloque, techo por config                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 2026-07-11 | `scene_for` devuelve el centro del pixel como punto de consulta                                                                                                                                                                                         | El motor recalcula rowcol contra el origen LOCAL del bloque; en el borde el redondeo float puede dar indice -1 o fuera del bloque (500 en un punto valido). Con muestreo espacial nearest el snap es semanticamente gratis                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 2026-07-11 | Rate limiting: middleware propio sobre `limits`; slowapi DESCARTADO                                                                                                                                                                                     | slowapi 0.1.10 resuelve el handler buscando `.endpoint` en app.routes y fastapi >= 0.139 envuelve los routers en `_IncludedRouter` sin ese atributo: exime TODAS las rutas en silencio (lo cazo el test de 429). El middleware propio son ~15 lineas sobre el mismo motor                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 2026-07-11 | Fixture `built_city` movido a coordenadas UTM reales de Cordoba                                                                                                                                                                                         | La API recibe lat/lon: con origen (0,0) ningun lat/lon real cae en el fixture. Coordenadas ~4e6 ademas destapan bugs de georef que el origen cero enmascara. Los goldens solares de Fase 1 siguen valiendo (~37.87N)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 2026-07-11 | `/v1/cities` lista solo ciudades con artefactos; attribution desde metadata.json                                                                                                                                                                        | "Disponible" = consultable; un YAML sin build se salta con warning (cordoba hasta Fase 4). La atribucion sale del artefacto construido, no del YAML vivo: es la del dato que responde                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 2026-07-11 | CORS origins como CSV en env con `NoDecode`                                                                                                                                                                                                             | pydantic-settings decodifica los campos lista como JSON ANTES de los validators; CSV es lo menos sorprendente para ops y NoDecode permite el validator before que lo trocea                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 2026-07-12 | Cordoba con PNOA 3a cobertura (LIDA3, vuelo 2024) y atribucion CC-BY de obra derivada                                                                                                                                                                   | 5 pt/m2 vs 1.5 de la 2a y un vuelo 2024 que refleja la ciudad que se validara en campo en 2026; formula abreviada del IGN (Obra derivada de PNOA-cob3 2022-2025 CC-BY 4.0 scne.es) en YAML, README y metadata                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 2026-07-12 | Driver CNIG: scraping de archivosSerie + POST descargaDir, resumible                                                                                                                                                                                    | Endpoints internos verificados en vivo (sin sesion ni captcha) pero sin contrato: fallo ruidoso con instrucciones de fallback manual; cache validado por magic LASF sobrevive cortes (limite documentado ~20/sesion anonima); seleccion y cobertura delegadas en LocalDirectory                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 2026-07-12 | Filtrado LiDAR: clases 7/18/12 y flags withheld/overlap fuera; synthetic se conserva                                                                                                                                                                    | El DSM es un max por celda: un punto espurio alto crea un obstaculo fantasma en el horizonte de 500 m a la redonda; synthetic marca puntos validos (suelo hidro-aplanado del Guadalquivir) y tirarlo agujerearia el DTM del rio                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 2026-07-12 | RAM del build: cubos de horizonte memmapped + COG por banda + sin copias float64                                                                                                                                                                        | Los cubos (~6.7 GB a escala ciudad) eran el pico; con respaldo en fichero el kernel pagina bajo presion. Probe: pico 1.26 GiB; ciudad estimada ~4.5 GiB, cabe en 11 GiB. Bit-identico. Descartados: COG incremental (driver CreateCopy-only) y bajar resolucion/sectores                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| 2026-07-12 | Cobertura: footprints bufferizados con mitre antes de la union (hallazgo del probe)                                                                                                                                                                     | Los tiles reales de PNOA cuantizan a mm y dejan costuras de 1 mm entre extents de puntos que unary_union nunca cierra; la tolerancia aplicada solo al perimetro del target no podia absorber huecos internos                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| 2026-07-12 | Probe 2x2 km: exact 48.7 min / geometric 16.8 min (2.9x); build canonico v1 en exact                                                                                                                                                                    | Geometric valido para iterar (p50/p90 identicos, p99 0.35 deg, 0.13% > 2 deg, blocker 99.4% igual; outliers por roce de esquina) pero la validacion de campo debe testear la fisica, no el atajo de muestreo. Extrapolado ciudad: exact ~11-12 h, geometric ~4 h                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 2026-07-12 | Fase 4 CERRADA sin el paseo; la validacion de campo pasa a tarea diferida                                                                                                                                                                               | El paseo se retrasa semanas y mantener la fase abierta solo por el bloqueaba la lectura del plan. El motor quedo verificado sobre datos reales (build completo + predict coherente + API en vivo); el contraste foto-vs-prediccion mejora ademas tras el deploy de Fase 6 (movil contra la API publica). Seccion "Diferido" con los 2 items para que no se pierdan                                                                                                                                                                                                                                                                                                                                                                       |
| 2026-07-12 | Parking en PostGIS como `geography(MultiLineString, 4326)`; modelos en `shade_core.db` tras extra opcional `shade-core[db]`                                                                                                                             | geography acepta METROS en ST_DWithin/ST_Distance (geometry 4326 filtraria en grados: la trampa de crs.md en SQL) y una tabla sirve N ciudades sin fijar un CRS local por fila. El extra mantiene el core base libre de DB (apunte 4). Indice GiST explicito (spatial_index=True de geoalchemy2 duplica DDL bajo alembic) y primera migracion a mano (autogenerate con geoalchemy2 exige helpers extra). Geometrias entran como EWKT (geoalchemy2 envuelve binds en ST_GeogFromText; GeoJSON crudo reventaria)                                                                                                                                                                                                                           |
| 2026-07-12 | compose en raiz (solo servicio db) y tests de DB contra database scratch: skip local sin server, raise si CI                                                                                                                                            | El docker/ del spec llega en Fase 6 con el Dockerfile de la api; un dir para un yaml es ruido. La scratch (nombre unico + upgrade head + DROP FORCE) aisla pytest de los datos dev del compose y prueba las migraciones en cada corrida; el raise con CI seteado evita que un service container roto convierta los tests de DB en skips verdes. OJO postgres:18: el volumen va en /var/lib/postgresql, sin /data                                                                                                                                                                                                                                                                                                                         |
| 2026-07-12 | Sombra por zona en /v1/parking/nearby: muestreo cada 10 m en CRS proyectado + fraccion + veredicto por mayoria (0.5); sin shade_type a nivel de zona                                                                                                    | Una calle de 300 m no tiene UN estado: la fraccion informa y el umbral da un veredicto accionable; el shade_type agregado quedaba mal definido con muestras mixtas (edificio+arbol) y el detalle punto a punto ya lo da /v1/shade. shaded_until barre las posiciones solares restantes del dia (UNA llamada pvlib por request, compartida entre zonas; por-instante costaria ms de pandas cada una) y cierra tambien al acabar la luz, como shade_timeline. radius con tope 1000 m + LIMIT 50                                                                                                                                                                                                                                            |
| 2026-07-12 | SHADE_DATABASE_URL unica para API y CLI (validation_alias en ApiSettings + populate_by_name)                                                                                                                                                            | Dos nombres para el mismo valor era ruido de ops. El alias puentea el prefijo SHADE*API*; populate_by_name mantiene la construccion por kwargs de los tests y de paso hace funcionar el nombre prefijado como fallback (el alias gana). Sin URL la API arranca igual y solo /v1/parking responde 503                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 2026-07-12 | Deploy: imagen construida EN el VPS desde clone en /opt/shade (sin registry); Dockerfile en RAIZ, no docker/ del spec; una sola imagen con TODO el workspace (--all-packages)                                                                           | Patron ya establecido en el VPS para apsis/geohazard (compose.yml prod en raiz + build local + tag :prod); un registry anadiria credenciales y latencia sin beneficio a esta escala. El root del workspace uv es virtual (package=false): sin --all-packages el sync no instala NADA. Incluir pipeline mete el CLI shade-engine en la imagen: el import de parking en prod es `docker compose run --rm api shade-engine import-layer ...`, sin uv en el VPS ni tuneles. Trampa real cazada: python:3.14-slim no trae libexpat1 y la wheel de rasterio lo enlaza sin vendorizarlo (unico apt-get de la imagen)                                                                                                                            |
| 2026-07-12 | Prod en cartagena: puertos loopback 8003 (api) / 5437 (db), migrate one-shot antes de servir, workers via WEB_CONCURRENCY, --forwarded-allow-ips "\*", COGs por rsync + bind mount :ro                                                                  | Todo se publica solo en 127.0.0.1: Caddy es el unico cliente, lo que hace seguro el "\*" (la IP origen dentro del contenedor es la gateway del bridge, no fijable). migrate corre como servicio con service_completed_successfully: la api nunca arranca contra un schema viejo. WEB_CONCURRENCY (default uvicorn de --workers) cumple "limites por config" sin rebuild; OJO: el rate limit es por worker (60/min x 2), y en la practica las conexiones secuenciales las gana casi siempre el mismo worker (accept race), asi que un cliente solo ve ~60/min. compose.yml GANA la precedencia a docker-compose.yml: el flujo dev lleva -f docker-compose.yml SIEMPRE (el ${VAR:?} del prod falla en seco sin .env como red de seguridad) |
| 2026-07-12 | DNS: Cloudflare DNS-only (nube gris), NO CloudFront (error del spec) ni proxied                                                                                                                                                                         | La infra real es Cloudflare; el spec se escribio pensando en AWS. DNS-only replica apsis/geohazard y deja a Caddy emitir Let's Encrypt sin interferencias. Sin CDN delante el item "verificar cache" se cumplio sobre las cabeceras (86400/no-store llegan intactas); si algun dia se activa proxied: cache real en el edge, pero Caddy vera la IP del edge de CF (trusted_proxies para el rate limit) y el cert inicial conviene emitirlo en gris                                                                                                                                                                                                                                                                                       |
| 2026-07-12 | Push-to-deploy con GH Actions calcado de la convencion del VPS: workflow_run sobre CI verde en main + gate SHADE_DEPLOY_ENABLED + clave ssh con forced command a /usr/local/bin/deploy_shade                                                            | La clave dedicada (restrict, no-pty, ...) solo puede ejecutar el script de deploy (fuente de verdad: deploy/deploy.sh; se instala con sudo install en el aprovisionamiento): fetch+reset a origin/main, build, migrate bloqueante, up api, smoke local; el workflow remata con smoke publico. Los DATOS quedan fuera de la pipeline (rsync de COGs e import-layer son operaciones manuales): la pipeline mueve codigo, no gigas                                                                                                                                                                                                                                                                                                          |
| 2026-07-13 | Visualizacion: PMTiles raster ESTATICOS por instante clave, no tiles PNG dinamicos; preset = solsticios + equinoccios 2026 x 4 horas locales (16 instantes)                                                                                             | La sombra de un instante fijo es inmutable: cacheable para siempre y servible como fichero por Caddy, cero carga en la API del VPS compartido (el spec 9.1 ya inclinaba aqui; TiTiler/dinamico queda como roadmap si el servido crece). El raster de estado se calcula vectorizado con UN sol en el centro del bbox (variacion en 8 km ~0.07 deg, bajo el medio-quantum de 0.176) leyendo solo las 2 bandas de horizonte adyacentes al azimut (el cubo float32 entero serian ~14 GB); paridad pixel a pixel con is_shaded testeada (float64 en la comparacion, empates de sector en uint8 crudo). Equinoccios comparten horas a proposito: declinacion ~0 en ambos, el mapa lo hace visible                                              |
| 2026-07-13 | Piramide 12-17 (z17 = 0.94 m/px a lat 37.9 ~ nativo), PNG paleta con sol transparente, tiles en blanco omitidos salvo en min_zoom, tile_compression NONE                                                                                                | z18 seria upsampling (el cliente ya overzooma). Trampas reales del writer pmtiles: finalize revienta con 0 entries (por eso min_zoom se escribe siempre; el dedupe guarda el PNG en blanco una vez), el orden ascendente de tileid mantiene clustered=True, y marcar GZIP en tiles PNG haria a los clientes "descomprimir" bytes que no lo estan. ~10 MB por instante, 158 MB los 16                                                                                                                                                                                                                                                                                                                                                     |
| 2026-07-13 | Servido de tiles: Caddy file_server bajo /tiles/\* replicando el arbol de data/cities (sin v1 hardcodeado), CORS \* con handler explicito de preflight OPTIONS, immutable + ?v= en manifest                                                             | file_server da Range y ETag nativos, que es todo lo que un cliente PMTiles necesita. La trampa que justifica el handler: fetch() con cabecera Range NO es peticion CORS simple y dispara preflight, asi que sin OPTIONS el mapa funciona same-origin y falla SOLO cross-origin. Los .pmtiles se cachean un ano immutable; regenerar tiles no purga nada porque el manifest (max-age 300) lleva ?v=<epoch> en cada URL                                                                                                                                                                                                                                                                                                                    |
| 2026-07-13 | Basemap: extract Protomaps (OSM) autoalojado + glyphs/sprites self-hosted en /tiles/assets; en la web, @protomaps/basemaps tema black (protomaps-themes-base esta deprecado)                                                                            | A escala de ciudad hacen falta calles y nombres; el land-110m de apsis/geohazard es escala mundial. El extract (build 20260712, bbox cordoba + margen, 3 MB) mantiene la regla de la web: sin tile servers de terceros ni API keys en runtime, atribucion OSM en el mapa. Vector y no raster para tenirlo con la estetica del sitio sin regenerar nada. Operacion manual unica por ciudad, documentada en adding-a-city.md                                                                                                                                                                                                                                                                                                               |
| 2026-07-13 | Canopy = vegetacion con CHM >= 2.5 m + sieve 8 px, materializado como artefacto canopy.tif (uint8 0/1, params como tags COG, sin bump del schema de metadata.json); core y tiles leen el fichero, no la formula                                         | La regla cruda landcover==VEGETATION contaba cesped, setos y arriates como copa (clases LiDAR 3/4/5 agregadas: el 55% de los pixeles de vegetacion de Cordoba mide < 2.5 m) y pintaba sombra vegetal permanente. El sieve mata el moteado de clasificacion urbano. Las copas siguen proyectando sombra via horizonte (el DSM no se toca): no hay que re-barrer, solo derivar la mascara (`shade-engine canopy`, backfill sin rebuild) y regenerar tiles. OJO ops: el SceneReader exige canopy.tif al arrancar -> rsync antes del push                                                                                                                                                                                                    |
| 2026-07-13 | Tiles por instante divididos en dos pmtiles (building, que incluye la sombra "other" / vegetation) + mascara de tejados (interior de edificio -> STATE_OUTSIDE, transparente); manifest schema 2 con urls.{building,vegetation} y url legacy = building | Capa de vegetacion conmutable en el visor sin recolor en cliente (dos sources raster y un toggle de visibilidad es lo robusto). La mascara de tejados convierte el overlay en sombra a nivel de calle: nadie pisa un tejado y el basemap ya dibuja los edificios; se aplica en build_tiles DESPUES de compute_state_raster para no romper la paridad pixel a pixel con is_shaded. OUTSIDE y no SUN para que un tile decodificado distinga tejado de calle soleada. El url legacy degrada con gracia al front viejo entre el rsync y el deploy de Pages                                                                                                                                                                                   |
| 2026-07-14 | Acumuladores de rasterize en float32/int32 (antes float64/int64), z a float32 por chunk, division del DTM con where= sin temporales comprimidos, del de acumuladores agotados                                                                           | OOM real: el build de labana a 1 m (bbox 2x2 km + buffer de horizonte 6 km = grid 14x14 km, 196M celdas) murio a manos del OOM killer con 9.9 GB RSS en una WSL de 11 GB; cinco acumuladores float64 de tamano n eran ~7.8 GB. float32 no pierde nada real: los COG de salida ya son float32 y su resolucion a cota 2000 m (~0.1 mm) queda tres ordenes por debajo de la exactitud vertical LiDAR (~10 cm). x/y siguen en float64: a easting ~7e5 float32 cuantiza a 6 cm y movia puntos de celda en el borde. Pico estimado tras el cambio ~5 GB. 171 tests verdes sin tocar ninguno                                                                                                                                                    |
| 2026-07-14 | fill_dtm_gaps: alcance de busqueda en METROS (default 200 m), no en pixeles                                                                                                                                                                             | El limite guardaba anchura fisica de agujero pero estaba denominado en pixeles: el mismo dataset rellenaba a 2 m (100 px = 200 m) y reventaba a 1 m (100 px = 100 m, 47022 celdas NaN). El agujero real localizado (binning de suelo reproducido + mask visual) es una cuna de ~450x150 m SIN NINGUN punto pegada a la costura del easting 688000: el hueco entre los bloques de vuelo PNOA GAL-E 2016 y CYL-NW 2021, cada uno recortado a su frontera autonomica (La Bana esta en el borde CyL/Galicia). Esta a 4.5 km del pueblo, solo afecta al skyline, y el IDW desde 200 m produce ladera lisa sin obstaculos fantasma (el fill no supera las cotas vecinas). Test nuevo: la misma malla rellena a 1 m/px y falla a 100 m/px       |
| 2026-08-13 | POSTMORTEM: horizon.tif del build v1 de cordoba corrupto (bandas 45-64, az ~247-360, a cero en toda la ciudad); prod afectado. El barrido calculo BIEN: los datos se perdieron en la ruta scratch memmapped -> GTiff temporal -> COG                    | Detectado por el visor (tardes sin sombra). Prueba de que no es bug de computo: blocker_class.tif intacto en esas bandas, y el par (angulo 0, blocker real) es imposible por construccion del barrido. El corte de banda muerta varia por tile (39-47) derivando suavemente con el orden de escritura: fallo de I/O temporal e intermitente durante las 11 h en WSL2, no determinista (irreproducible con GDAL a pequena escala). Efecto: sin sombra con sol al oeste de ~250 deg en /v1/shade, timeline, shaded_until, parking y tiles; la demo "coherente al atardecer" de Fase 5 era en parte el bug. Ningun paso verificaba lo escrito                                                                                               |
| 2026-08-13 | COGs con INTERLEAVE=BAND + BIGTIFF=IF_SAFER; write_cog relee el COG terminado y exige igualdad banda a banda; el barrido hace flush() (msync) de los cubos memmapped                                                                                    | Band interleave: leemos 2 de 64 bandas por consulta y pixel interleave descomprimia las 64 (32x de mas); ademas hace la escritura banda a banda estrictamente secuencial. IF_SAFER elimina el techo de 4 GB del TIFF clasico como clase de riesgo. El readback es el contrato "lo calculado es lo enviado": un build de horas no puede asumir que el stack de almacenamiento persiste cada pagina (WSL2 = VHD sobre NTFS). flush() convierte perdidas de writeback en OSError ruidoso                                                                                                                                                                                                                                                    |
| 2026-08-13 | Montilla segunda ciudad (EPSG:25830, bbox 3x2.5 km del casco, LIDA3 vuelo AND 2024, cobertura 25/25 tiles verificada en catalogo), decidida local + prod; y fix del driver CNIG: `archivosSerie` exige ahora POST                                       | Ensayo del pipeline endurecido con datos reales frescos ANTES de gastar las ~11 h del rebuild de cordoba: build corto (~2 h) que ejercita descarga, binning, barrido, verify y tiles end-to-end. Tecnicamente no valida nada nuevo (mismo huso y serie que cordoba), el valor es de producto + ensayo. El sondeo de catalogo cazo el cambio del centro CNIG (GET -> 403 desde 2026-08): fix de una linea + mock actualizado para rechazar GET como el servicio real. El case study de ajustino.dev sigue ligado a cordoba; montilla queda servida por API + tiles                                                                                                                                                                        |
| 2026-08-13 | Comando `shade-engine verify <city>` (invariante horizonte-vs-blocker + layout + sanidad por ventanas); build_city lo ejecuta al final de cada build                                                                                                    | Invariante de dominio en vez de checksums: q > 0 exige blocker real (exacto, 0 tolerancia) y q == 0 con blocker solo es legitimo bajo medio quantum (45/255 ~ 0.176 deg; umbral 5% por banda, la corrupcion real daba 30-100%). Sirve para auditar artefactos ya desplegados (rsync incluidos), no solo builds frescos; habria cazado la corrupcion en segundos. Streaming por ventanas de 512 px: ~1 min y memoria acotada a escala ciudad                                                                                                                                                                                                                                                                                              |

| 2026-08-13 | Tiles v2: UN set de sombra por instante (edificios + arboles proyectada + other, un solo color indigo; los indices de clase sobreviven en la paleta PNG) + `canopy.pmtiles` ESTATICO por ciudad (proyeccion vertical de copas, checkbox propio) + preset con 21-jun a paso horario (26 instantes) | La capa verde mezclaba dos semanticas: "bajo copa" (estatico, dominaba el peso: ~7 MB constantes por instante) y "sombra proyectada" (movil). El corte sigue la fisica: lo que no se mueve se sirve una vez. A pie de calle importa "sombra si/no", no quien la proyecta. Manifest sigue en schema 2 con campos aditivos (`urls.shade`, `canopy_url`) y alias legacy (`urls.building` = set de sombra, `urls.vegetation` = canopy estatico, colores legacy remapeados): la consola desplegada de ajustino.dev sigue pintando coherente sin cambios; migrarla al contrato nuevo queda anotado. Instantes extra: coste lineal (~1 min y ~5-10 MB cada uno), el solsticio horario es el demo "mira la sombra moverse" |
| 2026-08-13 | Tiles v3 (misma sesion que v2, a peticion del usuario): sombra proyectada DIVIDIDA en dos sets conmutables del mismo color (building+other / trees) y preset = ESCALERA DE DECLINACION: 7 fechas canonicas a pasos de ~7.8 deg (21-dic, 07-feb, 01-mar, 21-mar, 10-abr, 04-may, 21-jun) x paso horario en luz segura = 83 instantes; manifest con campo `ladder` (dia del año -> fecha gemela) | El split recupera un toggle con caso de uso real: apagar trees = "la calle sin arbolado" (aporte del arbolado a la sombra, semilla del diagnostico de intervenciones de Fase 11); el color compartido mantiene la lectura unificada de v2. La escalera sustituye a muestrear el calendario: la declinacion es lo unico que cambia entre dias y es simetrica alrededor de los solsticios (9-ago == 4-may), asi que 7 peldaños cubren el año con error < ~4 deg y cualquier fecha resuelve via `ladder.covers` (calculado con Spencer 71, la orbita eliptica desplaza los rangos ~3 dias del calendario naive). Limites horarios verificados con elevacion > 1.4 deg en cordoba y montilla; ciudad mas oriental debera re-verificar. Viewer local: sliders fecha+hora (rejilla queda para manifests legacy), 3 checkboxes y mini sol orbitando el bbox por azimut |

| 2026-08-13 | Refinado del visor tras probar v3: capa estatica `buildings.pmtiles` derivada del landcover LiDAR (huella real de edificios, conmutable como prueba; complementa exactamente los tejados que la sombra recorta, sin Google ni API keys), arranque en fecha/hora actuales via ladder, sol anclado al borde del VIEWPORT (no del bbox: sobrevive al zoom, corrige el bearing) y compare A/B eliminado | Peticion del usuario tras usar el visor. La huella LiDAR es mas fiel que los footprints OSM del basemap y es dato propio ya calculado (landcover == BUILDING). El sol en coordenadas de pantalla evita perderlo al hacer zoom (interseccion del azimut con el rectangulo del viewport, restando el bearing del mapa). Compare A/B: sin caso de uso real con los sliders (mover la hora ES la comparacion); menos codigo y menos UI. El arrastre de sliders exigio DOM persistente: reconstruir un input range en pleno drag mata el gesto |
| 2026-08-13 | Rebuild lanzable tambien EN el VPS cartagena: servicio compose `pipeline` (imagen shade:prod, gated por profile `tools`, mount ./data rw, user 1001:1001, 6g/3cpu) que construye en staging (`--output-root data/cities-rebuild`) con swap atomico de v1 al final; la opcion local sigue viva y se elige por-run | Cero codigo Python: la imagen ya trae el CLI y este ya parametriza rutas (--output-root, --lidar-dir). El servicio api no sirve para el build (mount :ro, mem_limit 1g, uid 1000 vs 1001 dueno de /opt/shade/data). Staging en vez de in-place: build escribe en el dir final sin staging y dejaria el metadata.json viejo visible horas bajo el SceneReader abierto; con staging prod sigue sirviendo durante las 12-18 h y el corte es ~1 min. VPS medido: 7.7 GiB RAM (4.5 disponibles), swap de 4 GiB ya existente, 4 vCPU EPYC-Milan, 199 GB disco; el pico ~5 GiB entra, y parar de noche observabilidad + apps ajenas (~2 GiB medidos) lo mete entero en RAM (asumible segun el usuario). Bonus: elimina el stack WSL (VHD sobre NTFS) que causo la corrupcion original y los artefactos nacen donde se sirven |
| 2026-08-14 | Fase 8 implementada: grafo peatonal como artefacto aditivo `v1/graph/` (graph.npz + fractions.npz uint8 + graph.json), comando `shade-engine graph`; osmnx SOLO en pipeline tras la interfaz `GraphSource` | Runtime sin Overpass, sin PostGIS y sin deps nuevas en la API (numpy ya estaba). Mismo patron que canopy.tif/tiles: invisible a metadata.json y verify, backfill sin rebuild, FileNotFoundError accionable si falta. Writer con readback (contrato del postmortem) y loader con chequeos de coherencia: un rsync truncado falla al cargar, no al consultar |
| 2026-08-14 | Motor de rutas: CSR numpy propio + A\* con heapq en `shade_api.routing`; networkx DESCARTADO en runtime (el sondeo solo descartaba routers externos) | RAM (~10-25 MB por ciudad en arrays vs +223 MiB del grafo networkx del sondeo, con mem_limit 1g y 2 workers), aristas paralelas de verdad (astar_path de networkx no soporta MultiGraph; la diagonal de plaza al sol vs el soportal en sombra son paralelas reales), y docstring didactico de admisibilidad. Los predecesores guardan el INDICE de adyacencia para reconstruir la paralela elegida, y astar() rechaza costes < longitud: normalizar el peso sin escalar la heuristica rompe el optimo en silencio |
| 2026-08-14 | Fracciones de sol por arista PRECALCULADAS sobre los 83 instantes de la escalera (muestreo cada 5 m, compute_state_raster + bincount por instante); fecha via covers + interpolacion lineal entre horas; muestra fuera de dato = sol | Ruta y overlay del visor responden la misma fisica en el mismo instante, y el A\* runtime no toca ningun COG (perezoso habria metido I/O de bloques impredecible en la latencia). uint8 = error < 0.4%, muy por debajo del muestreo a 5 m. Inventar sombra fuera del raster seria fabricar justo lo que el buscador quiere oir |
| 2026-08-14 | Endpoint `GET /v1/routes/shaded?from=lat,lon&to&at&alpha` (alfa 0-10, default 1, snap 400 m): la respuesta SIEMPRE lleva la ruta corta de referencia; atribucion ODbL; noche -> status night; sin grafo -> 503 accionable. Modo circuito FUERA del MVP | "1.4 km al 12% de sol vs 1.3 km al 54%" es la respuesta util; el circuito es otro problema (sin destino, generacion heuristica de bucles) y el usuario lo dejo fuera explicitamente. La geometria del viario ES OSM: attribution obligatoria en artefacto y respuesta |
| 2026-08-14 | Fase 8.5: snap de origen/destino al PUNTO mas cercano sobre una arista (no al nodo); `nearest_node` eliminado y A\* generalizado a extremos virtuales (semillas en los dos extremos de la arista de origen con su coste parcial, pseudo-nodo destino con h=0, y el paseo directo como candidato cuando ambos pines comparten arista) | El pin cae en mitad de la calle, no en un cruce: el snap a nodo daba 64 m de error mediano en Cordoba (p90 330 m) y rutas que arrancaban en la esquina siguiente. Sobre arista baja a 27 m. El coste parcial `c*s/L` es exacto porque la fraccion de sol se guarda por arista (coste por metro constante dentro de ella), y la consistencia aguanta porque `c*s/L >= s >=` euclidea, asi que el corte temprano sigue valiendo. La tabla de segmentos se precomputa al cargar: 1,8 ms por snap sobre 46.118 segmentos, sin indice espacial ni dependencia nueva |
| 2026-08-14 | Sombra vegetal separada de la de edificio: segunda matriz uint8 `veg_shade_fraction` en fractions.npz (schema 2, loader estricto) y parametro `beta` como ESCALERA de penalizaciones `len * (1 + alfa*sol + beta*(1-sol-copa))`, con `beta <= alfa` validado (400 si no) | La copa enfria mucho mas que un muro (transpira y el suelo bajo ella no se recalienta), pero el muestreo colapsaba los tres estados de sombra en "no sol". Penalizar y nunca premiar: un bonus negativo pondria el coste por debajo de la longitud y romperia la heuristica en silencio. Bump de schema en vez de campo opcional porque prod aun no tenia grafos desplegados y regenerar cuesta 22 s (montilla) / ~4 min (cordoba); un loader tolerante seria codigo muerto para siempre. CONSECUENCIA ACEPTADA: con beta > 0 el invariante `sol(sombreada) <= sol(corta)` deja de valer (medido en Cordoba: +42 m de sol a cambio de +128 m bajo arbolado), que es justo lo que se pidio |
| 2026-08-14 | Alternativas por BARRIDO DE ALFAS (0, 0.5, 1, 2, 4, 8) + dedup por secuencia de tramos + filtro de dominadas con umbral de mejora del 5%; k-shortest-paths (Yen) descartado | Reutiliza el A\* existente (~10 ms por pasada) y cada alfa es un gusto distinto, asi que cada optimo es un punto no dominado; Yen es mas complejo y devuelve primos hermanos de la misma ruta. El filtro de dominancia hace falta aunque parezca redundante: con beta > 0 el coste escalarizado no es monotono en (longitud, sol). El umbral del 5% es de producto, no de matematicas: alfas vecinos daban rutas 2 m mas largas con 4 m menos de sol sobre 1,4 km, dos filas identicas en pantalla. Limite documentado: la suma ponderada solo alcanza la envolvente convexa del frente, el barrido es una muestra |
| 2026-08-14 | Routing por ACERA descartado por ahora (sondeo Overpass en la misma sesion) | Sin datos: Cordoba solo tiene ~9% de calles con acera mapeada como way (439/4.942) y Montilla cero (0 ways, 1 nodo de cruce). Apoyarse en OSM daria un servicio que funciona en cuatro calles de una ciudad y en ninguna de la otra. La via realista, si se retoma, es sintetizar las dos aceras por offset del eje y muestrear el sol por lado (el motor ya soporta paralelas entre los mismos nodos); penalizar cruces exigiria partir nodos por lado |
| 2026-08-14 | Fase 8.6: la API expone `segments` (descomposicion por arista) SOLO en el leg `shaded`, crudos y SIN clasificar | Colorear exige una historia categorica a partir de dos fracciones continuas, y el umbral es presentacion: la API devuelve los numeros y el cliente elige (argmax, empates a favor del sol) y fusiona por clase (39 -> 21 tramos en Cordoba). Solo el leg activo porque es el unico que se colorea: la corta es referencia discontinua y las alternativas candidatas finas; asi el coste es +6,6 KB sobre 22,4 (medido 29,4 KB con alternativas) en vez de multiplicar por siete. Y de noche, con las dos fracciones a 0, clasificar pintaria la ciudad entera de sombra de edificio: el cliente corta por `status` |
| 2026-08-14 | Bocadillo por HOVER anclado en el cursor + tabla unica en el panel; pulsar una alternativa la ADOPTA (fija su alfa y reconsulta) | Un bocadillo permanente por ruta se solaparia justo donde las rutas van juntas, que es casi todo el recorrido, y con alternativas serian seis. La adopcion elimina el resaltado dorado y el estado `selectedAlternative`: habia dos formas de senalar una ruta y solo una la elegia de verdad; ademas unifica la lista con los presets de alfa. La fila activa se detecta comparando metricas y no el alfa, porque el filtro de Pareto (umbral 5%) puede haber colapsado el alfa pedido. Trampas ancladas en codigo: los popups sobreviven a `setStyle` (cierre al principio de `render()`), `queryRenderedFeatures` revienta sobre una capa que desaparecio (guard con `getLayer`), y su orden de hits no es contractual (prioridad explicita) |

Pendientes de decidir:

- Subir de 64 a 128 sectores de horizonte (anotado 2026-08-13, sin fecha): el
  mejor ratio coste/precision si la validacion de campo señala el borde
  azimutal de las sombras (~5.6 deg por sector hoy; el borde de una sombra de
  20 m baila ~1 m). Coste: x2 barrido (~22 h el rebuild exact de cordoba) y
  x2 el horizon.tif (~3.6 GB). Decidir DESPUES del paseo de validacion; no
  pagar computo antes de medir el error real. Bajar de 1 m/px queda
  descartado salvo dato nuevo: LIDA3 (5 pt/m2) no soporta 0.5 m reales y el
  barrido seria x8.
- Motor de rutas y estrategia de precalculo solar (Fase 8): RESUELTAS
  2026-08-14 (ver registro): CSR numpy propio + A\* heapq en la API, y
  precalculo uint8 sobre los 83 instantes de la escalera
- Cobertura de parking mas alla de las 21 zonas azules (roadmap, sin fecha): las
  fuentes ya se agotaron en la investigacion de Fase 5 (visor municipal roto,
  OSM ~1.2% del viario, Overture pierde los tags parking:\*). Idea a explorar:
  extraer senalizacion/marcas viales de imagenes a pie de calle (Google Street
  View, Mapillary) con un modelo de vision y generar nuestro propio
  parking.geojson. Requiere evaluar licencias de las imagenes ademas del modelo.

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
- 2026-07-13 (revision del visor, acuerdo SIN implementar aun): la sombra
  vegetal esta inflada por diseno del MVP: canopy = landcover==VEGETATION sin
  umbral de altura (clases LiDAR 3/4/5 juntas), y el 55% de los pixeles de
  vegetacion de la ciudad mide < 2.5 m de CHM (36% en el centro es < 1 m).
  Acordado con el usuario: (1) canopy solo donde CHM >= 2.5 m + sieve de area
  minima ~8 m2 sobre la mascara (las copas siguen proyectando sombra via
  horizonte; no hay que re-barrer, solo re-derivar mascara y regenerar tiles);
  (2) tiles con toggle de vegetacion via DOS pmtiles por instante
  (edificios+otros / vegetacion) + mascara de tejados (interior de edificio
  transparente: sombra a nivel de calle, el basemap ya dibuja edificios);
  (3) mantener 16 instantes y anadir verbosidad al comando tiles (tiempos por
  fase, tamanos, tabla resumen) antes de decidir ampliarlos (~53 s y ~10 MB
  por instante, coste lineal); (4) parking se queda en 21 zonas (idea de
  vision sobre imagenes a pie de calle anotada en Pendientes de decidir).
  OJO: el umbral de canopy cambia tambien /v1/shade, timeline y las
  shade_fraction del parking (bajaran); recapturar fixtures de ajustinodev
  tras regenerar. Siguiente sesion: planificar esta tanda en plan mode.
- 2026-07-13 (revision del visor implementada): dos commits de pipeline
  (canopy.tif + tiles divididos) y regeneracion completa en local. Numeros
  reales de Cordoba: la mascara de copa retiene el 43.2% de los pixeles de
  vegetacion (vegetacion cruda 40.7% de la ciudad -> copa 17.6%; cae el
  56.8% que era cesped/setos/cultivos, en linea con el 55% estimado en la
  revision). canopy.tif = 4.3 MB, deriva en ~1 min con `shade-engine canopy
cordoba`. Tiles: 32 pmtiles (2 por instante x 16), 161 MB, ~15 min de
  build (el split apenas encarece: la mascara de tejados y la copa reducida
  disparan los tiles transparentes omitidos). Orden ops OBLIGATORIO al
  desplegar: rsync de canopy.tif ANTES del push (el SceneReader lo exige al
  arrancar y el push autodespliega), luego rsync de tiles/ con --delete
  (borra los 16 pmtiles viejos de schema 1), luego push, luego recaptura de
  fixtures de ajustinodev contra la API viva (los valores de sombra vegetal
  cambian) y commit web con el toggle de vegetacion.
- 2026-07-13 (exploracion confort termico, SIN implementar): sesion de producto
  sobre refugios climaticos y confort termico. Reencuadre acordado: el horizon
  raster ya es un SVF (Sky View Factor) precomputado, asi que la parte cara del
  confort radiativo esta hecha; falta meteo + balance radiativo, no geometria.
  Documentado como seccion "Vision post-MVP: motor de confort termico" + Fases
  9-11 boceto: 9 (SVF + exposicion solar acumulada, casi gratis e independiente
  de la Fase 8), 10 (MRT/UTCI con AEMET + calibracion SOLWEIG + validacion de
  campo; el salto de credibilidad), 11 (rutas frescas sobre el grafo de la Fase 8
  - diagnostico urbanistico). Orden natural 9 -> 10 -> 11; sin fechas ni prioridad
    todavia. Trampa clave anotada: sombra != frescor (la MRT de las superficies
    recalentadas manda). Diferenciacion vs shademap.app: MRT/SVF a 1 m desde LiDAR
    real, no footprints OSM. Encaje B2G (adaptacion al calor) mas defendible que el
    aparcamiento. Siguiente si se retoma: planificar la Fase 9 (es la barata).
- 2026-08-13 (postmortem corrupcion + hardening): el usuario detecto en el visor
  que los instantes de tarde no pintaban sombra. Diagnostico completo en el
  registro (3 filas de hoy): horizon.tif de cordoba v1 corrupto en las bandas
  45-64, prod incluido; el barrido era correcto y la perdida fue de I/O. Hecho en
  esta sesion: labana ELIMINADA a peticion del usuario (yaml + doc + 9.3 GB de
  datos; sus fixes de codigo se conservan), venv y hook pre-commit regenerados
  (el repo se movio de ~/shade-engine a ~/proyectos/shade-engine y los shebangs
  apuntaban a la ruta vieja), write_cog endurecido (INTERLEAVE=BAND,
  BIGTIFF=IF_SAFER, readback banda a banda), flush de memmaps, comando
  `shade-engine verify` integrado en build, tests (177 verdes) y cog.md ampliado.
  OJO: PROD SIGUE SIRVIENDO EL HORIZONTE CORRUPTO hasta el rebuild. Siguiente:
  montilla como segunda ciudad (ensayo del pipeline endurecido, decidido
  local + prod) y despues el rebuild nocturno de cordoba.
- 2026-08-13 (runbook rebuild nocturno de cordoba, lo lanza el usuario; desde
  la tarde hay DOS opciones y se elige por-run):

  OPCION A - build local (WSL) + rsync. Precondiciones: hardening pusheado con
  CI verde, montilla construida y verificada, >= 30 GB libres en el disco
  fisico de Windows, sin cargas pesadas en WSL. Pasos:
  1. `mv data/cities/cordoba/v1 data/cities/cordoba/v1.pre-rebuild` (forense y
     rollback; se borra al validar).
  2. `uv run shade-engine build cordoba` (exact, ~11 h; los 90 LAZ ya estan en
     data/lidar/cordoba, no re-descarga; el build ahora se auto-verifica).
  3. Por la manana: `uv run shade-engine verify cordoba`; perfil de horizonte en
     Tendillas/Corredera/Potro con sectores oeste poblados; `predict` del kit y
     comparar las tardes contra la hoja vieja (deben aparecer sombras nuevas).
  4. `cp data/cities/cordoba/v1.pre-rebuild/tiles/basemap.pmtiles
data/cities/cordoba/v1/tiles/` y `uv run shade-engine tiles cordoba`
     (OJO: con la escalera de declinacion son 83 instantes x 2 sets, estimar
     ~2 h y ~0.5-0.7 GB; el basemap no se regenera).
  5. Datos a prod (orden obligatorio): rsync de artefactos SIN tiles/ ->
     reiniciar la api del VPS (el SceneReader cachea bloques en RAM) -> rsync de
     tiles/ con --delete -> recapturar fixtures de ajustinodev contra la API
     viva (los valores de tarde cambian todos) y commit en ese repo.
  6. Sanidad en vivo: /v1/shade de un punto de calle a las 20:00 de junio da
     sombra de edificio; visor publico con overlay en 17:00 y 20:00 de junio.

  OPCION B - build EN el VPS cartagena (servicio compose `pipeline`; construye
  en staging asi que prod SIGUE SIRVIENDO durante el build y el corte es la
  ventana de swap, ~1 min). Precondiciones: compose con el servicio pipeline
  desplegado (deploy normal), cache de LAZ subido. Pasos:
  1. Una vez, subir el cache de LAZ (~5.7 GB, queda como cache permanente):
     `rsync -a --info=progress2 data/lidar/cordoba/ cartagena:/opt/shade/data/lidar/cordoba/`
  2. Smoke del servicio (1 min; valida imagen, mounts y uid antes de las 12-18 h):
     `ssh cartagena "cd /opt/shade && docker compose run --rm pipeline shade-engine verify montilla"`
  3. Noche, margen de RAM opcional: `docker stop` del stack de observabilidad
     y las apps ajenas (~2 GiB medidos; restart unless-stopped respeta el stop)
     y `docker start` al dia siguiente. Sin parar nada tambien entra: 4.5 GiB
     disponibles + 4 GiB de swap frente a un pico de ~5 GiB en el binning
     (primeras ~2 h). shade api/db NO se paran hasta el swap.
  4. En tmux del VPS (`tmux new -s rebuild`):
     `cd /opt/shade && docker compose run --rm pipeline shade-engine build cordoba --lidar-dir data/lidar/cordoba --output-root data/cities-rebuild 2>&1 | tee data/build-cordoba-$(date +%F).log`
     (--lidar-dir fuerza LocalDirectory: cero red, cero CNIG; el build se
     auto-verifica; estimar 12-18 h, el barrido es ~single-core y las 11h21
     locales eran en otro CPU).
  5. Por la manana: `docker compose run --rm pipeline shade-engine verify
cordoba --output-root data/cities-rebuild`; despues el basemap (el v1
     vivo sigue intacto en su sitio, no hace falta v1.pre-rebuild todavia):
     `mkdir -p data/cities-rebuild/cordoba/v1/tiles && cp data/cities/cordoba/v1/tiles/basemap.pmtiles data/cities-rebuild/cordoba/v1/tiles/`
     y `docker compose run --rm pipeline shade-engine tiles cordoba
--output-root data/cities-rebuild` (~2-3 h).
  6. Swap (~1 min, de noche si se quiere): `docker compose stop api` ->
     `mv data/cities/cordoba/v1 data/cities/cordoba/v1.pre-rebuild && mv data/cities-rebuild/cordoba/v1 data/cities/cordoba/v1`
     -> `docker compose up -d api` (migrate re-corre, es idempotente). Caddy
     no cambia (sirve ficheros de data/cities tal cual) y no hay cache stale:
     los nombres v3 son URLs nuevas y el unico reutilizado, basemap.pmtiles,
     es identico byte a byte.
  7. Sanidad en vivo igual que A.6 + recapturar fixtures de ajustinodev. Tras
     unos dias verdes: `rm -rf data/cities/cordoba/v1.pre-rebuild data/cities-rebuild`.
     Rollback mientras tanto: parar api, deshacer los dos mv, arrancar api.

- 2026-08-13 (montilla en produccion): ensayo del pipeline endurecido COMPLETO
  en la misma sesion del postmortem. Sondeo de catalogo 25/25 tiles LIDA3 (AND
  2024), build exact 1h 35m (537 MiB; horizon.tif 442 MiB ya en interleave
  BAND), verify 6/6, los 64 sectores poblados en toda la ciudad (92-96% no
  nulos tambien en las bandas 45-64 que cordoba tenia muertas), timeline con
  sombra vegetal a las 20:15 y de edificio a las 21:30 el 21 jun (az ~290, el
  rango antes muerto), tiles 27 MiB con la firma sana (el building de las
  20:00 de junio es el MAYOR de los 4 instantes, como manda la fisica),
  basemap Protomaps build 20260812. En vivo tras rsync + restart de la api:
  /v1/cities lista [cordoba, montilla], /v1/shade de montilla responde sombra
  de tarde, manifest 200 con CORS y pmtiles Range 206 immutable. El viewer
  local (viewer/, fuera de git) queda con fallback [cordoba, montilla].
  RECORDATORIO: el rebuild nocturno de cordoba sigue PENDIENTE (runbook en la
  nota anterior); su horizonte corrupto sigue en prod hasta entonces.
- 2026-08-13 (tiles v2 + preset horario): implementado y desplegado para
  montilla en la misma sesion (registro: fila "Tiles v2"). El set de sombra
  por instante queda unificado en un color (la proyectada de arboles incluida)
  y las copas pasan a `canopy.pmtiles` estatico con checkbox propio; el
  solsticio de verano va a paso horario (26 instantes en total, 1m56s y 30 MB
  para montilla, regenerado y rsync-eado con --delete). El viewer local lee el
  contrato nuevo con fallback legacy (cordoba sigue en el manifest viejo hasta
  su rebuild y el visor la pinta igual). PENDIENTE ANOTADO: migrar la consola
  del case study (repo ajustinodev) a `urls.shade` + `canopy_url`; mientras,
  los alias legacy del manifest la mantienen coherente (su toggle de
  vegetacion pinta ahora las copas estaticas). El runbook del rebuild
  nocturno de cordoba NO cambia: `tiles cordoba` ya produce el formato nuevo.
  Futuro apuntado en Pendientes de decidir: subir a 128 sectores tras la
  validacion de campo; bajar de 1 m/px descartado.
- 2026-08-13 (tiles v3 + viewer): tercera iteracion del dia, pedida por el
  usuario tras ver v2: split de la sombra proyectada (building/trees, mismo
  color, toggles independientes), escalera de declinacion de 7 fechas x paso
  horario (83 instantes) con campo `ladder` en el manifest (dia del año ->
  gemela; 365/365 dias cubiertos, verificado), y en el viewer local sliders de
  fecha+hora (la rejilla queda para manifests legacy), 3 checkboxes y mini sol
  orbitando el bbox por azimut con color/tamaño por elevacion. Montilla
  regenerada (9m15s, 167 pmtiles, 137 MB) y en prod; verificado en vivo que
  2026-08-09 resuelve al peldaño 2026-05-04 (+15.73) y que el sol aparece al
  oeste a las 19:00. Docs: solar-geometry.md ampliado con declinacion y
  escalera; adding-a-city.md al contrato nuevo; runbook de cordoba actualizado
  (tiles ~2 h con 83 instantes). La consola de ajustinodev sigue via alias
  legacy (url = building cast, vegetation = canopy); su migracion sigue
  pendiente y ahora incluye sliders si se quiere paridad.
- 2026-08-13 (refinado del visor tras v3): cuarta tanda del dia a peticion del
  usuario. Nuevo `buildings.pmtiles` estatico por ciudad (huella de edificios
  del landcover LiDAR, checkbox de prueba "para ir viendo diferencias"; encaja
  exacto con los tejados que la sombra recorta), arranque del visor en la
  fecha/hora actuales resueltas via ladder, sol anclado al borde del VIEWPORT
  (sobrevive al zoom; en pantalla, restando el bearing) y compare A/B
  eliminado. Bug real cazado en vivo: la capa se llamaba "buildings" y el
  basemap de Protomaps ya posee ese id de capa -> MapLibre rechazaba el estilo
  ENTERO y el mapa quedaba negro; renombrada a "lidar-buildings". Montilla
  regenerada (9m08s, 168 pmtiles, 137.7 MiB) y en prod. El arrastre de los
  sliders se arreglo antes con DOM persistente (reconstruir un input range en
  pleno drag mata el gesto). Port a ajustinodev VALORADO y pendiente de
  decision: ShadeConsole.astro (845 lineas, chips propios, colores
  hardcodeados, snapshot de manifest en public/data/) sigue funcionando via
  alias legacy; portar sliders+ladder+toggles+sol es una sesion corta y
  conviene DESPUES del rebuild de cordoba (asi su manifest vivo ya es v3).
- 2026-08-13 (rebuild lanzable desde cartagena): quinta tanda del dia. El
  rebuild de cordoba se puede lanzar ahora tambien EN el VPS, eligiendo
  por-run (registro: fila "Rebuild lanzable tambien EN el VPS"). Cambio unico
  de codigo: servicio `pipeline` en compose.yml; cero Python (el CLI ya
  parametrizaba --output-root y --lidar-dir). Hechos medidos en vivo que
  fijaron el diseno: uid mismatch (imagen `app`=1000 vs ductual=1001 dueno de
  /opt/shade/data -> user: 1001:1001 y HOME=/tmp porque ese uid no tiene
  entrada passwd en la imagen), swap de 4 GiB YA existia en el VPS (la
  preocupacion de RAM de la manana era menor de lo temido), co-tenants ~2 GiB
  parables de noche (docker stats), tmux instalado, compose v5.3.1 (el
  targeting explicito de un servicio con profile lo activa solo). compose.yml
  validado contra el docker del VPS por stdin (`docker compose -f - config`:
  PARSE_OK y el servicio resuelve bind /opt/shade/data rw + 6g + 3cpu).
  Runbook OPCION B anadido a la nota del runbook de esta misma fecha; la
  OPCION A local queda intacta. Pendiente igual que antes: lanzar el rebuild
  (ahora con smoke previo `verify montilla` via el servicio pipeline) y
  despues el port de ajustinodev.
- 2026-08-14 (REBUILD DE CORDOBA EJECUTADO, opcion B): el usuario lo lanzo la
  noche del 13 (~20:25) y se completo el dia 14. Numeros reales: build 12h42m
  (barrido 3m32s/tile x 224; RAM sin incidentes con observabilidad parada esa
  noche y rearrancada por la manana), 3.7 GiB de artefactos (horizon.tif 3.1
  GiB ya en interleave BAND), auto-verificacion + verify redundante 6/6;
  tiles 1h08m (83 instantes, 168 pmtiles, 910 MiB, muy por debajo de la
  estimacion de 2-3 h); swap con ~15 s de api parada (mv v1 ->
  v1.pre-rebuild, staging -> v1, up -d). Sanidad en vivo: /v1/shade en
  Tendillas 21-jun 20:00 responde SOMBRA con sol en az 286 (sector 50, el
  rango que estuvo muerto), index.json 200 con CORS, pmtiles Range 206,
  /v1/cities lista [cordoba, montilla]. PROD QUEDA SANA por primera vez
  desde el postmortem. v1.pre-rebuild se conserva unos dias como rollback
  (borrar junto a data/cities-rebuild al validar con calma). Pendiente
  siguiente: recapturar fixtures de ajustinodev y el port de la consola.
- 2026-08-14 (Fase 8 implementada, ops pendiente): tres commits en una
  sesion: pipeline (`shade-engine graph`: artefacto v1/graph/ con fracciones
  uint8 sobre la escalera), api (`/v1/routes/shaded`: CSR + A\* propio, snap,
  interpolacion horaria, ODbL) y viewer local (modo ruta con 2 clicks,
  input de alfa, re-consulta al mover sliders, resumen comparativo).
  Verificado end-to-end en local sobre la ciudad sintetica cube (build +
  graph + tiles + API + viewer + captura Playwright): la ruta sombreada
  rodea la sombra de invierno del cubo (40 m al 40% de sol vs 40 m al 70%;
  empate de longitud resuelto por sol, visible en el mapa). 213 tests
  verdes, mypy y ruff limpios. Notas de learning: routing-graph.md y
  a-star.md. deps nuevas SOLO en pipeline: osmnx 2.1 + networkx (wheels
  py3/cp314 verificados, pyogrio incluido). El fixture cube local queda en
  data/cities/cube (gitignored, regenerable via tests/graph_fixture). El
  viewer gano SHADE_VIEWER_API para apuntar a un puerto local distinto de 8000. PENDIENTE para cerrar la fase (seccion "Ops pendiente" de Fase 8):
  push a main, `shade-engine graph cordoba|montilla` via el servicio
  pipeline del VPS (Overpass accesible; 1-2 h cordoba por los 83 rasteres
  de estado), restart de la api, smoke publico y criterio de salida sobre
  Cordoba real en el visor. Despues: valorar el port del modo ruta a la
  consola de ajustinodev.
- 2026-08-14 (validacion local sobre Montilla real): los artefactos SI
  estaban en local (un despiste de cwd los escondio en la sesion anterior:
  data/ tiene cordoba 2.5G del build de JULIO -- horizonte corrupto, no
  usar para el grafo --, montilla 677M del build sano de agosto, el cache
  lidar y assets). `shade-engine graph montilla` en local contra Overpass
  real: 15 s, 617 nodos / 884 aristas / 69 km, cache OSM 600K en
  data/cache/osm. Criterio de salida CUMPLIDO en el viewer con la fecha y
  hora de hoy (18:00, sol az 262): corta 961 m al 73% de sol vs sombreada
  1.16 km al 35% por calles distintas; captura route-mode-montilla.png.
  El grafo de montilla queda listo para subir a prod (paso 2 de la ops de
  Fase 8); el de cordoba debe construirse EN el VPS.
- 2026-08-14 (cordoba sana en local + grafo + criterio cumplido): rsync
  espejo del v1 sano del VPS (4.6G con tiles v3, --delete sobre la copia
  corrupta de julio; verify 6/6 y sanidad 20:00-jun = shade con az 286).
  El usuario lanzo `shade-engine graph cordoba` en local: 3m57s, 13,135
  nodos / 19,756 aristas no dirigidas / 230,980 muestras (cuadra con el
  sondeo de julio: 39k dirigidas ~ 19.7k tras dedup), artefacto 3.3 MB.
  Criterio de salida verificado en el viewer sobre Cordoba (21-jun 19:00,
  cruce E-O del casco: corta 1.41 km 47% sol vs sombreada 1.57 km 18%,
  capturas route-mode-cordoba.png). La ops de Fase 8 queda reducida a:
  push + rsync de los dos graph/ + restart api + smoke (ya no hay build
  de grafo pendiente en el VPS). El aviso de "cordoba local corrupta"
  queda OBSOLETO: data/cities/cordoba/v1 es ahora espejo del rebuild sano.
