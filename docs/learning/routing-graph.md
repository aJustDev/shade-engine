# Grafo peatonal (OSM) y fracciones de sol por arista

## Que es

Un modelo del viario como grafo: **nodos** = cruces, **aristas** = tramos
andables entre dos cruces, cada una con su polilinea real y su longitud en
metros. OSM guarda el viario como _ways_ (listas de vertices con tags:
`highway=footway`, `residential`, `steps`...); osmnx descarga los ways de
un bbox via Overpass y hace la conversion way -> aristas-entre-cruces
(`network_type="walk"`). El resultado es un `MultiDiGraph`: **multi**
porque entre el mismo par de nodos puede haber varias aristas (la diagonal
de una plaza y su soportal), **di** porque cada tramo aparece duplicado,
una vez por sentido, con la geometria espejada.

Nuestro artefacto (`data/cities/<id>/v1/graph/`) congela ese grafo como
arrays numpy planos:

- `graph.npz`: coordenadas de nodos (CRS proyectado de la ciudad, metros),
  extremos/longitud de cada arista, y todas las polilineas concatenadas en
  un array "ragged" (`geom_offsets[i]:geom_offsets[i+1]` corta la arista i).
- `fractions.npz`: matriz uint8 (aristas x 83): fraccion de la arista AL SOL
  en cada instante de la escalera de declinacion, escalada a 0-255.
- `graph.json`: procedencia + el mapa peldano/hora -> columna de la matriz.

## Por que lo usamos aqui

La Fase 8 pregunta "de A a B con la maxima sombra": eso es un camino minimo
con coste `longitud * (1 + alfa * fraccion_sol)`. Las decisiones clave:

- **Artefacto del build, no consulta en runtime**: osmnx (y su cola de
  geopandas) solo vive en `pipeline/`; la API enruta con numpy puro sobre
  los arrays. Sin Overpass en produccion, sin red, sin deps nuevas.
- **Dedup a no-dirigido**: andando no hay sentido unico, asi que los
  gemelos reciprocos se colapsan (misma polilinea orientada del nodo menor
  al mayor => misma arista). Las paralelas VERDADERAS (polilineas
  distintas) sobreviven: son opciones reales que el router debe ver.
- **Fracciones precalculadas sobre la escalera**: los mismos 83 instantes
  que los tiles (7 fechas canonicas x horas de luz). Por instante, UNA
  pasada vectorizada del raster de estado de toda la ciudad + indexado de
  las muestras (cada 5 m por longitud de arco). Ruta y overlay del visor
  responden la misma fisica en el mismo instante, y el A\* en runtime no
  toca ni un COG.
- **Muestra sin dato = sol**: fuera del raster o `STATE_OUTSIDE` cuenta
  como sol. Inventar sombra donde no hay dato es fabricar justo lo que el
  buscador de sombra quiere oir.

## Trampa tipica

**La licencia**. La geometria del viario ES OpenStreetMap: el artefacto es
base de datos derivada bajo ODbL y toda respuesta construida sobre el debe
atribuir "(c) OpenStreetMap contributors" (el campo `attribution` viaja en
graph.json y en la respuesta del endpoint). No mezclar esa geometria con
capas propias sin tener claro el share-alike (ya nos lo encontramos con el
parking en Fase 5, alli lo esquivamos usando la fuente municipal).

Otra de dominio: las longitudes se **recalculan** de la polilinea
proyectada (nunca se confia el atributo `length` de la fuente): asi son,
por construccion, el mismo arco que camina el muestreador de fracciones, y
nunca se mide longitud en grados (ver crs.md).

## Referencia

- osmnx: https://osmnx.readthedocs.io/
- Modelo de datos OSM (ways/nodes): https://wiki.openstreetmap.org/wiki/Elements
- ODbL y atribucion: https://www.openstreetmap.org/copyright
