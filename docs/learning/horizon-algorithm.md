# Algoritmo de horizonte por sectores

## Que es

En vez de precomputar rasteres de sombra por fecha x hora (explosion
combinatoria), cada pixel almacena su "huella angular": para N sectores de
azimut (64 -> sectores de 5.6 grados), el angulo de elevacion del obstaculo
mas alto visible en esa direccion. Un pixel en una calle estrecha orientada
N-S guarda angulos altos hacia E y O, bajos hacia N y S.

Con eso, cualquier consulta de sombra para cualquier instante se reduce a:

    sombra = elevacion_solar < horizonte(azimut_solar)

El computo caro (mirar hasta max_distance en 64 direcciones desde cada pixel)
se paga una vez por ciudad; la consulta es una comparacion de dos floats.

## Por que lo usamos aqui

- Un solo artefacto por ciudad valido para cualquier fecha y hora, frente a
  cientos de rasteres de sombra precalculados.
- El observador se situa en DTM + 1.6 m (nivel de calle) y los obstaculos se
  toman del DSM. Calcular desde el DSM pondria al observador encima de las
  copas y tejados: diria "sol" bajo un arbol que te esta dando sombra.
- `max_distance` (500 m - 1 km en produccion) acota coste y memoria. El
  precio: angulos de horizonte muy bajos quedan truncados, es decir, sombras
  kilometricas de sol rasante (amanecer/atardecer) se pierden. Irrelevante
  para aparcar a la sombra.

## Decisiones de muestreo

- **Azimut: interpolacion lineal circular** entre los dos sectores
  adyacentes (con wraparound 360 -> 0). Nearest erraria hasta medio sector
  (~2.8 grados con 64), que a 10 m de un edificio son metros de frontera de
  sombra mal puesta.
- **Espacio: pixel mas cercano, nunca bilinear.** El perfil de horizonte es
  discontinuo en una pared: promediar el perfil de un tejado con el de la
  calle da un angulo que no describe ningun sitio real.
- **Referencia por fuerza bruta como oraculo**: `compute_horizon_reference`
  camina la linea de vision en pasos de medio pixel y se queda con
  `max(atan2(z_obstaculo - z_observador, distancia))`. Lenta y obviamente
  correcta; la version vectorizada/tileada del pipeline (Fase 2) debe
  reproducir sus valores sobre los mismos fixtures.

## La version de produccion (pipeline, Fase 2)

El barrido del pipeline reproduce el muestreo del oraculo exactamente (mismas
distancias de medio pixel, mismos offsets con `round()`, misma matematica en
float64) con dos reestructuraciones que no cambian el resultado:

- **Deduplicado de offsets**: varias distancias consecutivas caen en la misma
  celda; basta conservar la menor (con dz >= 0 el atan2 decrece con la
  distancia, y con dz < 0 el floor a 0 absorbe todo). Es exacto, no una
  aproximacion: los tests exigen igualdad bit a bit con el oraculo.
- **Tiling con buffer**: la ciudad se barre por tiles, cada uno leyendo una
  ventana acolchada con `ceil(max_distance / resolucion)` pixeles. Como
  ningun offset supera ese acolchado, el resultado es independiente del
  tamano de tile y la memoria queda acotada (~10^8 pixeles x 64 sectores no
  caben de una pieza).

Dos artefactos salen del mismo barrido:

- **Horizonte cuantizado a uint8**: angulo \* 255/90, paso ~0.353 grados,
  error <= ~0.18 (muy por debajo de la discretizacion de medio pixel del
  propio barrido). Mitad de disco que uint16 y la escala viaja como tag del
  GeoTIFF (fichero autodescriptivo).
- **Clase del bloqueador por sector**: cuando una muestra sube el maximo de
  un sector, se apunta el landcover de esa celda. Asi "que da esta sombra"
  cuesta una lectura de 1 pixel en runtime, en vez de un ray-march sobre
  DSM+DTM+landcover (3 ventanas COG extra por consulta). Empates los gana el
  bloqueador mas cercano; sectores con horizonte 0 guardan 255 (cielo).

## Modo geometric (Fase 4)

El modo `exact` muestrea cada sector a paso constante de medio pixel: a
1 m/px y 500 m de radio, ~1000 distancias por sector. La observacion que
explota el modo `geometric`: el error angular de saltarse un obstaculo
depende de la distancia RELATIVA, no absoluta. Medio metro importa a 5 m
del observador (10% de la distancia) y es irrelevante a 400 m (0.125%).

Por eso el paso crece multiplicativamente: `d = max(d + paso, d * 1.02)`.
Denso cerca, ralo lejos, error relativo acotado ~2%. Las muestras caen de
~1000 a ~350 (regimen exponencial: `ln(2 * max_d / res) / ln(growth)`), y
el coste del barrido baja en esa proporcion.

El precio: puede saltarse obstaculos FINOS lejanos (una chimenea a 300 m
cabe entre dos muestras separadas 6 m). Por eso nunca se valida contra el
oraculo bit a bit: dos discretizaciones correctas del mismo continuo
discrepan legitimamente en pixeles sueltos (roce de esquina), y la
validacion es por cuantiles de la diferencia angular. Se expone como
`--step-mode` en el CLI; la eleccion exact/geometric para cada ciudad se
decide midiendo (probe de Fase 4) y queda registrada en su metadata.json.

## Trampa tipica

Confundir "el sol esta sobre el horizonte astronomico" (elevacion > 0, es de
dia) con "el sol es visible desde este pixel" (elevacion > horizonte local).
La primera decide dia/noche; la segunda, sol/sombra. Y el caso bajo copa no
lo resuelve el horizonte: se resuelve con la mascara de landcover (si tienes
vegetacion encima, estas a su sombra siempre que sea de dia).

## Referencia

- Dozier & Frew (1990), "Rapid calculation of terrain parameters for
  radiation modeling from digital elevation data" (el algoritmo clasico de
  horizonte); r.horizon de GRASS GIS y UMEP usan la misma idea.
