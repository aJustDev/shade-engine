# Validacion de campo: Cordoba

Protocolo para contrastar las predicciones del motor con la realidad
(criterio de salida de la Fase 4: predicciones correctas en la mayoria de
puntos de contraste, documentado). El resultado es material para el README:
foto con hora vs prediccion.

## AVISO: verificar las coordenadas antes del paseo

Las coordenadas de `validacion-cordoba-puntos.csv` son aproximadas (de
conocimiento general, error posible de decenas de metros). Antes del paseo,
abrir cada punto en un mapa, ajustar el pin al sitio exacto elegido y
corregir el CSV. Reglas al ajustar:

- Ningun punto sobre un edificio (el motor responde la verdad del pixel:
  un pin sobre tejado predice el horizonte del tejado).
- Los puntos "faciles" a mas de 10 m de cualquier fachada: el GPS urbano
  tiene 5-10 m de error y a 1 m/pixel la respuesta puede cambiar de pixel.
- Los puntos de estres (calles estrechas) son la excepcion deliberada: alli
  el pin se fija por referencia fisica (esquina, farola), no por GPS.

## Puntos de contraste

| id            | Punto                                     | Sombra esperada  | Dificultad |
| ------------- | ----------------------------------------- | ---------------- | ---------- |
| tendillas     | Plaza de las Tendillas (centro)           | edificio         | facil      |
| corredera     | Plaza de la Corredera (interior lado sur) | edificio         | facil      |
| naranjos      | Patio de los Naranjos (bajo los naranjos) | vegetal + muro   | media      |
| potro         | Plaza del Potro                           | edificio         | facil      |
| ribera        | Paseo de la Ribera (junto al murete)      | abierto/edificio | facil      |
| puerta-puente | Explanada Puerta del Puente / Triunfo     | abierto          | facil      |
| gran-capitan  | Bulevar Gran Capitan (eje arbolado)       | vegetal          | media      |
| victoria      | Jardines de la Victoria                   | vegetal          | media      |
| flores        | Calleja de las Flores (Juderia, ~2 m)     | edificio         | estres     |
| deanes        | Calle Deanes (Juderia)                    | edificio         | estres     |

Mezcla deliberada: plazas abiertas (robustas a GPS), sombra vegetal (prueba
el supuesto de copa opaca; ojo, el vuelo es de 2024 y el arbolado crece) y
dos callejas de la Juderia que estresan resolucion de 1 m + snap de pixel.

## Protocolo

1. Generar la hoja de predicciones para la fecha del paseo:

   ```
   uv run shade-engine predict cordoba docs/validacion-cordoba-puntos.csv --day 2026-07-20
   ```

   (requiere el build de Cordoba en `data/cities/cordoba/v1`).

2. Planificar el recorrido alrededor de TRANSICIONES predichas: visitar
   cada punto en una ventana de +-15 min alrededor de un cambio sol->sombra
   o sombra->sol, mas una visita en estado estable. Las transiciones son la
   prueba fuerte; un "sombra a las 18:00" en una calle de 2 m casi no
   informa.

3. En cada visita:
   - Foto del suelo alrededor del punto (1-2 m a la redonda).
   - Foto hacia el obstaculo que da (o no) la sombra.
   - Pin GPS o captura del mapa con la posicion real.
   - La hora va en el EXIF (movil en hora automatica) y ademas apuntada a
     mano por si la compresion/exportacion pierde metadatos.

4. Veredicto observado: "sombra" si mas del 50% del metro alrededor del pin
   esta en sombra (evita discutir con la penumbra del borde).

5. Rellenar la tabla de resultados y traerla a la sesion de cierre de fase.

## Resultados

| id  | fecha | hora local | predicho (estado/tipo) | observado | acierto | notas |
| --- | ----- | ---------- | ---------------------- | --------- | ------- | ----- |
|     |       |            |                        |           |         |       |

Criterios al evaluar: un fallo de +-10 min en una transicion es un acierto
con nota (el paso del timeline es de 5 min y el borde de sombra se mueve
rapido); un fallo de estado en pleno intervalo estable es un fallo real.
Anotar tambien la causa aparente (GPS, arbol crecido, obra posterior al
vuelo, clasificacion LiDAR) para el item de ajuste de precision.
