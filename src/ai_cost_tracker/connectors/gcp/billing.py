"""BigQuery billing export -> CostRecord. Owned by W2."""

from datetime import date
from typing import Any

from ai_cost_tracker.core.models import CostRecord


async def pull_cost(
    client: Any, *, table: str, location: str, project_id: str, start: date, end: date
) -> list[CostRecord]:
    raise NotImplementedError
