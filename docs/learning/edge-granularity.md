# Granularidad por arista (por que la ruta no se colorea mas fino)

## Que es

El artefacto del grafo guarda **una fraccion de sol y una de copa por
arista y por instante** de la escalera de declinacion (ver routing-graph.md
y vegetation-cooling.md): al construirlo se muestrea cada 5 m a lo largo
del arco y se promedia. Una calle de 100 m no dice "del metro 0 al 40 hay
sol"; dice "esta arista estaba al 40% de sol a esa hora".

La arista es ademas la **unidad de decision del router**: el A\* pondera
aristas enteras, no metros. Por eso la descomposicion de una ruta en tramos
por arista (`RouteLeg.segments`) es la rebanada mas fina que se puede
mostrar sin inventar. Un tramo parcial (cuando el pin cae en mitad de la
calle) se cobra a prorrata pero **conserva la fraccion de su arista sin
escalarla**: lo que interesa es como de soleada esta la calle, no cuanto de
ella se recorrio.

Medido en Cordoba, sobre el cruce este-oeste del casco: 40 tramos, 39 m de
media, y el **85% son inequivocos** (mas del 90% o menos del 10% de sol).
Solo 3 de 40 quedan realmente mezclados. Por eso pintar cada tramo con una
sola clase es honesto casi siempre.

## Por que lo usamos aqui

Para colorear el trazo hay que contar una historia **categorica** (sol /
sombra de edificio / sombra de arbol) a partir de dos numeros
**continuos**. La division de trabajo elegida:

- la API devuelve los tramos crudos con sus dos fracciones y **no
  clasifica**;
- el cliente decide el criterio (argmax de las tres cuotas, con los empates
  a favor del sol) y **fusiona tramos consecutivos de la misma clase**, que
  es puro renderizado (40 -> 19 en Cordoba).

Asi el motor no se casa ni con una paleta ni con un umbral, y otro
consumidor puede pintar una escala continua en vez de tres clases.

## Trampa tipica

**Creer que el color marca donde acaba la sombra en la acera.** No: el
color responde "como estaba esta calle EN PROMEDIO a esa hora". El borde
real de la sombra vive en los tiles raster, y ambos pueden discrepar en los
extremos de una calle larga. Es el mismo efecto que el **MAUP** de la
cartografia tematica: agregar un campo continuo sobre unidades predefinidas
hace que el resultado dependa de esas unidades tanto como del fenomeno.
Pintar a 5 m mostraria un detalle que el router no puede aprovechar, porque
no sabe decidir por debajo de la arista.

**De noche todas las fracciones son 0**, asi que un argmax ingenuo calcula
`1 - 0 - 0 = 1` de "otra sombra" y pinta la ciudad entera de sombra de
edificio. Sin sol no hay nada que clasificar: el cliente debe cortar por
`status == "night"` y decirlo, no inventarlo.

## Referencia

- routing-graph.md (como se muestrean las fracciones por arista)
- MAUP: https://en.wikipedia.org/wiki/Modifiable_areal_unit_problem
