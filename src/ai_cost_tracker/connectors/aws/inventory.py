"""Accounts and Bedrock deployments. Owned by W9."""

from typing import Any

from ai_cost_tracker.core.models import CloudAccount, Deployment


async def list_accounts(
    sessions: Any,
    *,
    include_all: bool,
    only: set[str],
    export_account_id: str | None,
    regions: list[str],
) -> list[CloudAccount]:
    raise NotImplementedError


async def check_access(sessions: Any, account_id: str) -> bool:
    raise NotImplementedError


async def list_deployments(sessions: Any, account_id: str, regions: list[str]) -> list[Deployment]:
    raise NotImplementedError
