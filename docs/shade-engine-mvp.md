# Shade Engine — Especificación MVP

Motor open source de cálculo de sombra urbana. Dado un punto y un instante (o rango), responde si está a la sombra, de qué tipo de sombra (vegetal / edificación) y durante cuánto tiempo lo estará. Primera ciudad: **Córdoba (España)**. Primer caso de uso: aparcamiento a la sombra. Casos de uso futuros sobre el mismo motor: rutas peatonales a la sombra, refugios climáticos.

---

## 1. Principios de diseño

- **Backend monolítico** en FastAPI. Sin microservicios. Si en algún punto se necesita mensajería/broker, se aplicará el **patrón outbox** sobre Postgres (tabla `outbox` + relay), nunca acoplamiento directo a un broker desde el dominio.
- **La ciudad es la unidad de despliegue/config**, no una fila más: añadir una ciudad = añadir un fichero de configuración + ejecutar el pipeline. Cero cambios de código.
- **Cómputo pesado offline, consulta ligera online.** El pipeline genera artefactos ráster una vez por ciudad; la API solo los lee.
- **Sin frontend en este repo.** La API se consumirá desde una web externa en Astro (perfil personal) que ya integra otros mapas. Implicaciones: CORS configurable, API pública documentada, tiles servibles directamente.
- Open source: licencia de código sugerida **MIT** o **Apache-2.0**. Datos derivados de PNOA/IGN y Catastro: uso comercial permitido **con atribución obligatoria** (CC BY 4.0 / licencia IGN) — incluir atribución en respuestas de API (campo `attribution`) y en README.

## 2. Concepto técnico central: mapa de horizonte

En lugar de precomputar rásteres de sombra por fecha×hora (explosión combinatoria), se precomputa **una sola vez por ciudad** un ráster multibanda de **horizonte**: para cada píxel, el ángulo de elevación que bloquea el skyline en N sectores de azimut (default **64 sectores** → 64 bandas).

**Observador a nivel de calle:** el horizonte de cada pixel se calcula con el observador en **DTM + 1.6 m** (cota del suelo mas altura de persona) y los obstaculos tomados del DSM. Calcularlo desde el propio DSM daria resultados erroneos en pixeles bajo copa (el observador quedaria encima del arbol) y sobre tejados. Caso especial: pixel bajo copa vegetal (el landcover indica vegetacion por encima) -> sombra vegetal siempre que el sol este sobre el horizonte astronomico, coherente con el supuesto de copa opaca.

Consulta en runtime (milisegundos, válida para cualquier instante):

1. Calcular azimut + elevación solar con `pvlib` para (lat, lon, datetime, timezone de la ciudad).
2. Interpolar el ángulo de horizonte del píxel en ese azimut.
3. `sombra = elevación_solar < horizonte(azimut)` (o sol bajo el horizonte astronómico → noche).
4. El rango "a la sombra hasta las HH:MM" se obtiene barriendo la trayectoria solar del día contra el mismo horizonte (paso de 5–10 min).

### Tipo de sombra

Se genera un segundo artefacto: **ráster de clasificación del obstáculo**. Para cada píxel y sector, qué clase de objeto proyecta el bloqueo dominante, derivado de la clasificación de puntos LiDAR PNOA:

- `building` (clase 6)
- `vegetation` (clases 3/4/5 — vegetación baja/media/alta)
- `ground/other`

Simplificación aceptable para MVP: en vez de clasificar por sector, un ráster de **cobertura** (¿este píxel está bajo copa vegetal? ¿el DSM aquí es edificio o árbol?) y clasificar la sombra por el objeto más cercano en la dirección del sol. Documentar la aproximación elegida.

### Supuestos documentados del MVP

- Copas vegetales tratadas como **opacas** (real: 10–30% de transmisión). Futuro: porosidad vía densidad de retornos.
- DSM = foto del momento del vuelo LiDAR. Caducifolios sobreestiman sombra en invierno. Aceptable: el caso de uso principal es verano.
- Edificios posteriores al vuelo no aparecen.

## 3. Pipeline de datos (CLI Python, offline)

Paquete `pipeline/` dentro del monorepo, ejecutable como CLI (`shade-engine build <city>`), pensado para correrse en local o en un runner. Pasos:

