# Sombra vegetal vs sombra de edificio

## Que es

Dos sombras que tapan el mismo sol no refrescan igual. Bajo una copa densa
el peaton gana, ademas de la sombra:

- **Evapotranspiracion**: el arbol bombea agua y la evapora por las hojas.
  Esa evaporacion consume calor latente del aire de alrededor (el mismo
  efecto que sudar) y baja la temperatura del aire unos grados.
- **Menor re-radiacion**: la sombra de un edificio suele venir acompanada
  de un muro y un pavimento que llevan horas cargandose de sol y devuelven
  infrarrojo al cuerpo. Una copa intercepta la radiacion arriba y bajo ella
  el suelo no se ha recalentado.

Lo que un peaton siente no es la temperatura del aire sino la **temperatura
radiante media** (MRT), que integra toda la radiacion que recibe el cuerpo.
Ahi es donde la diferencia se dispara: la literatura mide reducciones de
MRT bastante mayores bajo arbolado que bajo sombra de edificio, con la
misma temperatura de aire.

## Por que lo usamos aqui

La Fase 8.5 anade el parametro `beta` para poder pedir "prefiero ir por
debajo de los arboles". La formula del coste pasa a ser una **escalera de
penalizaciones**:

    coste = longitud * (1 + alfa * f_sol + beta * f_sombra_no_vegetal)

con `0 <= beta <= alfa`. Es decir: el sol penaliza `alfa`, la sombra de
edificio o de terreno penaliza `beta`, y la sombra vegetal no penaliza
nada (cuesta sus metros pelados). El orden fisico queda:
sol >= edificio >= arbol.

Para poder distinguirlas, el artefacto guarda desde el schema 2 una
**segunda matriz** por arista e instante (`veg_shade_fraction`), la
fraccion de muestras bajo copa. El raster de estado ya lo sabia
(`STATE_SHADE_VEGETATION`, que ademas absorbe el override de canopy), pero
el muestreo antiguo colapsaba los tres tipos de sombra en un "no sol". Lo
que queda sin nombrar, `1 - f_sol - f_vegetal`, es sombra de edificio o
terreno.

Honestidad de producto: `beta` es una **preferencia**, no grados. Decir
"prioriza arbolado" es correcto; decir "ruta mas fresca" no lo sera hasta
que la Fase 10 calcule MRT/UTCI de verdad.

## Trampa tipica

**Dar bonus en vez de penalizar.** La tentacion es restar coste a la
sombra vegetal ("premiar" el arbolado). Eso rompe el A\*: el coste caeria
por debajo de la longitud, la heuristica euclidea pasaria a sobreestimar y
la ruta dejaria de ser optima en silencio (ver a-star.md). Por eso la
escalera solo suma; el arbol "gana" siendo el unico que no paga peaje.

**Invertir el orden con beta > alfa.** Diria que caminar a la sombra de un
edificio es peor que al sol. El endpoint lo rechaza con un 400.

**La cuantizacion.** Cada matriz es uint8 (fraccion \* 255) y redondea por
separado, asi que `f_sol + f_vegetal` puede pasar de 1 por 1/255 y dejar la
resta `1 - f_sol - f_veg` ligeramente negativa. Se corrige con un `clip` a
[0, 1]; el loader ademas rechaza artefactos donde la suma se pase de verdad.

**Y el limite del dato**: la fraccion es por arista y por instante, no por
metro. Un tramo con arbolado solo en la primera mitad reparte la sombra
vegetal de forma uniforme al trocear la ruta.

## Referencia

- Konarska et al. (2014), transpiracion y balance energetico del arbolado
  urbano.
- Middel et al., "Sky View Factor / MRT" en canones urbanos aridos (la
  serie de Phoenix es la referencia clasica sobre sombra de arbol vs
  sombra de edificio).
- ISO 7726 / UTCI para la definicion formal de temperatura radiante media.
