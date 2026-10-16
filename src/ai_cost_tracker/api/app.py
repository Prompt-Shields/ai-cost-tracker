"""FastAPI application. Run with ``uvicorn ai_cost_tracker.api.app:app``."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ai_cost_tracker.api.auth import check_auth_settings
from ai_cost_tracker.api.routes import (
    admin,
    alerts,
    allocation,
    api_keys,
    audit_log,
    azure_connect,
    budgets,
    connections,
    costs,
    exports,
    health,
    inventory,
    invoices,
    me,
    notifications,
    ptu,
)
from ai_cost_tracker.config import Settings, get_settings

API_PREFIX = "/api/v1"


def _cors_origins(settings: Settings) -> list[str]:
    if settings.web_origins:
        return settings.web_origins
    return ["http://localhost:3000"] if settings.environment == "local" else []


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Fail at server start, not on the first request, if auth is unsafe or incomplete.
        check_auth_settings(settings)
        yield

    app = FastAPI(title="AI Cost Tracker", version="0.1.0", lifespan=lifespan)

    origins = _cors_origins(settings)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Azure-Management-Token"],
            # Lets the browser read the CSV export's filename.
            expose_headers=["Content-Disposition"],
        )

    app.include_router(health.router)
    for module in (me, inventory, costs):
        app.include_router(module.router, prefix=API_PREFIX)
    app.include_router(azure_connect.router, prefix=API_PREFIX)
    app.include_router(connections.router, prefix=API_PREFIX)
    app.include_router(admin.router, prefix=API_PREFIX)
    for module in (
        alerts,
        allocation,
        api_keys,
        audit_log,
        budgets,
        exports,
        invoices,
        notifications,
        ptu,
    ):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


app = create_app()