1. **Descarga** LiDAR PNOA (IGN, ficheros LAZ) para el bbox de la ciudad.
2. **DSM** a partir de primeros retornos **y DTM** a partir de puntos suelo (clase 2), con PDAL + rasterio. Resolución objetivo: **1 m/píxel** (configurable). El DTM da la cota del observador (calle); el DSM, los obstaculos.
3. **Ráster de clasificación** (edificio / vegetación / suelo) a partir de las clases LiDAR.
4. **Ráster de horizonte** (64 bandas, uint8 o uint16 cuantizado en grados) mediante algoritmo de barrido por sectores (numpy vectorizado). Radio maximo de busqueda `max_distance` configurable (500 m - 1 km): trunca angulos de horizonte muy bajos (sombras kilometricas al amanecer/atardecer, irrelevantes para el caso de uso) y acota memoria/tiempo. Tiling con buffer >= max_distance para no reventar memoria.
5. **Exportación como COG** (Cloud Optimized GeoTIFF): `dsm.tif`, `dtm.tif`, `horizon.tif`, `landcover.tif`.
6. **Tiles de visualización** opcionales (PMTiles) para pintar mapa de sombras a horas clave.
7. **Publicación** de artefactos en object storage (S3-compatible / disco local en dev) con versionado (`cities/cordoba/v1/...`).

Stack pipeline: Python 3.14, PDAL, rasterio, numpy, pvlib, click/typer.

> Nota: verificar al arrancar que las libs geoespaciales con extensiones nativas (PDAL bindings, rasterio, shapely) publican wheels para 3.14; si alguna cojea, fijar 3.13 **solo en el pipeline** y mantener 3.14 en la API (son procesos separados, no hay conflicto).

### Estimación de tamaño (validar en implementación)

Córdoba urbana ~ area configurable (empezar con bbox del casco urbano + barrios, no todo el término municipal). A 1 m/píxel y 64 bandas uint8, el horizonte de un área de 100 km² ≈ 6.4 GB sin comprimir; con COG+deflate y limitando al área urbana real debería quedar en cientos de MB. Si es excesivo: 2 m/píxel o 32 sectores como fallback.

## 4. Configuración por ciudad

Directorio `cities/` en el repo, un YAML por ciudad. Añadir ciudad = PR con un YAML. Ejemplo:

```yaml
# cities/cordoba.yaml
id: cordoba
name: Córdoba
country: ES
timezone: Europe/Madrid
crs: EPSG:25830 # UTM 30N ETRS89
bbox: [341000, 4192000, 349000, 4199000] # en CRS local
resolution_m: 1.0
horizon_sectors: 64
sources:
  lidar: pnoa # driver de descarga
  lidar_coverage: "PNOA-2ª cobertura" # informativo: año/campaña del vuelo
layers:
  parking: cities/cordoba/parking.geojson # opcional
  trees: cities/cordoba/trees.geojson # opcional (inventario arbolado)
attribution:
  - "LiDAR PNOA © Instituto Geográfico Nacional de España"
```

## 5. Capas complementarias por ciudad (datos vectoriales, editables a mano)

### 5.1 Zonas de aparcamiento (`parking.geojson`)

Digitalización manual (JOSM/uMap/QGIS) para el MVP; probablemente no existe dataset abierto limpio de la ORA de Córdoba. Schema mínimo por feature (LineString o Polygon):

```json
{
  "zone_type": "blue | green | free | loading | resident",
  "schedule": [
    { "days": "mo-fr", "from": "09:00", "to": "14:00" },
    { "days": "mo-fr", "from": "17:00", "to": "20:00" },
    { "days": "sa", "from": "09:00", "to": "14:00" }
  ],
  "max_minutes": 120,
  "tariff_eur_hour": 0.85,
  "notes": "…",
  "source": "ordenanza municipal AAAA / relevamiento propio",
  "last_verified": "2026-07-01"
}
```

Se importa a Postgres/PostGIS con un comando del CLI (`shade-engine import-layer cordoba parking`).

### 5.2 Inventario de arbolado (`trees.geojson`) — opcional, roadmap corto

Puntos con `species`, `common_name`, `deciduous: bool`, `height_m` si se conoce. Fuente: open data municipal si existe, o crowdsourcing/OSM. Uso: enriquecer la respuesta de tipo de sombra ("sombra de plátano de sombra") y, a futuro, factor estacional para caducifolios.

## 6. API (FastAPI, monolito)

### Stack

