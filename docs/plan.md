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

- [ ] Driver de descarga PNOA (movido desde Fase 2): envolver los endpoints internos del centro de descargas CNIG tras la interfaz `LidarSource`, con fallback documentado de descarga manual al directorio local
- [ ] Ejecutar pipeline con bbox urbano de Cordoba; medir tamano/tiempos (validar estimacion seccion 3 del spec; fallback 2 m/pixel o 32 sectores si excesivo; probar el modo geometric del barrido)
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

| Fecha      | Decision                                                                                | Porque                                                                                                                                                                                                                                                                        |
| ---------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-07-10 | Horizonte con observador en DTM+1.6m, obstaculos DSM                                    | Evita error bajo copa y sobre tejado (apunte 1)                                                                                                                                                                                                                               |
| 2026-07-10 | Postgres pospuesto a Fase 5                                                             | Fases 0-4 no necesitan DB (apunte 4)                                                                                                                                                                                                                                          |
| 2026-07-10 | Pipeline contenerizado desde el inicio                                                  | PDAL solo fiable via conda-forge (apunte 5)                                                                                                                                                                                                                                   |
| 2026-07-10 | Licencia MIT                                                                            | Eleccion del usuario; permisiva y minima                                                                                                                                                                                                                                      |
| 2026-07-10 | README en ingles; docs/ y docs/learning/ en castellano                                  | Alcance OSS vs objetivo didactico personal                                                                                                                                                                                                                                    |
| 2026-07-10 | Python 3.14 en todo el workspace                                                        | Wheels cp314 verificados en PyPI para rasterio 1.5.0, shapely 2.1.2, pyproj 3.7.2, numpy 2.5.1; pvlib puro                                                                                                                                                                    |
| 2026-07-10 | Repo publico ya: github.com/aJustDev/shade-engine                                       | Unica forma de verificar el criterio "CI verde"                                                                                                                                                                                                                               |
| 2026-07-10 | Commits en ingles (convencion en CLAUDE.md)                                             | Coherencia con repo OSS publico en ingles                                                                                                                                                                                                                                     |
| 2026-07-10 | docker-compose aplazado a Fase 2/5                                                      | Sin DB ni servicios que orquestar todavia                                                                                                                                                                                                                                     |
| 2026-07-10 | Elevacion solar APARENTE (con refraccion)                                               | Es el sol que se ve; relevante al amanecer/atardecer (~0.5 grados en el horizonte)                                                                                                                                                                                            |
| 2026-07-10 | Horizonte: interpolacion azimutal lineal circular                                       | Nearest erraria hasta medio sector (~2.8 grados con 64), metros de frontera de sombra                                                                                                                                                                                         |
| 2026-07-10 | Horizonte: muestreo espacial nearest, no bilinear                                       | Promediar perfiles a traves de una pared mezcla tejado con calle: angulos sin sentido fisico                                                                                                                                                                                  |
| 2026-07-10 | `compute_horizon_reference` (fuerza bruta) en core                                      | Oraculo para validar la version vectorizada/tileada del pipeline (Fase 2) sobre los mismos fixtures                                                                                                                                                                           |
| 2026-07-10 | Tipo de sombra: ray-march a medio pixel + fallback al sector contribuyente              | La interpolacion azimutal difumina bordes de obstaculo ~medio sector; en esa banda el tipo se atribuye al sector que aporto el angulo. Paso de medio pixel: uno entero salta esquinas                                                                                         |
| 2026-07-11 | laspy + lazrs (pip puro) en vez de PDAL; REVIERTE "pipeline contenerizado" (2026-07-10) | lazrs publica wheel cp314 y laspy es Python puro: pipeline entero instalable con uv, smoke test e2e en CI sin Docker. Solo necesitabamos binning, que numpy hace en pocas lineas. Dockerfile aplazado                                                                         |
| 2026-07-11 | Tipo de sombra en produccion: raster de clase por sector (cierra apunte 3)              | El argmax del barrido ya sabe que celda bloquea cada sector: guardar su landcover cuesta casi nada y la consulta pasa a 1 lectura de pixel (vs 3 ventanas COG del ray-march). Deflate comprime clases casi gratis (24K el cubo). Ray-march conservado como oraculo de paridad |
| 2026-07-11 | Driver PNOA aplazado a Fase 4; Fase 2 usa driver de directorio local                    | CNIG sin API documentada (visor con endpoints internos jQuery, fragiles). No bloquea el criterio de salida de la fase; en Fase 4 se intenta el scraper con fallback manual                                                                                                    |
| 2026-07-11 | Horizonte cuantizado a uint8 (90/255 deg) con la escala en tag del GeoTIFF              | Error <= ~0.18 deg, muy por debajo del medio pixel del barrido; mitad de disco que uint16; el fichero es autodescriptivo                                                                                                                                                      |
| 2026-07-11 | Barrido de produccion: dedupe de offsets + tiling con buffer ceil(max_d/res)            | Exacto tras el floor a 0 (prueba en docstring): bit-identico al oraculo en modo exact, memoria acotada por tile. El modo geometric (paso creciente) queda como knob para Fase 4, validado solo por cuantil                                                                    |

Pendientes de decidir:

- PMTiles estaticos vs tiles PNG dinamicos (Fase 7)

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
