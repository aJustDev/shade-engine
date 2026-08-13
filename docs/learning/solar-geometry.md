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

## La declinacion y la escalera de fechas (tiles)

La declinacion solar (el angulo del sol sobre el plano del ecuador) es lo
UNICO que cambia entre dias: fijada la declinacion y la hora solar, la
posicion del sol -- y por tanto la sombra -- es la misma. Recorre +-23.44
grados en el año y es simetrica alrededor de los solsticios: el 4 de mayo y
el 9 de agosto tienen la misma declinacion y sombras practicamente
identicas. Por eso los tiles no muestrean el calendario (52 semanas serian
~27 patrones utiles, con vecinos casi iguales) sino la declinacion: 7
fechas canonicas a pasos de ~7.8 grados cubren el año entero, y el manifest
publica el mapeo dia -> "gemela de declinacion" (campo `ladder`). Error
maximo en el peor dia del rango de un peldaño: ~4 grados de declinacion,
por debajo de lo que se aprecia en un overlay urbano.

Trampa asociada: la declinacion NO es simetrica alrededor de los
equinoccios en el calendario (la orbita es eliptica; el otoño llega ~3 dias
"tarde"): los rangos del mapeo se calculan con la serie de Spencer, no
restando fechas a mano.

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
