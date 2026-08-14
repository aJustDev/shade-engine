# Fuentes de datos abiertos de Cordoba

Registro de lo encontrado y verificado el 2026-08-14. Ninguna de estas capas
esta integrada todavia: este documento existe para no volver a buscarlas.
Cada cifra de aqui esta comprobada contra el servicio en vivo ese dia.

Contexto: el bbox de artefactos de Cordoba es
`[341000, 4192000, 349000, 4199000]` en EPSG:25830 (8x7 km, ver
`cities/cordoba.yaml`). Fuera de el no sabemos calcular sombra, asi que la
columna "dentro del bbox" es la que manda para decidir si una capa nos sirve.

## 1. Puntos de entrada

Hay tres, y el que se encuentra por Google es el peor de los tres.

| Fuente              | URL                                                           | Nota                                    |
| ------------------- | ------------------------------------------------------------- | --------------------------------------- |
| Portal CKAN         | `https://datosabiertos.cordoba.es`                            | 145 datasets, API v3 completa           |
| GeoServer (IDE)     | `https://ide.cordoba.es/geoserver/idecordoba/wfs`             | 105 capas, WFS 2.0, GeoJSON, EPSG:25830 |
| Buscador del portal | `https://www.cordoba.es/transparencia/datosabiertos/busqueda` | Carcasa JS, sale vacia. NO usar         |

**API CKAN** (verificada sin prefijo `/ckan`, aunque el campo `help` de las
respuestas apunta a `/ckan/api/3/...`):

    https://datosabiertos.cordoba.es/api/3/action/package_list
    https://datosabiertos.cordoba.es/api/3/action/package_show?id=<nombre>

**WFS**. Nativo en EPSG:25830, que es exactamente el CRS en el que calculamos:
no hay reproyeccion ni error de ida y vuelta. Conteo barato sin descargar
geometrias con `resultType=hits`, y filtro por nuestro bbox:

    https://ide.cordoba.es/geoserver/idecordoba/wfs
      ?service=WFS&version=2.0.0&request=GetFeature
      &typeNames=idecordoba:Arbolado
      &outputFormat=application/json
      &bbox=341000,4192000,349000,4199000,EPSG:25830

## 2. Capas WFS relevantes

De las 105 capas, la mayoria son ediciones anuales del concurso de Patios
(`patios_2015`, `sql_juderia_17`...). Las que nos interesan:

| Capa                                    | Total   | Dentro del bbox | Que es                        |
| --------------------------------------- | ------- | --------------- | ----------------------------- |
| `idecordoba:Arbolado`                   | 89.833  | 74.766          | Arbol individual, con especie |
| `idecordoba:palmeras`                   | 3.435   | 2.879           | Palmeras, capa aparte         |
| `idecordoba:construcciones`             | 137.911 | 87.509          | Huellas de edificacion        |
| `idecordoba:zonas_verdes`               | 2.315   | 1.673           | Poligonos de zona verde       |
| `idecordoba:manzana`                    | 20.730  | -               | Manzanas catastrales          |
| `idecordoba:ejes_red_viaria`            | 9.668   | -               | Ejes de calle                 |
| `idecordoba:sup_viales`                 | 6.529   | -               | Superficie de vial (poligono) |
| `idecordoba:toponimo`                   | 6.612   | -               | Toponimia                     |
| `idecordoba:colegios`                   | 70      | -               | Centros educativos            |
| `idecordoba:centros_de_mayores`         | 32      | -               | Centros de mayores            |
| `idecordoba:centros_servicios_sociales` | 7       | -               | Servicios sociales            |
| `idecordoba:bibliotecas`                | 6       | -               | Bibliotecas municipales       |
| `idecordoba:puntos_estrategicos`        | 4       | -               | Sin inspeccionar              |

### Arbolado: la capa que toca el motor

No es una capa de puntos de interes. Es la unica fuente encontrada que puede
cambiar como calculamos, por tres vias distintas:

1. **Contraste del CHM.** Detectamos copas desde LiDAR (ver
   `docs/learning/canopy-sieve.md`); esto es una verdad de campo municipal
   sobre donde hay arbol. Es la validacion de vegetacion que no tenemos.
2. **Caducifolio vs perenne.** Hoy asumimos copa opaca los doce meses (ver
   apunte 1 de `plan.md`) mientras la escalera de declinacion cubre todo el
   ano. El campo de especie permite distinguirlos. En una muestra de 2.000
   registros: naranjo amargo 467 (perenne), y melia + jacaranda + sofora
   379 (caducifolios). En enero esos ultimos no dan sombra y nosotros
   decimos que si.
