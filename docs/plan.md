# Shade Engine - Plan de implementacion por fases

Documento vivo. Cada sesion de trabajo toma items de la fase activa, los marca al
completarlos y anota decisiones en el registro del final. El spec de referencia es
[shade-engine-mvp.md](shade-engine-mvp.md).

## Estado global

| Fase | Nombre                             | Estado    |
| ---- | ---------------------------------- | --------- |
| 0    | Bootstrap del repo                 | pendiente |
| 1    | core/: modelo solar + horizonte    | pendiente |
| 2    | pipeline/: de LAZ a artefactos COG | pendiente |
| 3    | api/: consulta de sombra (sin DB)  | pendiente |
| 4    | Cordoba real + validacion de campo | pendiente |
| 5    | Parking                            | pendiente |
| 6    | Despliegue en cartagena            | pendiente |
| 7    | Visualizacion + integracion Astro  | pendiente |

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

- [ ] git init + LICENSE (decidir MIT vs Apache-2.0) + README con vision y roadmap (seccion 11 del spec)
- [ ] Estructura monorepo: `api/`, `pipeline/`, `core/`, `cities/`, `tests/`, `docker/`, `docs/learning/`
- [ ] Tooling: uv (workspace), ruff, mypy, pytest, pre-commit
- [ ] CLAUDE.md del repo con las instrucciones didacticas de la seccion 10 del spec
- [ ] Verificar wheels Python 3.14 para rasterio/shapely/pdal-bindings; decidir 3.14 vs 3.13 por paquete
- [ ] CI GitHub Actions: lint + mypy + pytest
- [ ] docker-compose dev minimo (sin postgis todavia, apunte 4)
- [ ] Incorporar al spec los apuntes aceptados (DTM/observador, max_distance)

Criterio de salida: CI verde con al menos un test trivial; README publicable.

## Fase 1 - core/: modelo solar + consulta de horizonte

Objetivo: motor de sombra correcto sobre rasteres sinteticos.

- [ ] Modulo solar sobre pvlib: azimut/elevacion para (lat, lon, t, tz); convenciones documentadas (azimut 0=N, horario)
- [ ] Lectura de horizonte: ventana puntual sobre raster multibanda, interpolacion entre sectores de azimut
- [ ] `is_shaded(point, t)`: observador DTM+1.6m; caso especial pixel bajo copa (apunte 1)
- [ ] Timeline diario: barrido de la trayectoria solar (paso configurable 5-10 min), fusion de intervalos
- [ ] Golden test: cubo de 20 m sobre DSM plano, sombras calculadas a mano (solsticios + equinoccio)
- [ ] Segundo sintetico con "arbol" para validar tipo de sombra
- [ ] docs/learning: CRS y por que 25830; azimut/elevacion/declinacion; algoritmo de horizonte por sectores; DSM vs DTM vs CHM

Criterio de salida: golden tests pasando; timeline continuo con amanecer/atardecer correctos.

## Fase 2 - pipeline/: de LAZ a artefactos COG

Objetivo: `shade-engine build <city>` produce artefactos validos desde LiDAR PNOA.

- [ ] CLI con typer; carga de config YAML de ciudad (`cities/cordoba.yaml` como en spec seccion 4)
- [ ] Driver de descarga PNOA por bbox + buffer (buffer >= max_distance, apunte 2)
- [ ] DSM (primeros retornos) + DTM (clase 2) con PDAL -> rasterio, 1 m/pixel configurable
- [ ] Raster landcover (building/vegetation/ground) desde clases LiDAR
- [ ] Raster de horizonte: 64 bandas uint8 cuantizado, observador en DTM+1.6m, obstaculos DSM, tiling con buffer, max_distance configurable
- [ ] DECISION ABIERTA (apunte 3): clasificacion tipo de sombra, ray-march runtime vs bandas por sector; elegir y documentar
- [ ] Export COG (deflate) + metadatos versionados (`cities/cordoba/v1/`); disco local en dev
- [ ] Fixture LAZ minimo + smoke test del pipeline en CI (el build pesado NO corre en CI)
- [ ] Dockerfile del pipeline (conda-forge/pixi para PDAL, apunte 5)
- [ ] docs/learning: clases y retornos LiDAR, COG y lecturas por ventana

Criterio de salida: pipeline corre sobre el fixture y produce COGs validos que core/ sabe leer.

## Fase 3 - api/: consulta de sombra (sin DB)

Objetivo: API publica de sombra leyendo COGs.

