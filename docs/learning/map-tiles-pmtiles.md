# Tiles de mapa (slippy map) y PMTiles

## Que es

Un mapa web no descarga "el mapa": descarga teselas cuadradas de 256 px
direccionadas por `z/x/y` (slippy map). En el zoom `z` el mundo entero, en
proyeccion Web Mercator, se parte en una rejilla de `2^z x 2^z` tiles;
`x` crece hacia el este e `y` hacia el **sur** (origen arriba-izquierda,
al reves que la latitud). Cada nivel duplica la resolucion del anterior:
es una piramide, la misma idea que los overviews de un COG.

Hay dos familias:

- **Tiles raster**: cada tile es una imagen ya pintada (PNG/JPEG/WebP).
- **Tiles vectoriales** (MVT): cada tile lleva geometrias + atributos y el
  cliente los estiliza (colores, tipografia, idioma) al vuelo.

PMTiles es un **contenedor**: la piramide completa (raster o vectorial) en
un unico fichero estatico, con un directorio al principio que mapea cada
tile a su offset. Los tiles se ordenan por curva de Hilbert, que preserva
localidad espacial: tiles vecinos en el mapa quedan vecinos en el fichero.

## Por que lo usamos aqui

Es el truco del COG aplicado a piramides web (mismo patron: indice
delante + lecturas parciales). El navegador pide el directorio con una
peticion HTTP `Range`, localiza el tile y pide solo sus bytes: **cero
servidor de tiles**, solo Caddy sirviendo un fichero. En un VPS compartido
eso decide la eleccion frente a tiles PNG dinamicos (TiTiler y compania
quedan como roadmap si el servido crece; registro de decisiones, Fase 7).

En shade-engine conviven los dos mundos:

- El **overlay de sombra** es raster (el dato es un campo por pixel:
  sol/sombra-edificio/sombra-vegetacion) y ademas inmutable para un
  instante fijo: un PMTiles por instante clave, cacheable para siempre.
  Se pintan en paleta PNG con alfa (sol = transparente).
- El **basemap** es vectorial (extract de OSM via Protomaps): asi el
  cliente lo tine con la estetica del sitio sin regenerar nada.

Diferencia de roles COG vs PMTiles: el COG es el artefacto _analitico_
(georreferenciado en el CRS local, consultable por ventana); el PMTiles es
el artefacto de _visualizacion_ (ya proyectado a Web Mercator, ya pintado
o estilizable, listo para el navegador).

## Trampa tipica

Tres del writer, aprendidas aqui: (1) un archivo PMTiles no puede quedar
vacio (el finalize revienta sin entries) - por eso el zoom minimo se
escribe siempre aunque el tile sea transparente; (2) los tiles deben
escribirse en orden ascendente de tileid o el archivo queda sin clusterizar
(peor localidad); (3) `tile_compression` describe la compresion _interna_
de cada tile: para PNG debe ser NONE - un PNG ya va comprimido y marcarlo
GZIP hace que los clientes descompriman bytes que no lo estan.

Y una del servido: `fetch()` con cabecera `Range` **no** es una peticion
CORS "simple", dispara preflight OPTIONS. Sin handler de OPTIONS el mapa
funciona en local (same-origin) y falla en produccion (cross-origin).

## Referencia

- https://github.com/protomaps/PMTiles (spec v3)
- https://docs.protomaps.com/
- https://wiki.openstreetmap.org/wiki/Slippy_map_tilenames
