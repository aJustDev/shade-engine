# PNOA LiDAR y el centro de descargas del CNIG

## Que es

El PNOA (Plan Nacional de Ortofotografia Aerea) incluye vuelos LiDAR que
cubren Espana en campanas sucesivas, distribuidos gratis por el CNIG:

| Cobertura | codSerie | Anos      | Densidad  | Tile   | En Cordoba |
| --------- | -------- | --------- | --------- | ------ | ---------- |
| 1a        | `LIDAR`  | 2008-2015 | 0.5 pt/m2 | 2x2 km | 2014       |
| 2a        | `LIDA2`  | 2015-2021 | 1-2 pt/m2 | 2x2 km | 2020       |
| 3a        | `LIDA3`  | 2022-2025 | ~5 pt/m2  | 1x1 km | 2024       |

La 3a cobertura llega con clasificacion automatica NPC01 (F-score >= 0.9;
NPC02/03 son niveles editados/extendidos que van publicandose despues). El
nombre del fichero codifica la esquina NW del tile en km UTM del huso local:
`PNOA-2024-AND-343-4195-H30-NPC01.laz` cubre easting 343-344 km y northing
4194-4195 km en EPSG:25830. Eso hace el mapeo bbox -> tiles deterministico.

## Por que lo usamos aqui

Elegimos la 3a cobertura para Cordoba: 5 pt/m2 definen bordes de tejado y
copas mucho mejor que 1.5, y un vuelo de 2024 refleja la ciudad que vamos a
fotografiar en la validacion de campo (2026) mejor que uno de 2020.

El centro de descargas no publica API. Su visor llama endpoints internos
que funcionan sin sesion ni captcha (verificado 2026-07-11): un GET a
`archivosSerie` busca por poligono GeoJSON (EPSG:4326) y devuelve HTML con
el nombre y un id `sec` por fichero; un POST a `descargaDir` con ese `sec`
sirve el LAZ. El `sec` no se deduce del nombre: hay que consultar el
catalogo. El driver (`shade_pipeline.cnig`) envuelve esos endpoints tras la
interfaz `LidarSource` y esta disenado para romperse ruidosamente: al no
haber contrato, cualquier cambio del HTML o descarga invalida falla con
instrucciones de descarga manual (`--lidar-dir`). Cada tile validado en el
cache local sobrevive a cortes (hay un limite documentado de ~20 descargas
por sesion anonima): re-ejecutar el comando reanuda. Un throttle de 1 s
entre descargas es cortesia con un servicio publico gratuito.

## Licencia

Los datos IGN/CNIG son CC-BY 4.0 (Orden FOM/2807/2015). Para obra derivada
(nuestros rasteres de sombra) la formula abreviada valida es:

    Obra derivada de PNOA-cob3 2022-2025 CC-BY 4.0 scne.es

La atribucion viaja en `metadata.json` y en el campo `attribution` de la
API; es obligacion de licencia, no decoracion.

## Trampa tipica

- El catalogo lista nombres con guiones pero el `Content-Disposition` de la
  descarga usa underscores; canonizamos al nombre del catalogo.
- `descargaDir` con GET devuelve 403: tiene que ser POST con form.
- La respuesta de descarga es chunked sin `Content-Length`: no se puede
  validar por tamano esperado, se valida por magic bytes `LASF`.
- Los endpoints son internos: cualquier actualizacion del centro puede
  romperlos sin aviso. El fallback manual no es un extra, es parte del
  diseno.

## Referencia

- Centro de descargas: https://centrodedescargas.cnig.es
- Especificaciones PNOA LiDAR: https://pnoa.ign.es/pnoa-lidar/especificaciones-tecnicas
- Licencia IGN: https://www.ign.es/resources/licencia/Condiciones_licenciaUso_IGN.pdf
