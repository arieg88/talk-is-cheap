# One image for the API (the default command) and every CLI job:
#   docker compose run --rm api talk-is-cheap ingest news

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini universe.yaml feeds.yaml archives.yaml ./
COPY alembic ./alembic
# Search models, SEC responses, news and LLM responses are cached here (a volume in
# docker-compose.yml).
RUN mkdir -p /app/.cache && chown app:app /app/.cache
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=4)"]
CMD ["talk-is-cheap", "serve", "--host", "0.0.0.0", "--port", "8080"]
