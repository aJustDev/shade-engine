# shade-engine

Motor open source de calculo de sombra urbana: core, pipeline y API.

**La documentacion vive fuera de este repo**, en el vault privado
`~/shade/docs` (repo `shade-docs`). La entrada canonica es
`~/shade/docs/docs/INDEX.md`. Aqui solo quedan `README.md` (publico, en
ingles) y este fichero.

Repos hermanos: `~/shade/web` (frontend React, privado) y `~/shade/docs`.

## Comandos

- `uv sync --all-packages` - instalar el workspace completo
- `uv run pytest` - tests
- `uv run ruff check .` y `uv run ruff format .` - lint y formato
- `uv run mypy` - type check (config en pyproject raiz)

## Convenciones

- Monorepo con uv workspace: `core/` (dominio compartido), `pipeline/` (CLI de
  artefactos raster), `api/` (FastAPI). Los tres son paquetes con src layout;
  `pipeline` y `api` dependen de `shade-core`.
- Codigo, docstrings y commits en ingles.
- Solo ASCII en todo output (codigo, commits).
- Los rasteres nunca van a git ni a Postgres: viven en `data/` (ignorado) o en
  el storage del despliegue.

## Objetivo didactico (obligatorio)

Este proyecto es tambien un vehiculo de aprendizaje sobre geomatica y calculo
solar. El agente debe explicar los conceptos a medida que los usa, no solo
aplicarlos:

- Antes de implementar cada pieza geoespacial, explicar el concepto en la
  conversacion y dejarlo escrito: que es un CRS y por que usamos EPSG:25830
  (UTM) para calcular y EPSG:4326/3857 para servir; que diferencia hay entre
  DSM, DTM y CHM; que son primeros retornos y clases LiDAR; que es un COG y
  por que permite lecturas por ventana; que son azimut, elevacion solar,
  declinacion y ecuacion del tiempo; como funciona el algoritmo de horizonte
  por sectores; que es un vector tile / PMTiles.
- **La nota del concepto va en `~/shade/docs/docs/learning/<concepto>.md`**
  (formato: que es, por que lo usamos aqui, trampa tipica, referencia), escrita
  en la MISMA SESION en que el concepto aparece por primera vez. El commit de
  este repo la cita como `shade-docs: learning/<nota>.md`, que es la forma que
  usan ya los docstrings. La atomicidad commit-a-commit no es posible entre
  repos; la disciplina es no cerrar la sesion sin la nota.
- Docstrings didacticos en `core/`: las funciones de geometria solar y
  horizonte deben incluir la explicacion matematica (formulas, unidades,
  convenciones de signo - p.ej. azimut 0 = Norte, sentido horario) y no asumir
  que el lector conoce el dominio.
- Al elegir entre alternativas tecnicas geo (interpolacion de horizonte,
  resampling, estrategia de tiling), exponer brevemente las opciones y el
  porque de la eleccion, no decidir en silencio.
- Trampas a explicar explicitamente cuando toquen: confusion lat/lon vs
  lon/lat entre librerias, distorsion de distancias en Web Mercator, timezone
  vs hora solar, y por que nunca se calculan distancias en grados.

## Decisiones

Una decision estructural nueva se registra como ADR en
`~/shade/docs/docs/decisions/`; una eleccion puntual, como una linea en
`registro-historico.md`. Al cerrar una fase, actualizar su nota en
`~/shade/docs/docs/milestones/`.

Lo que ya esta decidido y no conviene reabrir sin leer su ADR: horizonte
precomputado por sectores (ADR-001), observador en DTM+1.6 m (ADR-002),
clasificacion por sector contribuyente (ADR-003), COG band-interleave con
relectura (ADR-005), grafo peatonal como artefacto congelado (ADR-009), A\*
propio sin networkx (ADR-010) y el arbolado como penalizacion menor, nunca
bonus (ADR-012).
