# Frente de Pareto (dar varias rutas y puntuarlas)

## Que es

Cuando un problema tiene **dos objetivos que compiten** no existe "la"
solucion optima, sino un conjunto de ellas. Aqui los objetivos son andar
poco y andar a la sombra: la ruta mas corta suele ser la mas soleada, y
esquivar el sol cuesta metros.

Una ruta **domina** a otra si no es peor en ningun objetivo y es mejor en
alguno (mas corta y con igual o menos sol). Las rutas que nadie domina
forman el **frente de Pareto**: cada una es la mejor eleccion posible para
alguien, y elegir entre ellas ya no es matematica sino gusto. Las
dominadas se pueden tirar sin discusion: son peores en todo.

## Por que lo usamos aqui

`?alternatives=true` no lanza un algoritmo nuevo: hace un **barrido de
alfas** (0, 0.5, 1, 2, 4, 8) con el A\* que ya existe, ~10 ms cada
pasada en Cordoba. Convertir dos objetivos en un solo numero ponderandolos
se llama **escalarizacion**, y cada alfa es una ponderacion distinta: alfa
0 solo mira metros, alfa 8 casi solo mira sol. El optimo de cada alfa es,
por construccion, un punto no dominado.

Rutas repetidas entre alfas vecinas se deduplican por su secuencia de
tramos (`EdgeSpan`), quedandose con el alfa mas pequeno que la produjo, y
luego se filtran las dominadas. Beta viaja en la misma proporcion que pidio
el usuario, para que la preferencia por arbolado no se diluya al subir
alfa. Frente a k-shortest-paths (Yen y compania) esto es mas simple, mas
barato y da variantes realmente distintas en vez de primos hermanos de la
misma ruta.

## Trampa tipica

**La suma ponderada solo alcanza la envolvente convexa del frente.** Es el
limite conocido de la escalarizacion lineal: hay rutas no dominadas que
NINGUN alfa devuelve, porque quedan en una concavidad del frente. El
barrido es una **muestra** del frente, no el frente entero. Esta bien para
ofrecer opciones; no lo esta para afirmar "estas son todas".

**Creer que deduplicar basta.** Con un solo peso activo (beta = 0) el
barrido sale ordenado: mas alfa, mas largo y menos sol, y no aparecen
dominadas. En cuanto beta > 0 el numero escalarizado mezcla sol y sombra
de edificio, y deja de ser monotono en el plano (longitud, sol): puede
colarse una ruta mas larga Y mas soleada que otra. Por eso el filtro de
dominancia se aplica siempre, aunque muchas veces no quite nada.

## Referencia

- Multi-objective shortest path (planteamiento general):
  https://en.wikipedia.org/wiki/Multi-objective_optimization
- Sobre el limite de la escalarizacion lineal (soluciones soportadas vs no
  soportadas): Ehrgott, "Multicriteria Optimization", cap. 3.