- Python 3.14, FastAPI, SQLAlchemy 2 + Alembic, **PostgreSQL 18 + PostGIS 3.6** (o la última estable en el momento de arrancar; verificar compatibilidad PostGIS↔Postgres antes de fijar imagen Docker). Guarda zonas de parking, arbolado, metadatos de ciudades y outbox si aplica.
- Lectura de COGs con rasterio (ventanas puntuales; sin cargar el ráster entero). Cache LRU de tiles/ventanas calientes.
- Los rásteres NO van a Postgres: viven en object storage/disco; Postgres guarda solo metadatos y vectores.

### Endpoints MVP

```
GET /v1/cities
    → lista de ciudades disponibles + bbox + atribución

GET /v1/shade?city=cordoba&lat=..&lon=..&at=2026-08-01T16:00:00
    → { in_shade: bool, shade_type: "building"|"vegetation"|null,
        sun: {azimuth, elevation}, attribution: [...] }

GET /v1/shade/timeline?city=cordoba&lat=..&lon=..&date=2026-08-01
    → intervalos de sombra/sol del día:
      [{from:"08:00", to:"11:20", in_shade:true, shade_type:"building"}, ...]
      + "shaded_until" para el instante actual si date=hoy

GET /v1/parking/nearby?city=cordoba&lat=..&lon=..&radius=300&at=...
    → tramos/zonas de aparcamiento cercanos, con su estado de sombra
      en el instante `at` y shaded_until (combina PostGIS + motor de sombra)

GET /v1/tiles/{city}/{layer}/{z}/{x}/{y}.png   (o servir PMTiles estático)
    → visualización del mapa de sombra a la hora solicitada (query ?at=)
```

### Transversales

- **CORS** configurable por env (necesario: la web Astro consume desde otro dominio).
- **Rate limiting** básico (slowapi o similar) — API pública.
- Versionado de API en path (`/v1`).
- Timezone: la API acepta ISO 8601; sin offset se interpreta en la TZ de la ciudad.
- OpenAPI/Swagger autogenerado como documentación pública.
- Campo `attribution` en respuestas (obligación de licencia IGN).
- Healthcheck `/healthz` + endpoint de metadatos de artefactos cargados.

### Outbox (solo si aparece la necesidad)

No hay caso de uso de mensajería en el MVP. Si aparece (p.ej. notificaciones "tu zona quedará al sol en 40 min"), patrón outbox: tabla `outbox(id, aggregate, event_type, payload jsonb, created_at, processed_at)` escrita en la misma transacción que el cambio de dominio + relay en background task/proceso aparte. No introducir broker hasta entonces.

## 7. Estructura de repositorio (monorepo)

```
shade-engine/
├── api/                  # FastAPI app (dominio, routers, servicios)
├── pipeline/             # CLI de generación de artefactos por ciudad
├── core/                 # compartido: modelo solar, lectura de horizonte, geo utils
├── cities/               # configs YAML + capas vectoriales por ciudad
├── tests/                # pytest; fixtures con mini-rásteres sintéticos
├── docker/               # Dockerfile api, docker-compose (api+postgis+minio dev)
├── docs/                 # arquitectura, cómo añadir una ciudad, formato de capas
└── README.md             # visión: shade engine para ciudades; roadmap
```

## 8. Testing y calidad

- **Tests del modelo solar** contra valores conocidos (pvlib ya validado, pero testear la integración y TZ).
- **Rásteres sintéticos** en tests: un "edificio" cubo de 20 m en un DSM plano → sombra esperada calculable a mano en varios instantes. Es la prueba de oro del motor.
- Test de timeline: continuidad de intervalos, amanecer/atardecer correctos.
- CI (GitHub Actions): lint (ruff), mypy, pytest. El pipeline pesado NO corre en CI; sí un smoke test con un LAZ mínimo de fixture.

## 9. Despliegue MVP

Infraestructura existente: VPS personal **"cartagena"**, con otros servicios ya corriendo detrás de **Caddy** como reverse proxy y **CloudFront** como CDN por delante. Los servicios se exponen como subdominios de `ajustino.dev` (patrón `loquesea.ajustino.dev`).

