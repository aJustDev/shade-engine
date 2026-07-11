# COG (Cloud Optimized GeoTIFF)

## Que es

Un GeoTIFF normal cuyo layout interno cumple un contrato:

- Los pixeles se guardan en **tiles internos** (bloques de p.ej. 512x512)
  comprimidos de forma independiente, no en filas continuas.
- Incluye **overviews**: una piramide de versiones reducidas (1/2, 1/4...)
  para visualizar sin leer la resolucion completa.
- Los indices de donde cae cada tile van al **principio del fichero**.

No es un formato nuevo: cualquier lector de GeoTIFF lo abre. La diferencia
es que un lector listo puede pedir solo los bytes que necesita.

## Por que lo usamos aqui

La consulta de sombra necesita 1 pixel de un raster de 64 bandas que puede
ocupar GB. Con un COG eso cuesta descomprimir un unico tile (KB): la API
(Fase 3) hara lecturas por ventana con rasterio sin cargar nada entero. Y
como los offsets de tile son conocidos de antemano, la misma lectura
funciona contra un fichero remoto con peticiones HTTP `Range`: los
artefactos publicados se pueden consultar sin descargarlos.

Detalles de nuestros artefactos:

- Compresion `deflate`; el raster de clases (valores repetidos a lo grande)
  comprime casi gratis.
- Overviews con resampling `nearest`: nuestras bandas son categoricas
  (clases) o cuantizadas (angulos uint8); promediar inventaria valores que
  no existen (entre "edificio"=2 y "suelo"=0 no hay un 1 = "vegetacion").
- El driver COG de GDAL es CreateCopy: no permite escritura incremental.
  Patron: GTiff temporal tileado -> copia con el driver COG. GDAL marca el
  resultado con el tag `LAYOUT=COG`, que es lo que asertan los tests.

## Lectura por ventana en la practica (Fase 3)

`rasterio.windows.Window(col, row, ancho, alto)` + `src.read(window=...)`
lee solo los tiles internos que la ventana toca. Matiz importante: el coste
de una lectura fria de 1 pixel NO es 1 pixel, es descomprimir el tile
completo de 512x512 que lo contiene, por cada banda tocada (64 en
`horizon.tif`). Por eso el `SceneReader` de core no cachea pixeles sino
**bloques alineados** de 64x64 (64 divide a 512: un bloque alineado nunca
cruza dos tiles), ya decuantizados y envueltos en una `ShadeScene` local:
la consulta caliente es un lookup de diccionario, y el timeline de un dia
(~288 consultas en el mismo punto) cae entero en un bloque.

El LRU esta acotado: 64 bandas x 64x64 float32 (~1 MiB) + clases + canopy
~ 1.3 MiB por bloque; con `max_blocks=64` el techo es ~84 MiB por ciudad.
Dos detalles no obvios:

- Los handles de rasterio no son thread-safe: las lecturas van bajo un
  lock (los endpoints sync de FastAPI corren en un threadpool).
- El motor recalcula (row, col) contra el origen LOCAL del bloque, y en el
  borde ese redondeo float puede discrepar del calculo global (indice -1 o
  fuera del bloque). `scene_for` devuelve el centro del pixel como punto de
  consulta: con muestreo espacial nearest es identico y elimina el borde.

## Trampa tipica

"Es un .tif y se abre" no significa que sea un COG: un GeoTIFF en strips
obliga a leer media imagen para un recorte, y sobre HTTP es inutilizable.
Verificar el layout (tag `IMAGE_STRUCTURE:LAYOUT`) y no fiarse del nombre.
Ojo tambien con reescribir un COG con herramientas que no conservan el
layout (un `gdal_translate` sin opciones lo degrada a GTiff normal).

## Referencia

- https://cogeo.org/
- Driver COG de GDAL: https://gdal.org/en/stable/drivers/raster/cog.html
