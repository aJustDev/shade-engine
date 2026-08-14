# Proyeccion punto-segmento (enganchar un pin a la calle)

## Que es

Dado un punto `p` y un segmento que va de `a` a `b`, el punto del segmento
mas cercano a `p`. Se resuelve con una proyeccion escalar y un clamp:

    d = b - a
    t = producto_escalar(p - a, d) / |d|^2      # posicion sobre la RECTA
    t = clip(t, 0, 1)                           # confinar al SEGMENTO
    q = a + t * d                               # el punto mas cercano

`t` es la fraccion recorrida del segmento: 0 = el extremo `a`, 1 = el
extremo `b`, 0.5 = el punto medio. Sin el clamp estariamos midiendo contra
una recta infinita, no contra el tramo de calle que existe.

Extenderlo de un segmento a una polilinea (una calle con quiebros) es
repetir la cuenta por segmento y quedarse con el minimo. Y de una polilinea
a una ciudad entera, lo mismo: en `RouteGraph.build` todas las aristas se
aplanan en una **tabla de segmentos** (arrays paralelos `seg_x`, `seg_dx`,
`seg_len`, `seg_arc0`, `seg_edge`), asi que `snap_point` resuelve las
~200.000 proyecciones de Cordoba en una sola pasada vectorizada de numpy,
un par de milisegundos. `seg_arc0` guarda el arco acumulado desde el inicio
de SU arista, de modo que un acierto en el segmento `i` con parametro `t`
se traduce a "estas a `s` metros del extremo u de la arista e" sin llevar
contabilidad por arista.

## Por que lo usamos aqui

La gente pone el pin en mitad de una calle, no en un cruce. Hasta la Fase
8.5 el origen y el destino se pegaban al **nodo** mas cercano y en Cordoba
eso se veia: rutas que arrancaban a 100 m del pin, en la esquina, porque la
manzana no tiene cruces intermedios. Enganchando al punto sobre la arista,
el error de snap pasa a ser la distancia real del pin a la calle (metros) y
la ruta empieza donde el usuario la pidio.

Detalle que hace falta cuidar: el arco `s` se mide en la escala de la
longitud almacenada de la arista (float32), mientras que la suma de
segmentos se hace en float64. Difieren en ~1e-7 m; por eso `snap_point`
hace clamp de `s` a `[0, edge_len]` y el recorte de geometria reescala el
arco antes de interpolar. Sin eso, recorrer una arista entera se quedaba a
una millonesima de metro de su ultimo vertice.

## Trampa tipica

**Olvidar el clamp**: sin el, un pin situado "mas alla" del final de la
calle proyecta fuera del segmento y el snap devuelve un punto que no
pertenece al viario (y un `s` negativo o mayor que la longitud, que luego
rompe el troceado de la ruta).

**Segmentos de longitud cero**: OSM tiene vertices repetidos; `|d|^2 = 0`
divide por cero. Se resuelve sustituyendo el divisor por 1 cuando es cero
(el `t` resultante da igual: el segmento es un punto).

**Comparar distancias con raiz cuadrada**: para elegir el minimo basta el
cuadrado de la distancia. Sacar 200.000 raices para luego quedarse con una
es tirar tiempo; la raiz se calcula solo para el ganador.

Y la trampa heredada del snap a nodos, que sigue viva: el mas cercano _en
linea recta_ puede estar al otro lado de un rio o de un muro. La ruta
seguira siendo correcta, pero empezara en la otra orilla.

## Referencia

- "Distance from a point to a line" (deduccion clasica):
  https://en.wikipedia.org/wiki/Distance_from_a_point_to_a_line
- Ericson, "Real-Time Collision Detection", seccion 5.1.2 (closest point
  on segment), la version canonica del clamp.
