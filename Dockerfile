# One image for the API and the arq worker; the worker overrides CMD in docker-compose.yml.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md alembic.ini* ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --no-create-home --home-dir /app app && chown -R app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "ai_cost_tracker.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
