# GeoJSON (RFC 7946)

**Que es.** Formato JSON para geometria vectorial: features con `geometry`
(Point, LineString, Polygon y sus variantes Multi\*) y `properties` libres.
Es EL formato de intercambio para capas pequenas editables a mano o por
script, legible en cualquier visor (geojson.io, QGIS, Leaflet, PostGIS).

**Por que lo usamos aqui.** Las capas complementarias por ciudad (parking,
arbolado) son vectores de decenas o cientos de features: viven como GeoJSON
versionado en git (`cities/<id>/parking.geojson`), al contrario que los
rasteres (gigas, fuera de git). Git ademas documenta gratis la procedencia y
cada correccion manual. En Fase 5 se importan a PostGIS con `import-layer`;
el fichero sigue siendo la fuente de verdad editable.

**La trampa tipica (otra vez lon/lat).** RFC 7946 fija DOS cosas que no son
opcionales: las coordenadas van siempre en WGS84 (EPSG:4326) -- un GeoJSON
moderno no declara CRS porque no puede llevar otro -- y el orden es
`[longitud, latitud]`, o sea x,y como pyproj con `always_xy=True`. Quien
escribe `[lat, lon]` (el orden hablado y el de Google Maps) produce puntos
en el oceano Indico. Corolario: nunca medir distancias sobre estos grados;
se reproyecta a metros (EPSG:25830) primero.

**MultiLineString como agrupador.** Una zona de aparcamiento son varios
tramos de calle con atributos comunes (plazas totales, horario). Un feature
MultiLineString por zona mantiene los atributos en su dueno real: con un
LineString por tramo habria que repetir `capacity` y cualquier agregacion
(sumar plazas) contaria doble.

**Referencia.** RFC 7946 (https://datatracker.ietf.org/doc/html/rfc7946);
spec del proyecto seccion 5.1 para el schema de la capa de parking.
