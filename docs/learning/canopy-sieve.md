# Mascara de copa (canopy) y filtro sieve

## Que es

La mascara de copa marca los pixeles donde hay vegetacion POR ENCIMA de la
cabeza del observador: alli el motor declara sombra vegetal siempre que el
sol este alto (supuesto MVP de copa opaca), sin consultar el horizonte. Se
deriva del CHM (ver dsm-dtm-chm.md):

    canopy = (landcover == VEGETATION) & (DSM - DTM >= 2.5 m)

y despues se pasa un **sieve** (rasterio.features.sieve): elimina las
regiones conexas de la mascara menores que un umbral de area (8 px = 8 m2 a
1 m/px) sustituyendolas por el valor del vecino dominante.

## Por que lo usamos aqui

La clasificacion PNOA agrega vegetacion baja, media y alta (clases ASPRS
3/4/5) y el landcover no distingue: sin umbral de altura, cesped, setos y
arriates contaban como "copa" y pintaban sombra vegetal permanente (en
Cordoba, el 55% de los pixeles de vegetacion mide < 2.5 m). El umbral de
2.5 m deja solo la vegetacion bajo la que de verdad se camina. El sieve mata
el moteado de clasificacion urbano (retornos sueltos en fachadas, balcones,
mobiliario) que sobrevive al umbral. Las copas y setos siguen proyectando
sombra igual: el barrido de horizonte lee el DSM, que no se toca; la mascara
solo responde "hay copa encima de este pixel".

Se materializa como artefacto propio (`canopy.tif`, uint8 0/1) con los
parametros grabados como tags del COG: `build` lo escribe y `shade-engine
canopy <id>` lo deriva para artefactos anteriores sin re-barrer nada.

## Trampa tipica

- `sieve` exige dtype entero (un array bool revienta) y su conectividad por
  defecto es 4: hay que pasar `connectivity=8` para que los pixeles de copa
  en diagonal cuenten como una sola region.
- El sieve actua sobre regiones de AMBOS valores: tambien rellena huecos
  menores que el umbral dentro de copas grandes. Sesgo aceptado (un hueco de
  8 m2 dentro de una copa esta a la sombra en la practica) y fijado en test.
- El DTM se interpola bajo edificios: un arbol que asoma sobre un tejado
  tiene CHM inflado (incluye la altura del edificio). Como ya es vegetacion
  real de mas de 2.5 m, el veredicto no cambia.

## Referencia

- rasterio sieve: https://rasterio.readthedocs.io/en/stable/api/rasterio.features.html#rasterio.features.sieve
- Clases ASPRS LAS: https://www.asprs.org/divisions-committees/lidar-division/laser-las-file-format-exchange-activities
