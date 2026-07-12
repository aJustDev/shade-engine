# Production image for the shade-engine API (uv workspace, multi-stage).
#
# The whole workspace is installed, not just shade-api: the extra weight is
# small (laspy, shapely, typer, httpx -- numpy/rasterio already come with
# shade-core) and it puts the `shade-engine` CLI in the image, so prod data
# imports are one `docker compose run --rm api shade-engine import-layer ...`
# away, with no uv on the host and no ssh tunnels.

FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependency layer: manifests only, so source edits do not bust the cache.
# --all-packages is mandatory: the workspace root is virtual (package =
# false), a plain sync would install nothing at all.
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/pyproject.toml
COPY pipeline/pyproject.toml pipeline/pyproject.toml
COPY api/pyproject.toml api/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --all-packages

# Workspace layer: sources + non-editable install, so the venv is
# self-contained and the source tree does not ship in the runtime image.
COPY core/src core/src
COPY pipeline/src pipeline/src
COPY api/src api/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --all-packages

FROM python:3.14-slim

# Everything in the lock resolves to manylinux wheels (psycopg[binary]
# included). The one system library needed is libexpat1: rasterio's wheel
# links it but does not vendor it, and the official python image bundles
# its own expat instead of the Debian one.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

COPY --from=builder /app/.venv /app/.venv
# The one-shot migrate service runs `alembic upgrade head` from this image
# before the API starts serving.
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
# City configs and vector layers live in git and ship with the image; raster
# artifacts (data/cities, ~2.4 GB) are bind-mounted read-only by compose.
COPY cities ./cities

USER app
EXPOSE 8000

# Slim images have no curl; probe /healthz with the stdlib instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# Worker count comes from WEB_CONCURRENCY (uvicorn's native default for
# --workers), settable from compose without a rebuild. Trusting every proxy
# hop ("*") is safe only because compose publishes this port on the host
# loopback: the sole client is Caddy, and inside the container the peer
# address is the bridge gateway, which is not stable enough to pin.
CMD ["uvicorn", "shade_api.app:app", "--host", "0.0.0.0", "--port", "8000", \
    "--proxy-headers", "--forwarded-allow-ips", "*"]
