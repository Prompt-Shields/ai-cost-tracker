# Contributing

1. Pick or open an issue. Larger changes: agree the approach on the issue first, and update
   [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) if the design changes.
2. Branch from `main` (e.g. `feat/azure-cost-backfill`, `fix/meter-parsing`).
3. Make the change with tests. Before pushing:
   ```bash
   uv run pytest
   uv run ruff check . && uv run ruff format --check .
   cd web && npm run typecheck && npm run build   # if you touched web/
   ```
4. Open a pull request against `main`. Link the issue (`Closes #123`), say what changed and
   how you tested it.
5. CI must pass and one review is required before merge. Squash-merge.

Conventions (Decimal money, tenant scoping, idempotent ingestion, no secrets) are listed in
[CLAUDE.md](CLAUDE.md).