- La API se despliega como un contenedor más en cartagena, expuesta p.ej. en `shade.ajustino.dev` (o `sombra.ajustino.dev`) vía Caddy. No gestionar TLS en la app: lo hace Caddy.
- Docker Compose: `api` + `postgis` (+ `minio` solo en dev; en cartagena los COGs pueden vivir en un volumen local, no hace falta object storage para una ciudad).
- **La app debe respetar cabeceras de proxy** (`X-Forwarded-For/Proto`): configurar uvicorn con `--proxy-headers` y `--forwarded-allow-ips`.
- **Caché**: los endpoints de tiles y `/v1/shade` con `at` explícito son cacheables — emitir `Cache-Control` correctos para aprovechar CloudFront (los tiles de sombra para una hora dada son inmutables). Los endpoints con "ahora" implícito, no cachear o TTL corto.
- **CORS**: permitir `https://ajustino.dev` y `https://*.ajustino.dev` (configurable por env).
- Cuidar el consumo de RAM/CPU: es un VPS compartido con otros servicios; limitar workers de uvicorn y el tamaño del cache LRU de ventanas ráster por configuración.

### Decisión: sin GeoServer

Se descarta GeoServer para el MVP: es un servidor Java pesado orientado a publicar muchas capas WMS/WFS en contextos GIS corporativos, y duplicaría lo que ya hacemos con menos piezas (COG + rasterio para ráster puntual, PostGIS + FastAPI para vectores, PMTiles estáticos para visualización). En un VPS compartido su JVM sería el proceso más gordo de la máquina para aportar poco. Si en el futuro el servido de tiles crece: **TiTiler** (tiles dinámicos desde COG, hecho sobre FastAPI, mismo ecosistema) para ráster y **Martin** o **pg_tileserv** para vector tiles desde PostGIS. Mencionado en roadmap, no en MVP.

## 10. Objetivo didáctico — instrucciones para el agente de código

Este proyecto es también un vehículo de aprendizaje sobre geomática y cálculo solar. El agente que implemente el código debe **explicar los conceptos a medida que los usa**, no solo aplicarlos. Concretamente:

- **Antes de implementar cada pieza geoespacial, explicar el concepto** en la conversación y dejarlo escrito: qué es un CRS y por qué usamos EPSG:25830 (UTM) para calcular y EPSG:4326/3857 para servir; qué diferencia hay entre DSM, DTM y CHM; qué son primeros retornos y clases LiDAR; qué es un COG y por qué permite lecturas por ventana; qué son azimut, elevación solar, declinación y ecuación del tiempo; cómo funciona el algoritmo de horizonte por sectores; qué es un vector tile / PMTiles.
- Mantener un **`docs/learning/`** con una nota corta por concepto (formato: qué es, por qué lo usamos aquí, trampa típica, enlace de referencia). Añadir la nota en el mismo PR/commit donde el concepto aparece por primera vez.
- **Docstrings didácticos** en `core/`: las funciones de geometría solar y horizonte deben incluir la explicación matemática (fórmulas, unidades, convenciones de signo — p.ej. azimut 0°=Norte, horario) y no asumir que el lector conoce el dominio.
- Al elegir entre alternativas técnicas geo (interpolación de horizonte, resampling, estrategia de tiling), **exponer brevemente las opciones y el porqué de la elección**, no decidir en silencio.
- Trampas a explicar explícitamente cuando toquen: confusión lat/lon vs lon/lat entre librerías, distorsión de distancias en Web Mercator, timezone vs hora solar, y por qué nunca se calculan distancias en grados.

Estas instrucciones deben copiarse al `CLAUDE.md` del repo para que apliquen en todas las sesiones de trabajo.

## 11. Fuera de alcance del MVP (roadmap en README)

- Rutas peatonales a la sombra (A\* sobre grafo OSM con peso solar) — mismo motor, feature aparte.
- Refugios climáticos / índice de confort térmico.
- Porosidad de copas y factor estacional de caducifolios.
- Disponibilidad de plazas en tiempo real (sensores/crowdsourcing).
- Más ciudades (el diseño ya lo permite; no ejecutarlas aún).
- Frontend propio del proyecto (de momento consumo desde web Astro externa).

## 12. Orden de trabajo sugerido

1. Esqueleto del monorepo + docker-compose + CI.
2. `core/`: modelo solar (pvlib) + consulta de horizonte sobre ráster sintético + tests de oro.
3. `pipeline/`: DSM desde un LAZ de muestra de Córdoba → horizonte → COGs.
4. `api/`: `/v1/shade` y `/v1/shade/timeline` leyendo los COGs reales.
5. Ejecutar pipeline completo de Córdoba (área urbana) y validar sobre el terreno (fotos reales vs predicción — la mejor demo posible para el README).
6. Capa `parking.geojson` inicial (centro de Córdoba) + `/v1/parking/nearby`.
7. Despliegue en cartagena (Caddy + subdominio) + tiles/PMTiles + integración en la web Astro (ajustino.dev).
