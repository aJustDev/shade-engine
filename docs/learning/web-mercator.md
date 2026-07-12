# Web Mercator (EPSG:3857) y su distorsion

## Que es

La proyeccion de facto de toda la web de mapas (Google, OSM, MapLibre).
Es un Mercator esferico que proyecta el mundo (hasta ~85.05 grados de
latitud) sobre un **cuadrado**, lo que hace trivial la rejilla de tiles
`2^z x 2^z`. Es conforme: los angulos y las formas locales se conservan,
por eso los mapas "se ven bien" a cualquier zoom.

El precio es la escala: Mercator estira las distancias por un factor
`1/cos(lat)`. En el ecuador 1 unidad de EPSG:3857 = 1 metro; en Cordoba
(37.9 N) el factor es ~1.27, o sea que un "metro" de Web Mercator mide
~0.79 m sobre el terreno. En Groenlandia el factor pasa de 3.

## Por que lo usamos aqui

Solo para **pintar**. Los tiles de visualizacion (Fase 7) tienen que estar
en Web Mercator porque es lo que el cliente de mapas espera; el pipeline
reproyecta el raster de estados de EPSG:25830 a EPSG:3857 con resampling
nearest justo antes de cortar tiles, y nada mas.

La eleccion del zoom maximo sale de esta cuenta: la resolucion de un tile
es `156543 / 2^z` metros/pixel _en el ecuador_; multiplicada por
`cos(37.9)` da los metros reales en Cordoba. En z17 son 0.94 m/px, justo
nuestra resolucion nativa de 1 m/px - generar z18 seria upsampling (el
cliente ya sobreamplia el ultimo zoom por su cuenta).

## Trampa tipica

Medir en 3857. Un buffer de "500 m" hecho en Web Mercator sobre Cordoba
mide ~394 m reales; en Estocolmo, ~250 m. Todo calculo de distancias,
areas o buffers del motor ocurre en el CRS local en metros verdaderos
(EPSG:25830, ver crs.md), y las distancias en grados directamente no
existen en este repo. Regla: 4326 para hablar con el usuario, 25830 para
calcular, 3857 solo para pintar.

Trampa secundaria: 3857 usa la esfera (no el elipsoide WGS84) - a veces
llamado "pseudo-Mercator". Mezclarlo con un Mercator elipsoidal (EPSG:3395)
desplaza todo decenas de km en latitud.

## Referencia

- https://en.wikipedia.org/wiki/Web_Mercator_projection
- https://wiki.openstreetmap.org/wiki/Zoom_levels (tabla m/px por zoom)
