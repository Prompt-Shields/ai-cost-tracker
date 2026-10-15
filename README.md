# AI Cost Tracker

One place to see what your cloud AI actually costs: **billed cost joined with token usage,
per model and per deployment**. It starts with Azure OpenAI / AI Foundry, because Microsoft
Cost Management makes this hard to see, and expands to GCP Vertex AI and AWS Bedrock.

> Status: **skeleton (M0)**. Runs locally end to end on demo data from a fake connector.
> See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the plan.

Internal tool first, run as a standalone SaaS web app that we host.

## What it will answer

- Cost and tokens per deployment and model, today (estimated) and historically (billed)
- Effective cost per 1M input / output tokens, and how it drifts over time
- Spend by team, app and cost centre (tags and deployment naming; gateway attribution later)
- PTU utilisation and idle provisioned capacity
- Budget alerts and overnight spikes, sent to email, Slack or Teams

## Roadmap

Tracked as [GitHub milestones](https://github.com/Prompt-Shields/ai-cost-tracker/milestones).

0. Foundations: Entra ID login, RBAC, tenant-scoped schema, fake connector
1. Azure MVP (internal): one-click connect (one Reader role, no setup in Azure), token metrics, cost, per-deployment view
2. Budgets, alerts, PTU reports, tag / deployment-based allocation
2b. Open to external users: signup, connection wizard, Google login, SAML
3. GCP (Vertex AI)
4. AWS (Bedrock)

## Quickstart

With Docker (Postgres + TimescaleDB, Redis, API, worker, web):

```bash
docker compose up -d --build
docker compose exec api act seed-demo   # migrate + 30 days of demo data
```

Open http://localhost:3000. The API is on http://localhost:8000 (`/healthz`, `/docs`).
Locally, sign-in is bypassed (`ACT_AUTH_DISABLED=true`) and you act as a dev user with the
owner role. Set `ACT_DB_PORT` if port 5432 is taken.

Without Docker you need Python 3.12, [uv](https://docs.astral.sh/uv/), Node 20+, and a
Postgres and Redis you can reach (e.g. `docker compose up -d db redis`):

```bash
cp .env.example .env
uv sync
uv run act migrate
uv run act seed-demo
uv run uvicorn ai_cost_tracker.api.app:app --reload
uv run arq ai_cost_tracker.worker.settings.WorkerSettings   # optional, scheduled ingestion
cd web && npm ci && npm run dev
```

Tests and lint: `uv run pytest`, `uv run ruff check . && uv run ruff format --check .`.

## Project layout

```
src/ai_cost_tracker/
├── core/         domain models (FOCUS-style cost records, token usage)
├── connectors/   Connector protocol, fake connector, Azure connector
├── normalise/    meter parsing (Azure meter name -> model, direction)
├── db/           SQLAlchemy models, session, Alembic migrations
├── api/          FastAPI app, Entra ID auth, /api/v1 routes
├── worker/       arq jobs and schedules (inventory, usage, cost)
├── ingest.py     idempotent ingestion used by the worker and CLI
└── cli.py        `act` command: migrate, seed-demo, ingest-once
web/              Next.js frontend
docs/             architecture and setup guides
tests/            pytest suite
```

## Connecting Azure

The app reads cost, token metrics and inventory with one built-in `Reader` role on each
subscription, granted to its managed identity; no storage account, export or client
secret. See [ARCHITECTURE.md §4.3](docs/ARCHITECTURE.md#43-connecting-azure-one-click-one-role)
for the design and [docs/entra-setup.md](docs/entra-setup.md) for the app registration and
sign-in setup. To try it locally, `az login`, then set `ACT_CONNECTOR=azure` and optionally
`ACT_AZURE_SUBSCRIPTION_IDS`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
