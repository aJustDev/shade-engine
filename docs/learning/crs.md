# CRS (sistema de referencia de coordenadas)

## Que es

Un CRS define como se traducen numeros a posiciones sobre la Tierra: el
elipsoide de referencia (datum) y, si es proyectado, la proyeccion que aplana
la superficie curva en un plano. Se identifican con codigos EPSG:

- **EPSG:4326 (WGS84)**: geografico, coordenadas en grados (lat/lon). Es lo
  que dan un GPS o una API web.
- **EPSG:25830 (ETRS89 / UTM zona 30N)**: proyectado, coordenadas en metros.
  Cubre el huso UTM donde cae Cordoba (y la mayor parte de la peninsula).
- **EPSG:3857 (Web Mercator)**: proyectado, el estandar de facto de los mapas
  web (tiles). Pseudo-metros: la escala se distorsiona con la latitud.

## Por que lo usamos aqui

No existe la proyeccion buena para todo: cualquier plano distorsiona algo
(distancias, angulos o areas). Se elige la buena para cada tarea:

- **Calculamos en EPSG:25830**: el bbox de cada ciudad, los rasteres (DSM,
  DTM, horizonte) y toda la matematica de distancias van en metros reales.
  Pixel de 1 m = 1 m de suelo, y el barrido de horizonte necesita distancias
  euclidianas correctas (`sombra = altura / tan(elevacion)` exige metros).
  - **Por que UTM**: trocea el mundo en 60 husos de 6 grados y proyecta cada
    uno centrado en su meridiano. Dentro del huso el error de escala maximo
    es ~0.04%: una sombra de 100 m se equivoca en centimetros. No hay
    proyeccion global sin distorsion, pero si proyecciones locales casi
    perfectas; por eso el CRS va en el YAML de cada ciudad (Cordoba huso 30;
    Vigo usaria 25829).
  - **Por que ETRS89 y no WGS84**: es el datum oficial europeo, anclado a la
    placa euroasiatica (WGS84 deriva ~2.5 cm/ano con el continente). Y el
    LiDAR PNOA ya viene en ETRS89 UTM: calcular en el CRS del dato fuente =
    cero reproyecciones del raster = cero perdida por resampling.
- **Servimos en EPSG:4326**: es el idioma de intercambio universal (GPS,
  geocoders; GeoJSON lo exige por RFC 7946). La conversion a 25830 ocurre una
  sola vez, en la frontera de la API; el interior nunca ve un grado.
- **Tiles en EPSG:3857 (Web Mercator)**: en la latitud de Cordoba infla las
  distancias ~27% (factor 1/cos(lat)), pero para _mirar_ es ideal: conforme
  (las calles conservan angulos, norte siempre arriba) y proyecta el mundo en
  un cuadrado que se subdivide en quadtree, de ahi el esquema de tiles z/x/y
  que esperan MapLibre/Leaflet. Regla: en 3857 se pinta, nunca se mide.

Resumen del flujo: LiDAR llega en 25830 -> pipeline y motor calculan en 25830
-> la API traduce lat/lon en la frontera -> los tiles se reproyectan a 3857
solo para el navegador. Como guardar dinero en centimos enteros y formatear
en euros solo al mostrarlo.

## La transformacion en runtime (Fase 3)

La API convierte con pyproj, construyendo el transformer UNA vez por ciudad
(compila el pipeline de PROJ; hacerlo por peticion seria tirar milisegundos):

```python
to_projected = Transformer.from_crs("EPSG:4326", "EPSG:25830", always_xy=True)
x, y = to_projected.transform(lon, lat)  # (x, y) = (lon, lat), SIEMPRE
```

`always_xy=True` no es opcional: sin el, pyproj respeta el orden de ejes
OFICIAL de cada CRS, y el de EPSG:4326 es (lat, lon). Con el flag, ambos
extremos quedan en orden (x, y) y la llamada es `transform(lon, lat)`.
Detalles que muerden:

- Un punto fuera del dominio de la proyeccion no lanza excepcion: devuelve
  `inf`. El check de pertenencia al bbox lo absorbe de gratis porque toda
  comparacion con inf/nan es False.
- Para reproyectar un BBOX se usa `transform_bounds`, no las dos esquinas:
  un lado recto en UTM es una curva en lat/lon, y transformar solo esquinas
  puede recortar el area real. `transform_bounds` densifica los bordes.
- El transformer es thread-safe desde pyproj 3.1 (uno por ciudad, compartido
  entre peticiones del threadpool).

## Trampa tipica

El orden de los ejes. "lat/lon" es el orden coloquial (y el de EPSG:4326
formal), pero casi todas las librerias geo (shapely, rasterio, GeoJSON,
PostGIS) trabajan en orden **(x, y) = (lon, lat)**. Mezclarlos pone el punto
en el oceano Indico (y con pyproj sin `always_xy` no falla: devuelve
coordenadas absurdas en silencio). Convencion del proyecto: internamente
siempre (x, y) en CRS proyectado; lat/lon solo como parametros de entrada de
la API, nombrados explicitamente `lat` y `lon`.

Segunda trampa: nunca calcular distancias en grados. Un grado de longitud en
Cordoba mide ~89 km, pero uno de latitud ~111 km, y la diferencia cambia con
la latitud. Distancias, buffers y resoluciones: siempre en un CRS proyectado
en metros.

## Referencia

- https://epsg.io/25830
- https://proj.org/en/stable/usage/projections.html