- [ ] FastAPI + settings por env; sin Postgres todavia (apunte 4)
- [ ] `GET /v1/cities` (desde YAMLs + metadatos de artefactos)
- [ ] `GET /v1/shade` y `GET /v1/shade/timeline`
- [ ] `/healthz` + endpoint de metadatos de artefactos cargados
- [ ] Lectura COG por ventana con cache LRU acotado por config
- [ ] CORS por env, rate limiting (slowapi), campo `attribution`, versionado `/v1`
- [ ] Semantica de timezone: ISO 8601, sin offset -> TZ de la ciudad
- [ ] Cache-Control: cacheable con `at` explicito, TTL corto o no-cache con "ahora" implicito
- [ ] Tests de integracion contra artefactos del fixture; OpenAPI como doc publica

Criterio de salida: API respondiendo sobre los artefactos del fixture, tests de integracion verdes.

## Fase 4 - Cordoba real + validacion de campo

Objetivo: la mejor demo posible: prediccion vs realidad.

- [ ] Ejecutar pipeline con bbox urbano de Cordoba; medir tamano/tiempos (validar estimacion seccion 3 del spec; fallback 2 m/pixel o 32 sectores si excesivo)
- [ ] Validacion de campo: puntos conocidos, fotos con hora vs prediccion; material para README
- [ ] Ajustar precision segun lo detectado (interpolacion, snapping de puntos que caen sobre edificio)

Criterio de salida: predicciones correctas en la mayoria de puntos de contraste, documentado.

## Fase 5 - Parking

Objetivo: caso de uso aparcamiento completo.

- [ ] PostGIS en compose + SQLAlchemy 2 + Alembic (primera migracion); verificar compat PostGIS<->Postgres antes de fijar imagen
- [ ] `shade-engine import-layer <city> parking`
- [ ] Digitalizar `parking.geojson` del centro de Cordoba (schema seccion 5.1 del spec)
- [ ] `GET /v1/parking/nearby` con estado de sombra en `at` y `shaded_until`

Criterio de salida: consulta nearby devuelve tramos con sombra correcta contra timeline.

## Fase 6 - Despliegue en cartagena

Objetivo: `shade.ajustino.dev` en produccion.

- [ ] Dockerfile api multi-stage; compose prod: api + postgis + volumen local de COGs (sin minio)
- [ ] uvicorn con --proxy-headers y --forwarded-allow-ips; limites de workers/RAM/cache por config
- [ ] Caddy: subdominio + TLS; verificar cache CloudFront con los Cache-Control de Fase 3
- [ ] CORS prod: https://ajustino.dev y https://\*.ajustino.dev

Criterio de salida: API publica respondiendo con datos reales de Cordoba desde el VPS.

## Fase 7 - Visualizacion + integracion Astro

Objetivo: mapa de sombra consumible desde la web.

- [ ] PMTiles estaticos de sombra a horas clave (o tiles PNG dinamicos; decidir y documentar)
- [ ] Integracion en la web Astro externa
- [ ] docs: como anadir una ciudad, formato de capas

Criterio de salida: mapa de sombra visible en ajustino.dev.

## Transversal (todas las fases)

- Cada concepto geo nuevo: nota corta en `docs/learning/` en el mismo commit (spec seccion 10)
- Docstrings didacticos en `core/` (formulas, unidades, convenciones de signo)
- Decisiones tecnicas con alternativas: exponer opciones y porque, y anotarlas en el registro

---

## Registro de decisiones

| Fecha      | Decision                                             | Porque                                          |
| ---------- | ---------------------------------------------------- | ----------------------------------------------- |
| 2026-07-10 | Horizonte con observador en DTM+1.6m, obstaculos DSM | Evita error bajo copa y sobre tejado (apunte 1) |
| 2026-07-10 | Postgres pospuesto a Fase 5                          | Fases 0-4 no necesitan DB (apunte 4)            |
| 2026-07-10 | Pipeline contenerizado desde el inicio               | PDAL solo fiable via conda-forge (apunte 5)     |

Pendientes de decidir:

- LICENSE: MIT vs Apache-2.0 (Fase 0)
- Python 3.14 vs 3.13 por paquete, segun wheels disponibles (Fase 0)
- Clasificacion tipo de sombra: ray-march runtime vs bandas por sector (Fase 2)
- PMTiles estaticos vs tiles PNG dinamicos (Fase 7)

## Notas entre sesiones

(espacio para dejar contexto a la siguiente sesion: donde se quedo el trabajo, bloqueos, ideas)
