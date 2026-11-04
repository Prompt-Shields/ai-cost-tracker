"""Projects and Vertex AI deployments. Owned by W3."""

from typing import Any

from ai_cost_tracker.core.models import CloudAccount, Deployment


async def list_projects(client: Any, *, include_all: bool, only: set[str]) -> list[CloudAccount]:
    raise NotImplementedError


async def check_access(client: Any, project_id: str) -> bool:
    raise NotImplementedError


async def list_deployments(client: Any, project_id: str, regions: list[str]) -> list[Deployment]:
    raise NotImplementedError
