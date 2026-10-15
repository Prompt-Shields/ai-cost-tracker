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