3. **Alcorques vacios y tocones.** En la misma muestra, 53 "Marra o alcorque
   vacio" y 69 "Tocon": en torno al 6%. Es la capa "donde plantar" de la
   Fase 11, sin trabajo extra.

Aviso sobre esos porcentajes: son los primeros 2.000 registros que devuelve el
WFS, en orden de insercion y probablemente agrupados geograficamente. Sirven
para saber que el campo de especie es utilizable, NO para caracterizar el
arbolado de Cordoba. Para eso hay que bajar la capa entera y contar.

Campos utiles del feature: `res` (especie, formato
`"Quercus ilex - Encina (QIL)"`), `espi` (codigo de especie, `FJ.QIL`), `cod`,
`ide`, `fecdat` (fecha del dato; los ejemplos vistos son de 2014). Hay muchas
columnas nulas (`alcada`, `cubpda`, `tpro`): la altura de copa NO viene
rellena, asi que el CHM del LiDAR sigue siendo nuestra unica fuente de altura.

## 3. Datasets CKAN con descarga directa

URLs verificadas con HTTP 200 salvo donde se indica.

**Fuentes de agua para beber** (674 puntos, 547 dentro del bbox), WGS84:

    https://datosabiertos.cordoba.es/ckan/dataset/b9b9e2bd-3b1f-46b4-84b1-386e88fe5266/resource/48ebe3e7-923c-40b4-aba8-140bb8c13579/download/fuentes-de-agua.geojson
    https://datosabiertos.cordoba.es/ckan/dataset/b9b9e2bd-3b1f-46b4-84b1-386e88fe5266/resource/aa82ea7d-3d38-4e8b-ad8c-9d1edf10aa6d/download/fuentes.csv

Propiedades: `name`, `description`, `Codigo de fuente`, `Descripcion`,
`Latitud`, `Longitud`. Dos trampas: el GeoJSON viene de una conversion de KML
y las coordenadas son `[lon, lat, 0]` con una Z falsa; y la clave del codigo
de fuente lleva un BOM pegado delante, asi que un acceso literal por nombre
falla. Ojo tambien con que la capa mezcla fuentes ornamentales monumentales
con fuentes de beber (ejemplo: "Fuente de los Jardines de la Victoria").

**Zonas verdes por distritos** (2.315 poligonos, 1.673 dentro del bbox):

    https://datosabiertos.cordoba.es/ckan/dataset/c771b677-cf63-47b9-8390-e93ac77771c6/resource/cb3e5952-8027-4f46-9547-387ce3026bf5/download/zonas_verdes.json

**Bibliotecas municipales**:

    https://datosabiertos.cordoba.es/ckan/dataset/7d85cd78-c665-4eb6-a8b6-8b6f3285ce17/resource/429fd04d-8d02-4bf9-96cc-cd4452785f95/download/red-municipal-de-bibliotecas-.geojson

**Centros deportivos municipales**:

    https://datosabiertos.cordoba.es/ckan/dataset/8c732141-6689-4fb5-b61e-76f9ca10ea64/resource/eabfb7bd-a91f-4e42-880b-c4196f365943/download/centros-deportivos.geojson

**Colegios** (KML y CSV):

    https://datosabiertos.cordoba.es/ckan/dataset/f999b178-9176-4bad-b02b-65026b4e9ff7/resource/82a277a8-c611-40ee-a144-2adec1d46ca2/download/colegios.kml

**Centros de mayores** (solo CSV):

    https://datosabiertos.cordoba.es/ckan/dataset/2568086d-e6cc-44bf-a65e-fc2ec987a474/resource/a3fcdb84-a85d-4cce-b21f-278dbba1d8ac/download/ayuncordoba_otros_servicios_servicios_sociales_centrodemayores.csv

**Piscinas municipales**: NO hay descarga estatica. Solo salen por la API de
Ciudades Abiertas, que el 2026-08-14 devolvia 502:

    https://datosabiertos.cordoba.es/api_ciudadesabiertas/instalacion-deportiva/instalacion-deportiva.json?id=IMP*

Los centros deportivos usan la misma API con `id=IMD*` pero ademas tienen un
GeoJSON estatico en CKAN, que es el que conviene usar. Si hicieran falta las
piscinas y la API siguiera caida, probablemente esten dentro de la capa de
centros deportivos.

## 4. Refugios climaticos (PDF, verano 2026)

    https://www.cordoba.es/sites/default/files/PDF/Servicios/servicios-sociales/2026/Horaro_Verano_refugios.pdf

