# DSM, DTM y CHM

## Que es

Tres modelos rasterizados de elevacion derivados de la misma nube LiDAR:

- **DSM** (Digital Surface Model): la superficie que ve el cielo — tejados,
  copas de arboles, coches. Se construye con los **primeros retornos** de
  cada pulso laser (lo primero que toca el haz).
- **DTM** (Digital Terrain Model): el suelo desnudo, sin nada encima. Se
  construye con los puntos clasificados como **suelo (clase LiDAR 2)**,
  interpolando bajo edificios y copas donde el laser no llego al suelo.
- **CHM** (Canopy Height Model): altura de la vegetacion sobre el suelo.
  No es un dato nuevo: CHM = DSM - DTM (sobre pixeles de vegetacion).

## Por que lo usamos aqui

El motor necesita los dos primeros con papeles opuestos:

- El **DSM** son los _obstaculos_: lo que proyecta sombra.
- El **DTM** es donde esta el _observador_: una persona camina sobre el
  terreno, no sobre los tejados. Observador = DTM + 1.6 m.

Mezclarlos es el error clasico: calcular el horizonte "desde el DSM" pone al
observador encima de la copa del arbol y el motor dice "sol" justo donde el
arbol te esta dando sombra.

## Trampa tipica

El DSM es una foto del dia del vuelo LiDAR: arboles podados o crecidos,
edificios nuevos y gruas no estan. Y los caducifolios se levantaron con
hojas o sin ellas segun la fecha de la campana: un DSM volado en invierno
subestima la sombra de verano y viceversa (nuestro supuesto MVP: copa opaca,
sesgo documentado).

## Referencia

- Especificaciones PNOA LiDAR (IGN): https://pnoa.ign.es/pnoa-lidar/especificaciones-tecnicas
- Clases ASPRS LAS: https://www.asprs.org/divisions-committees/lidar-division/laser-las-file-format-exchange-activities
