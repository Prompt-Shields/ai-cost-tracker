"""CloudWatch AWS/Bedrock token metrics -> TokenUsage. Owned by W3."""

from datetime import datetime
from typing import Any

from ai_cost_tracker.core.models import Deployment, TokenUsage


async def pull_usage(
    sessions: Any,
    account_id: str,
    regions: list[str],
    deployments: list[Deployment],
    start: datetime,
    end: datetime,
) -> list[TokenUsage]:
    raise NotImplementedError
