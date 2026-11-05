"""CUR 2.0 Data Export (S3, CSV gzip) -> CostRecord. Owned by W2."""

from datetime import date
from typing import Any

from ai_cost_tracker.connectors.aws.ids import ExportLocation
from ai_cost_tracker.core.models import CostRecord


async def pull_cost(
    sessions: Any, export: ExportLocation, account_id: str, start: date, end: date
) -> list[CostRecord]:
    raise NotImplementedError
