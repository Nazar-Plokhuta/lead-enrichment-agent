# syntax=docker/dockerfile:1

# Python is pinned to the version CI tests against. Playwright's own images are
# avoided on purpose: their bundled browser revision tracks a fixed Playwright
# release, while uv.lock decides which revision the pip package expects.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Manifests only, so this layer is reused until dependencies actually change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM ${PYTHON_IMAGE} AS runtime

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Browsers live outside any home directory so the unprivileged user can read
# them. UV_FROZEN/UV_NO_SYNC make `uv run` use the baked venv as-is instead of
# re-resolving (and needing network access) on every container start.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UV_FROZEN=1 \
    UV_NO_SYNC=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH="/app/.venv/bin:${PATH}"

RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --create-home app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# The Playwright CLI comes from the venv, so the browser revision always
# matches the locked playwright package. --with-deps pulls the system libraries
# Chromium needs; it requires root, hence this runs before the USER switch.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY src ./src

USER app

EXPOSE 8000

# No curl in the slim image; the interpreter is already here. urlopen raises on
# the 503 the endpoint returns when the database is unreachable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"]

CMD ["uv", "run", "uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
