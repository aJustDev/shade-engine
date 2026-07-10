# Geometria solar: azimut, elevacion, declinacion

## Que es

La posicion del sol vista desde un punto se describe con dos angulos:

- **Azimut**: angulo horizontal. Convencion del proyecto (y de pvlib):
  0 = Norte, sentido horario -> 90 = Este, 180 = Sur, 270 = Oeste.
- **Elevacion**: angulo vertical sobre el horizonte (zenith = 90 - elevacion).

Dos fenomenos gobiernan como cambian a lo largo del ano:

- **Declinacion**: angulo del sol respecto al ecuador terrestre. Oscila entre
  +23.44 (solsticio de junio) y -23.44 (diciembre) porque el eje de la Tierra
  esta inclinado. Formula de servilleta: elevacion al mediodia solar =
  90 - latitud + declinacion. Cordoba (37.88 N): ~75.6 en junio, ~28.7 en
  diciembre, ~52.1 en equinoccios.
- **Ecuacion del tiempo**: la orbita es eliptica y el eje inclinado, asi que
  el mediodia solar verdadero se adelanta/atrasa hasta +-16 min respecto al
  tiempo medio de reloj segun la epoca del ano.

Ademas, la **refraccion atmosferica** curva la luz y "sube" el sol aparente
~0.5 grados cuando esta en el horizonte. La elevacion _aparente_ (la que
usamos) es la del sol que se ve; la _geometrica_ es la del sol sin atmosfera.

## Por que lo usamos aqui

`sombra = elevacion_solar < horizonte(azimut)`: el motor entero es comparar
estos dos angulos contra el raster de horizonte, que almacena exactamente las
mismas magnitudes. pvlib (algoritmo SPA de NREL) calcula la posicion con
precision de fracciones de grado y vectorizado, y ya esta validado: no
reimplementamos efemerides.

## Trampa tipica

Timezone vs hora solar. El huso Europe/Madrid va adelantado respecto al sol
en Cordoba: el mediodia solar cae hacia las 14:20 en horario de verano (CEST)
y ~13:20 en invierno. Dos causas que se suman: la hora oficial de Espana no
corresponde a su longitud (Madrid deberia ir con UTC+0) y la ecuacion del
tiempo anade su vaiven de +-16 min. Nunca hardcodear "mediodia = 12:00";
preguntar siempre a la efemeride. Y nunca pasar datetimes naive: el core los
rechaza (ValueError); resolver "sin offset = TZ de la ciudad" es cosa de la
API.

## Referencia

- NOAA Solar Calculator: https://gml.noaa.gov/grad/solcalc/
- pvlib solarposition: https://pvlib-python.readthedocs.io/en/stable/reference/solarposition.html