Es una infografia hecha en Canva: **no hay direcciones ni coordenadas**, hay
que geocodificar los ocho a mano. El texto se extrae con `pdftotext -layout`.
Se transcribe aqui entero porque la fuente es fragil (cada verano sera otro
PDF en otra URL) y porque son solo ocho filas.

| #   | Sitio                             | Alerta naranja (40 C o mas)             | Alerta roja (44 C o mas) |
| --- | --------------------------------- | --------------------------------------- | ------------------------ |
| 01  | Centro Civico Fuensanta           | L-V 8.30-15.00                          | L-D 9.00-21.00           |
| 02  | Centro Civico Norte               | L-V 8.30-15.00                          | L-D 9.00-21.00           |
| 03  | Centro Civico Poniente Sur        | L-V 8.30-15.00                          | L-D 9.00-21.00           |
| 04  | CPAPM Huerta de la Reina          | L-V 10.30-13.00 y 17.00-20.00           | L-D 9.00-21.00           |
| 05  | Jardin Botanico                   | M-D 9.00-14.00                          | L-D 9.00-21.00           |
| 06  | Biblioteca Central                | L-V 10.00-21.00                         | L-D 9.00-21.00           |
| 07  | Edificio La Normal (sala lectura) | L-V 7.30-15.30                          | L-D 9.00-21.00           |
| 08  | CRV                               | L-S 9.00-18.45; D y festivos 9.00-14.15 | L-D 9.00-21.00           |

La alerta roja anade el matiz "44 C o superior / persistencia durante varios
dias". La sigla CRV no se desarrolla en el documento (probablemente Centro de
Recepcion de Visitantes; sin confirmar).

Consecuencia de diseno si algun dia se integran: el horario depende del nivel
de alerta vigente, que es un dato externo (AEMET) y cambiante. O se modela el
nivel como entrada, o se muestran los dos horarios y se dice cual aplica
segun el aviso del dia. No existe "el horario del refugio" a secas.

## 5. Avisos antes de integrar nada

**Licencia sin especificar.** Ninguno de los datasets inspeccionados declara
licencia: el campo sale como "License not specified" / vacio. No se localizo
el aviso legal del portal (`/ckan/pages/aviso-legal` da 404, y la portada no
lo menciona). Hay que aclararlo antes de redistribuir; la atribucion visible
de la fuente es el minimo en cualquier caso.

**Antiguedad.** Los `metadata_modified` de los datasets utiles estan entre
2024-01 y 2024-05. El arbolado trae fechas de dato de 2014. El PDF de
refugios es de verano de 2026.

**El bbox municipal es mucho mayor que el nuestro.** Las fuentes se extienden
de lon -4.93 a -4.63 (unos 26 km); 127 de las 674 caen fuera del bbox de
artefactos. Cualquier capa que se importe hay que recortarla, o la API
prometera sombra donde no la sabe calcular.

**Dos APIs distintas conviven** en el mismo dominio: CKAN (`/api/3/action/...`,
estable) y Ciudades Abiertas (`/api_ciudadesabiertas/...`, caida el dia de la
consulta). Preferir siempre el recurso estatico de CKAN o el WFS.

## 6. Donde encajaria

Los POI son datos, y los datos van detras de la API. El patron ya existe y es
el del parking: bloque `layers:` en `cities/<id>.yaml`, `shade-engine
import-layer <city> <capa>`, tabla en PostGIS y endpoint que devuelve el
estado de sombra a una hora (ver Fase 5 en `plan.md` y
`docs/adding-a-city.md`). Un `/v1/poi/nearby` reutilizaria casi todo.

La funcion que justifica el conjunto: **refugio climatico abierto ahora, mas
ruta a la sombra hasta el**. Es la "conexion con refugios climaticos" de la
Fase 11 adelantada, apoyada en el grafo peatonal que ya esta en produccion.

## 7. Pendiente de comprobar

- Licencia real de reutilizacion (preguntar al ayuntamiento o buscar el
  aviso legal del portal por otra via).
- Composicion de especies del arbolado sobre la capa completa, no sobre los
  primeros 2.000.
- Si `idecordoba:construcciones` (87.509 dentro del bbox) aporta algo frente
  a nuestro tileset de edificios derivado de LiDAR, que ya tiene altura real.
- Que hay en `idecordoba:puntos_estrategicos` (solo 4 elementos).
- Si las piscinas municipales estan contenidas en la capa de centros
  deportivos, para no depender de la API caida.
