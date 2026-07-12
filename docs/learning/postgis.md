# PostGIS: geometry vs geography, GiST y EWKT

**Que es.** PostGIS es la extension espacial de PostgreSQL: anade tipos de
columna para geometria vectorial, funciones espaciales (`ST_DWithin`,
`ST_Distance`, `ST_AsGeoJSON`...) e indices espaciales. Convierte la base de
datos en el sitio natural para preguntas como "que zonas de aparcamiento hay
a 300 m de este punto".

**geometry vs geography.** PostGIS tiene DOS familias de tipos y elegir mal
es la fuente clasica de bugs:

- `geometry` calcula en el plano del SRID, en SUS unidades. Con datos
  lon/lat (SRID 4326) eso son GRADOS: `ST_DWithin(geom, punto, 300)`
  filtraria a 300 grados (el planeta entero), y con 0.003 "parece" que
  funciona hasta que la latitud cambia. Es la trampa "nunca distancias en
  grados" de crs.md, ahora en SQL.
- `geography` calcula sobre el elipsoide WGS84 y acepta METROS. Es mas
  lenta y cubre menos funciones, pero la consulta nearby necesita
  exactamente lo que ofrece.

Aqui usamos `geography(MultiLineString, 4326)` ademas por multi-ciudad: una
columna `geometry` en el CRS local (EPSG:25830) tambien mediria metros, pero
clavaria el huso UTM de Cordoba en una tabla que debe servir cualquier
ciudad. El motor de sombra sigue calculando en el CRS local de cada ciudad;
esta columna solo responde "que hay cerca".

**GiST.** Un B-tree ordena escalares y no puede indexar extents 2D. GiST
(Generalized Search Tree) indexa bounding boxes: `ST_DWithin` primero filtra
por cajas via indice (barato) y despues refina con el predicado exacto solo
los candidatos. Sin GiST, cada consulta espacial es un scan completo. Con
geoalchemy2 lo declaramos explicito (`spatial_index=False` + `Index(...,
postgresql_using="gist")`) porque el indice implicito duplica DDL bajo
Alembic.

**EWKT.** `SRID=4326;MULTILINESTRING((lon lat, ...), ...)` es WKT extendido
con el SRID delante: el formato texto que `geography` ingiere nativamente
(geoalchemy2 envuelve los binds en `ST_GeogFromText`). Al importar lo
construimos con un f-string desde las coordenadas GeoJSON; al leer,
`ST_AsGeoJSON` devuelve GeoJSON directamente. Ojo: EWKT tambien es lon-lat.

**La trampa tipica.** Ademas de los grados de arriba: pasar un string
GeoJSON como valor de una columna `geography` via ORM. geoalchemy2 lo
envuelve en `ST_GeogFromText('{"type": ...}')` y revienta en runtime; la
geometria entra como EWKT (o WKB), nunca como GeoJSON crudo.

**Referencia.** https://postgis.net/docs/using_postgis_dbmanagement.html
(seccion geography); https://postgis.net/workshops/postgis-intro/indexing.html
(GiST); shade_core/db.py para el modelo y la migracion 0001 para el DDL.
