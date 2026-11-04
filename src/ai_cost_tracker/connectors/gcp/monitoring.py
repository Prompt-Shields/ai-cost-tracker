"""Cloud Monitoring token metrics -> TokenUsage. Owned by W3."""

from datetime import datetime
from typing import Any

from ai_cost_tracker.core.models import Deployment, TokenUsage


async def pull_usage(
    client: Any, project_id: str, deployments: list[Deployment], start: datetime, end: datetime
) -> list[TokenUsage]:
    raise NotImplementedError
