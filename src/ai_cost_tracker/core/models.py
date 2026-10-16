"""Cloud-neutral domain records that every connector emits.

Cost fields follow FOCUS 1.x column names in snake_case (``BilledCost`` → ``billed_cost``)
so GCP and AWS can map onto the same shape later.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Cloud = Literal["azure", "gcp", "aws"]


class CloudAccount(BaseModel):
    """An Azure subscription, GCP project or AWS account."""

    cloud: Cloud
    account_id: str
    name: str
    has_ai_resources: bool = False


class Deployment(BaseModel):
    """A model deployment: the unit we attribute cost and tokens to."""

    cloud: Cloud
    account_id: str
    resource_id: str
    name: str
    model: str
    model_version: str | None = None
    sku: str | None = None
    capacity: int | None = None
    region: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class CostRecord(BaseModel):
    """One day of billed cost for one resource and meter (subset of FOCUS)."""

    cloud: Cloud
    sub_account_id: str
    resource_id: str | None
    charge_period_start: date
    service_name: str | None = None
    sku_meter: str
    consumed_quantity: Decimal | None = None
    billed_cost: Decimal
    billing_currency: str


class TokenUsage(BaseModel):
    """Token counts for one deployment over one time bucket."""

    cloud: Cloud
    account_id: str
    resource_id: str
    deployment: str
    model: str | None = None
    bucket_start: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    source: Literal["metrics", "gateway"] = "metrics"
