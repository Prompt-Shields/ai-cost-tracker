# CLAUDE.md

Guide for coding agents working in this repo. The design lives in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); read the relevant section before changing
behaviour.

## Commands

```bash
uv sync                                            # install (Python 3.12, uv-managed)
uv run pytest                                      # tests (asyncio_mode=auto, SQLite in-memory)
uv run ruff check . && uv run ruff format --check .  # lint + format (line length 100)
uv run act migrate | seed-demo | ingest-once       # CLI
docker compose up -d --build                       # full local stack
cd web && npm run typecheck && npm run build       # frontend checks
```

CI runs the same checks; run them before you finish.

## Architecture pointers

- `src/ai_cost_tracker/config.py`: all settings, `ACT_*` env vars. Document new ones in
  `.env.example`.
- `core/models.py`: pydantic domain models (`CostRecord`, `TokenUsage`, ...).
- `connectors/base.py`: the `Connector` protocol. `fake.py` for dev/tests, `azure/` for real.
- `normalise/azure_meters.py`: meter name -> model / direction.
- `ingest.py`: writes connector output to the DB; `worker/` schedules it, `cli.py` runs it once.
- `db/models.py` + `db/migrations/`: schema. Every schema change needs an Alembic revision.
- `api/`: FastAPI app, `auth.py` (Entra ID, roles), `routes/`.
- `web/`: Next.js frontend talking to `/api/v1`.

## Conventions

- Money is `Decimal`, never float; serialise it as a string in the API.
- Every table and query is scoped by `tenant_id`. No query without a tenant filter.
- Cloud integrations implement the `Connector` protocol; nothing outside `connectors/`
  imports a cloud SDK.
- Ingestion is idempotent per partition (account x date range): replace, don't append.
  Running a job twice must leave identical rows.
- No secrets: Azure access is via managed identity (or `az login` locally) through
  `azure-identity`. Never add client secrets, keys or connection strings to code or config.
- Small modules, type hints, short docstrings that say why.
- Keep docs/ARCHITECTURE.md in sync with design changes, in the same PR.
