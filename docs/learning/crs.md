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

- **Calculamos en EPSG:25830**: el bbox de cada ciudad, los rasteres (DSM,
  DTM, horizonte) y toda la matematica de distancias van en metros reales.
  Pixel de 1 m = 1 m de suelo, y el barrido de horizonte necesita distancias
  euclidianas correctas.
- **Servimos en EPSG:4326/3857**: la API recibe lat/lon (lo que tiene el
  cliente) y los tiles de visualizacion van en Web Mercator (lo que espera la
  libreria de mapas). La conversion ocurre solo en la frontera de la API.

## Trampa tipica

El orden de los ejes. "lat/lon" es el orden coloquial (y el de EPSG:4326
formal), pero casi todas las librerias geo (shapely, rasterio, GeoJSON,
PostGIS) trabajan en orden **(x, y) = (lon, lat)**. Mezclarlos pone el punto
en el oceano Indico. Convencion del proyecto: internamente siempre (x, y) en
CRS proyectado; lat/lon solo como parametros de entrada de la API, nombrados
explicitamente `lat` y `lon`.

Segunda trampa: nunca calcular distancias en grados. Un grado de longitud en
Cordoba mide ~89 km, pero uno de latitud ~111 km, y la diferencia cambia con
la latitud. Distancias, buffers y resoluciones: siempre en un CRS proyectado
en metros.

## Referencia

- https://epsg.io/25830
- https://proj.org/en/stable/usage/projections.html
