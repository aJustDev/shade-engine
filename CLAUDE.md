# shade-engine

Motor open source de calculo de sombra urbana. Spec completo:
`docs/shade-engine-mvp.md`. Plan de fases (documento vivo): `docs/plan.md`.

## Comandos

- `uv sync --all-packages` - instalar el workspace completo
- `uv run pytest` - tests
- `uv run ruff check .` y `uv run ruff format .` - lint y formato
- `uv run mypy` - type check (config en pyproject raiz)

## Convenciones

- Monorepo con uv workspace: `core/` (dominio compartido), `pipeline/` (CLI de
  artefactos raster), `api/` (FastAPI). Los tres son paquetes con src layout;
  `pipeline` y `api` dependen de `shade-core`.
- Codigo, docstrings y commits en ingles. Documentos de `docs/` y
  `docs/learning/` en castellano.
- Solo ASCII en todo output (codigo, docs, commits).
- Al completar items de una fase: marcar checkboxes en `docs/plan.md` y anotar
  decisiones nuevas en su registro de decisiones.
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
- Mantener `docs/learning/` con una nota corta por concepto (formato: que es,
  por que lo usamos aqui, trampa tipica, enlace de referencia). Anadir la nota
  en el mismo commit donde el concepto aparece por primera vez.
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
