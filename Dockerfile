# syntax=docker/dockerfile:1.7
# One image, two entrypoints (jobs-api, media-jobs-mcp). Multi-stage: build tools never ship.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --from=build --chown=app:app /app/src /app/src
COPY --chown=app:app runbooks ./runbooks
COPY --chown=app:app samples ./samples
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 MJM_MEDIA_ROOT=/app/samples
USER app
EXPOSE 8080 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1
CMD ["jobs-api"]
