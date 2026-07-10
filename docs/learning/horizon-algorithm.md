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
