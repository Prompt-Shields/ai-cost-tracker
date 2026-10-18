"""SQLAlchemy models (ARCHITECTURE.md §6).

Every table carries ``tenant_id`` (D7). Money is ``Numeric(20, 10)``, never float. Unique
constraints make ingestion idempotent: a re-run replaces the same rows.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Money = Numeric(20, 10)


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CloudAccountRow(Base):
    __tablename__ = "cloud_account"
    __table_args__ = (UniqueConstraint("tenant_id", "cloud", "account_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    cloud: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(200))
    has_ai_resources: Mapped[bool] = mapped_column(default=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Whether an admin chose to track this account (ARCHITECTURE.md §4.3, step 2).
    tracked: Mapped[bool] = mapped_column(default=True, server_default="true")
    # connected | waiting_for_access | no_access. "waiting_for_access" means the Reader role
    # was requested but not granted yet; the worker re-checks and connects it automatically.
    access_status: Mapped[str] = mapped_column(
        String(32), default="connected", server_default="connected"
    )
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Day of month (1-28) the pay-as-you-go invoice period starts; null = calendar month.
    billing_period_day: Mapped[int | None] = mapped_column(Integer)


class DeploymentRow(Base):
    __tablename__ = "ai_deployment"
    __table_args__ = (UniqueConstraint("tenant_id", "cloud", "resource_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    cloud: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    resource_id: Mapped[str] = mapped_column(String(512))
    name: Mapped[str] = mapped_column(String(200))
    model: Mapped[str] = mapped_column(String(128))
    model_version: Mapped[str | None] = mapped_column(String(64))
    sku: Mapped[str | None] = mapped_column(String(64))
    capacity: Mapped[int | None] = mapped_column(Integer)
    # Azure region of the owning account, e.g. "eastus2"; prices are per region.
    region: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CostLine(Base):
    """One day of billed cost per resource and meter. FOCUS-style column names."""

    __tablename__ = "cost_line"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "cloud",
            "sub_account_id",
            "resource_id",
            "sku_meter",
            "charge_period_start",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    cloud: Mapped[str] = mapped_column(String(16))
    sub_account_id: Mapped[str] = mapped_column(String(128))
    resource_id: Mapped[str] = mapped_column(String(512), default="")
    charge_period_start: Mapped[date] = mapped_column(Date, index=True)
    service_name: Mapped[str | None] = mapped_column(String(128))
    sku_meter: Mapped[str] = mapped_column(String(256))
    consumed_quantity: Mapped[Decimal | None] = mapped_column(Money)
    billed_cost: Mapped[Decimal] = mapped_column(Money)
    billing_currency: Mapped[str] = mapped_column(String(3))
    # Parsed from sku_meter by ai_cost_tracker.normalise; null when the meter is unmapped.
    ai_model: Mapped[str | None] = mapped_column(String(128))
    ai_direction: Mapped[str | None] = mapped_column(String(32))


class TokenUsageRow(Base):
    """Token counts per deployment per hour. A TimescaleDB hypertable on Postgres."""

    __tablename__ = "token_usage"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "cloud", "resource_id", "deployment", "bucket_start", "source"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    cloud: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str] = mapped_column(String(128))
    resource_id: Mapped[str] = mapped_column(String(512))
    deployment: Mapped[str] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(128))
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    requests: Mapped[int] = mapped_column(BigInteger, default=0)
    source: Mapped[str] = mapped_column(String(16), default="metrics")


class Price(Base):
    """Unit price per meter; history kept via valid_from so old estimates stay reproducible."""

    __tablename__ = "price"
    __table_args__ = (UniqueConstraint("cloud", "sku_meter", "region", "valid_from"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    cloud: Mapped[str] = mapped_column(String(16))
    sku_meter: Mapped[str] = mapped_column(String(256))
    region: Mapped[str] = mapped_column(String(64), default="")
    unit: Mapped[str] = mapped_column(String(64))
    unit_price: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    valid_from: Mapped[date] = mapped_column(Date)
    # Parsed from sku_meter with normalise.azure_meters; null when unmapped.
    ai_model: Mapped[str | None] = mapped_column(String(128))
    ai_direction: Mapped[str | None] = mapped_column(String(32))
    ai_deployment_type: Mapped[str | None] = mapped_column(String(32))
    # How many tokens one ``unit`` covers (1000 for "1K", 1_000_000 for "1M").
    tokens_per_unit: Mapped[int | None] = mapped_column(BigInteger)


class SyncState(Base):
    """Per account and source: last success, last error, watermark and backfill progress.

    Drives the connection health panel (§4.3) and the backfill progress bar.
    """

    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("tenant_id", "cloud", "account_id", "source"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    cloud: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str] = mapped_column(String(128))
    # inventory | cost | usage | access
    source: Mapped[str] = mapped_column(String(32))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(2000))
    # Newest point successfully ingested (usage bucket or cost day).
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Backfill: oldest day requested, oldest day done, and 0..1 progress.
    backfill_from: Mapped[date | None] = mapped_column(Date)
    backfill_done_through: Mapped[date | None] = mapped_column(Date)
    backfill_progress: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))


class UnmappedMeter(Base):
    """Billing meters the parser didn't recognise, surfaced to admins instead of dropped."""

    __tablename__ = "unmapped_meter"
    __table_args__ = (UniqueConstraint("tenant_id", "cloud", "sku_meter"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    cloud: Mapped[str] = mapped_column(String(16))
    sku_meter: Mapped[str] = mapped_column(String(256))
    first_seen: Mapped[date] = mapped_column(Date)
    last_seen: Mapped[date] = mapped_column(Date)
    total_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3))


