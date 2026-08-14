# A\* y heuristicas admisibles

## Que es

Un algoritmo de camino minimo sobre grafos con pesos no negativos. Es
Dijkstra con brujula: en vez de expandir siempre el nodo mas cercano al
origen (`g(n)` = coste recorrido), expande el que minimiza `g(n) + h(n)`,
donde `h(n)` es una **heuristica**: una estimacion del coste que queda
hasta el destino. Con `h = 0` degenera en Dijkstra; cuanto mejor estima
`h`, menos nodos explora.

La garantia de optimalidad depende de una sola propiedad: `h` debe ser
**admisible** (nunca sobreestimar el coste real restante). Si ademas es
**consistente** (`h(u) <= coste(u,v) + h(v)`, la desigualdad triangular),
cada nodo que sale de la cola de prioridad esta cerrado para siempre y la
busqueda puede parar en cuanto sale el destino.

## Por que lo usamos aqui

La ruta a la sombra minimiza `coste = longitud * (1 + alfa * fraccion_sol)`
sobre el grafo peatonal (ver routing-graph.md). Nuestra heuristica es la
distancia euclidea al destino **en metros UTM** (en grados no seria una
distancia, ver crs.md), y sigue siendo admisible con el peso solar por una
cadena de desigualdades que conviene tener escrita:

    coste(arista) = longitud * (1 + alfa * fraccion) >= longitud >= euclidea

para todo `alfa >= 0` (la fraccion nunca es negativa). La consistencia se
hereda por el mismo argumento, asi que el corte temprano es correcto.

Implementacion en `shade_api.routing`: A\* de ~50 lineas con `heapq` sobre
una adyacencia CSR de numpy (sin networkx en runtime). Los predecesores
guardan el **indice de adyacencia**, no el nodo: entre dos nodos puede
haber aristas paralelas (la diagonal de la plaza al sol y el soportal en
sombra) y reconstruir por nodos perderia cual se eligio. El sondeo de
2026-07-12 midio ~10 ms por ruta en el grafo real de Cordoba: sobra para
servirlo en proceso.

## Origen y destino virtuales

Los pines caen en mitad de la calle, no en los cruces, asi que desde la
Fase 8.5 el origen y el destino son **puntos sobre una arista** (ver
point-segment-projection.md) y no nodos del grafo. La forma limpia de
enrutar entre ellos es la construccion clasica de **super-fuente /
super-sumidero**: imaginar dos nodos nuevos, O y D, conectados a los
extremos de sus respectivas aristas con el coste del trozo que falta.

En `_astar_virtual` eso no se materializa en el grafo (seria mutar el
artefacto); se expresa en la cola:

- **Semillas**: en vez de empezar con un solo nodo a coste 0, la cola nace
  con los DOS extremos de la arista de origen, cada uno con el coste
  parcial de llegar hasta el (`coste_arista * s / longitud`).
- **Destino**: un pseudo-nodo `_TARGET` con heuristica 0. Cuando se cierra
  un extremo de la arista de destino, se empuja `_TARGET` con el coste
  acumulado mas el trozo final. El primer `_TARGET` que sale de la cola es
  el optimo.
- **Caso misma arista**: si los dos pines comparten arista, el paseo
  directo entre ellos entra como candidato inicial. Compite de verdad:
  puede ganar salir por un extremo, dar la vuelta por la paralela en
  sombra y volver a entrar por el otro.

Que el coste parcial sea proporcional (`coste * s / L`) es exacto en
nuestro modelo, porque la fraccion de sol se guarda **por arista**: dentro
de una arista el coste por metro es constante. Y la admisibilidad
sobrevive: la arista virtual de entrada a D cuesta `c_d * s / L >= s >=`
euclidea desde ese extremo hasta el punto destino (porque `c_d >= L`), asi
que `h` sigue sin sobreestimar y el corte temprano sigue valiendo.

## Trampa tipica

**Normalizar el peso rompe el optimo en silencio.** Si algun dia el coste
se divide por `(1 + alfa)` (para que "coste" y "metros" queden comparables)
la cadena de arriba se rompe: el coste de una arista puede quedar por
debajo de su longitud y la euclidea pasa a SOBREestimar: A\* deja de ser
optimo sin avisar (devuelve rutas plausibles pero no minimas, que es la
peor clase de bug). Por eso `astar()` valida `coste >= longitud` y revienta
con ValueError si no se cumple: quien cambie la formula debe escalar la
heuristica en el mismo commit.

La otra clasica: A\* exige pesos no negativos (herencia de Dijkstra); un
"bonus" negativo por sombra (restar coste) lo invalida igual.

## Referencia

- Hart, Nilsson, Raphael (1968), "A Formal Basis for the Heuristic
  Determination of Minimum Cost Paths" (el paper original de A\*)
- https://www.redblobgames.com/pathfinding/a-star/introduction.html
  (la mejor explicacion visual que existe)
