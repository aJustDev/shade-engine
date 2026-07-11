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

## Trampa tipica

"Es un .tif y se abre" no significa que sea un COG: un GeoTIFF en strips
obliga a leer media imagen para un recorte, y sobre HTTP es inutilizable.
Verificar el layout (tag `IMAGE_STRUCTURE:LAYOUT`) y no fiarse del nombre.
Ojo tambien con reescribir un COG con herramientas que no conservan el
layout (un `gdal_translate` sin opciones lo degrada a GTiff normal).

## Referencia

- https://cogeo.org/
- Driver COG de GDAL: https://gdal.org/en/stable/drivers/raster/cog.html