class DailyEstimate(Base):
    """Estimated cost per deployment and day: tokens x retail price (D6)."""

    __tablename__ = "daily_estimate"
    __table_args__ = (UniqueConstraint("tenant_id", "cloud", "resource_id", "deployment", "day"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    cloud: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str] = mapped_column(String(128))
    resource_id: Mapped[str] = mapped_column(String(512))
    deployment: Mapped[str] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(128))
    day: Mapped[date] = mapped_column(Date, index=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    input_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    output_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    estimated_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # True when no price was found for the model/direction/region; cost is then incomplete.
    price_missing: Mapped[bool] = mapped_column(default=False)


class Reconciliation(Base):
    """Estimated vs billed cost per account, model and day (§13 M1, issue #21)."""

    __tablename__ = "reconciliation"
    __table_args__ = (UniqueConstraint("tenant_id", "cloud", "account_id", "model", "day"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    cloud: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(128))
    day: Mapped[date] = mapped_column(Date, index=True)
    estimated_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    billed_cost: Mapped[Decimal | None] = mapped_column(Money)
    # (estimated - billed) / billed; null until billed cost exists or when billed is zero.
    variance: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    # estimated | billed | finalised
    day_status: Mapped[str] = mapped_column(String(16), default="estimated")


class AlertEvent(Base):
    """Something an admin should look at (variance, lost access, unmapped meters)."""

    __tablename__ = "alert_event"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "subject"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    # e.g. "variance", "access_lost", "unmapped_meter", "new_subscription"
    kind: Mapped[str] = mapped_column(String(32))
    # Stable key so repeats update one row, e.g. "<account>/<model>/<day>".
    subject: Mapped[str] = mapped_column(String(512))
    message: Mapped[str] = mapped_column(String(2000))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # info | warning | critical
    severity: Mapped[str] = mapped_column(String(16), default="warning", server_default="warning")
    # Set once the notifier has handled this event (sent, or nothing to send it to).
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Budget(Base):
    """A monthly spend budget on a scope (§13 M2, issue #29)."""

    __tablename__ = "budget"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # all | account | model | deployment | tag   (tag scope_value is "key:value")
    scope_type: Mapped[str] = mapped_column(String(16))
    scope_value: Mapped[str | None] = mapped_column(String(512))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # Fractions of amount that raise an alert, e.g. [0.8, 1.0].
    thresholds: Mapped[list] = mapped_column(JSON, default=lambda: [0.8, 1.0])
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NotificationChannel(Base):
    """Where alerts go: email, Slack, Teams or a webhook (issue #30).

    ``target`` is an email address or a webhook URL. Webhook URLs embed a token, so the API
    never returns them in full.
    """

    __tablename__ = "notification_channel"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # email | slack | teams | webhook
    kind: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(String(2000))
    # HMAC secret for generic webhooks (signature header); null for other kinds.
    signing_secret: Mapped[str | None] = mapped_column(String(200))
    # Alert kinds routed here; empty list = all kinds.
    alert_kinds: Mapped[list] = mapped_column(JSON, default=list)
    min_severity: Mapped[str] = mapped_column(String(16), default="warning")
    # Send a daily digest instead of one message per alert.
    digest: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NotificationDelivery(Base):
    """One attempt to send one alert (or digest) to one channel."""

    __tablename__ = "notification_delivery"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_channel.id"))
    # Null for a digest delivery.
    alert_event_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("alert_event.id")
    )
    # sent | failed
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(String(2000))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PtuUtilization(Base):
    """Hourly utilisation (0..1) of a provisioned (PTU) deployment (issue #32)."""

    __tablename__ = "ptu_utilization"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cloud", "resource_id", "deployment", "bucket_start"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    cloud: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str] = mapped_column(String(128))
    resource_id: Mapped[str] = mapped_column(String(512))
    deployment: Mapped[str] = mapped_column(String(200))
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    utilization: Mapped[Decimal] = mapped_column(Numeric(7, 4))


class AllocationRule(Base):
    """Maps deployments to a cost centre / team / app by tag, name pattern or account (#33).

    Rules are evaluated by ascending priority; the first match wins.
    """

    __tablename__ = "allocation_rule"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    # tag | name_pattern | account
    match_type: Mapped[str] = mapped_column(String(16))
    # Tag key for "tag"; unused otherwise.
    match_key: Mapped[str | None] = mapped_column(String(200))
    # Tag value, glob pattern on "<account resource>/<deployment>", or account id.
    match_value: Mapped[str] = mapped_column(String(512))
    cost_centre: Mapped[str | None] = mapped_column(String(200))
    team: Mapped[str | None] = mapped_column(String(200))
    app: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=True)


class SplitRule(Base):
    """Splits a shared deployment's cost by fixed percentages (#34).

    ``allocations`` is ``[{"cost_centre": str | None, "team": str | None, "app": str | None,
    "percent": number}]`` summing to 100.
    """

    __tablename__ = "split_rule"
    __table_args__ = (UniqueConstraint("tenant_id", "resource_id", "deployment"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    resource_id: Mapped[str] = mapped_column(String(512))
    deployment: Mapped[str] = mapped_column(String(200))
    allocations: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(default=True)


class ApiKey(Base):
    """Programmatic access (#35). Only a hash of the key is stored."""

    __tablename__ = "api_key"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # First characters of the key, shown in the UI to tell keys apart.
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    # App roles the key acts with, e.g. ["viewer"]; never "owner".
    roles: Mapped[list] = mapped_column(JSON, default=lambda: ["viewer"])
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Who changed what (#37). Written through ``ai_cost_tracker.audit.record``."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor_id: Mapped[str] = mapped_column(String(200))
    actor_name: Mapped[str | None] = mapped_column(String(200))
    # e.g. "budget.create", "connection.track", "api_key.revoke", "export.csv", "login"
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(512))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
