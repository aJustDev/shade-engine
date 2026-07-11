# LiDAR aereo: retornos y clases

## Que es

Un avion barre el terreno con pulsos laser y mide el tiempo de vuelo de cada
rebote. Cada pulso puede rebotar varias veces: en una copa de arbol parte de
la energia vuelve arriba (**primer retorno**) y parte sigue hasta el suelo
(**retornos posteriores**). Por eso el primer retorno "ve" la superficie
superior (copas, tejados) y los ultimos suelen ver el suelo bajo vegetacion.
Ademas del retorno, el proveedor clasifica cada punto con los codigos ASPRS;
las clases que usa PNOA y que consumimos aqui:

- `2` suelo
- `3` / `4` / `5` vegetacion baja / media / alta
- `6` edificio
- `7` / `18` ruido bajo / alto (reflejos, pajaros, aerosoles): se descartan
- `12` solape entre pasadas del vuelo (peor geometria): se descarta

LAS 1.4 anade ademas flags por punto, aparte de la clase: `withheld` (la
spec manda excluirlo del procesado), `overlap` (equivalente moderno de la
clase 12) y `synthetic` (punto VALIDO generado por otra tecnica; el caso
tipico es el hidro-aplanado: laminas de agua rellenadas como clase 2 +
synthetic).

## Por que lo usamos aqui

- DSM = maximo z de **primeros retornos** por celda (los obstaculos que
  proyectan sombra).
- DTM = puntos **clase 2** por celda, sea cual sea su numero de retorno
  (bajo copa el eco del suelo es un retorno posterior; descartarlo dejaria
  el DTM sin datos justo donde mas lo necesitamos).
- Landcover (building/vegetation/ground) = clase del punto que fijo el DSM
  de la celda: es lo que la consulta reporta como "que da esta sombra".

PNOA distribuye tiles LAZ en el UTM local (EPSG:25830 en Cordoba): otro
motivo de calcular en ese CRS (cero reproyecciones). La 2a cobertura
(~0.5-2 pt/m2, tiles de 2x2 km) y la 3a (~5 pt/m2, tiles de 1x1 km, 2024 en
Cordoba) usan formato de punto 6 de LAS 1.4.

El filtrado de ruido importa mas de lo que parece por como agregamos: el DSM
es un **max** por celda. Un outlier por debajo del terreno lo absorbe el
suelo (`dsm = max(dsm, dtm)`), pero un unico punto de ruido a +50 m sobre
una calle no tiene defensa: seria el DSM de su celda y, como el barrido de
horizonte mira hasta `max_distance` (500 m), apareceria como obstaculo
fantasma en el perfil de ~10^5 pixeles vecinos. Por eso 7/18/12, `withheld`
y `overlap` se tiran antes del binning. `synthetic` NO se tira: en el
Guadalquivir es lo unico que da suelo al DTM (sin el, el rio seria un
agujero mas ancho que el radio de rellenado y el build abortaria).

## Trampa tipica

Dos del formato LAS y una del dato:

- Los formatos de punto 0-5 empaquetan la clasificacion en 5 bits
  compartidos con flags (clases > 31 imposibles); el formato 6 le da un byte
  entero. Al generar ficheros sinteticos, usar formato 6.
- `return_number` es un subcampo de 4 bits **1-based**: un punto con valor 0
  es invalido y algunas librerias lo escriben sin quejarse.
- La clasificacion del vuelo no es perfecta: gruas y torres aparecen como
  "edificio", fachadas como vegetacion, y coches/mobiliario quedan sin
  clasificar. El landcover hereda ese ruido; se asume y se documenta.

## Referencia

- Especificacion LAS 1.4 (ASPRS): https://www.asprs.org/wp-content/uploads/2019/07/LAS_1_4_r15.pdf
- Especificaciones PNOA LiDAR (IGN): https://pnoa.ign.es/pnoa-lidar/especificaciones-tecnicas
