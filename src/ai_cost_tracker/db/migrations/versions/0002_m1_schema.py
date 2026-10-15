"""M1 schema: onboarding state, sync health, unmapped meters, estimates, reconciliation.

Revision ID: 0002_m1_schema
Revises: 0001_initial
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_m1_schema"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BigId = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
Money = sa.Numeric(20, 10)


def _id() -> sa.Column:
    return sa.Column("id", BigId, primary_key=True, autoincrement=True)


def _tenant() -> sa.Column:
    return sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False)


def upgrade() -> None:
    with op.batch_alter_table("cloud_account") as batch:
        batch.add_column(
            sa.Column("tracked", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch.add_column(
            sa.Column("access_status", sa.String(32), server_default="connected", nullable=False)
        )
        batch.add_column(sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("ai_deployment") as batch:
        batch.add_column(sa.Column("region", sa.String(64), nullable=True))

    with op.batch_alter_table("price") as batch:
        batch.add_column(sa.Column("ai_model", sa.String(128), nullable=True))
        batch.add_column(sa.Column("ai_direction", sa.String(32), nullable=True))
        batch.add_column(sa.Column("ai_deployment_type", sa.String(32), nullable=True))
        batch.add_column(sa.Column("tokens_per_unit", sa.BigInteger(), nullable=True))

    op.create_table(
        "sync_state",
        _id(),
        _tenant(),
        sa.Column("cloud", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(2000), nullable=True),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backfill_from", sa.Date(), nullable=True),
        sa.Column("backfill_done_through", sa.Date(), nullable=True),
        sa.Column("backfill_progress", sa.Numeric(5, 4), nullable=True),
        sa.UniqueConstraint("tenant_id", "cloud", "account_id", "source"),
    )
    op.create_index("ix_sync_state_tenant_id", "sync_state", ["tenant_id"])

    op.create_table(
        "unmapped_meter",
        _id(),
        _tenant(),
        sa.Column("cloud", sa.String(16), nullable=False),
        sa.Column("sku_meter", sa.String(256), nullable=False),
        sa.Column("first_seen", sa.Date(), nullable=False),
        sa.Column("last_seen", sa.Date(), nullable=False),
        sa.Column("total_cost", Money, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.UniqueConstraint("tenant_id", "cloud", "sku_meter"),
    )
    op.create_index("ix_unmapped_meter_tenant_id", "unmapped_meter", ["tenant_id"])

    op.create_table(
        "daily_estimate",
        _id(),
        _tenant(),
        sa.Column("cloud", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("resource_id", sa.String(512), nullable=False),
        sa.Column("deployment", sa.String(200), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("input_cost", Money, nullable=False),
        sa.Column("output_cost", Money, nullable=False),
        sa.Column("estimated_cost", Money, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("price_missing", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("tenant_id", "cloud", "resource_id", "deployment", "day"),
    )
    op.create_index("ix_daily_estimate_tenant_id", "daily_estimate", ["tenant_id"])
    op.create_index("ix_daily_estimate_day", "daily_estimate", ["day"])

    op.create_table(
        "reconciliation",
        _id(),
        _tenant(),
        sa.Column("cloud", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("estimated_cost", Money, nullable=False),
        sa.Column("billed_cost", Money, nullable=True),
        sa.Column("variance", sa.Numeric(12, 6), nullable=True),
        sa.Column("day_status", sa.String(16), nullable=False),
        sa.UniqueConstraint("tenant_id", "cloud", "account_id", "model", "day"),
    )
    op.create_index("ix_reconciliation_tenant_id", "reconciliation", ["tenant_id"])
    op.create_index("ix_reconciliation_day", "reconciliation", ["day"])

    op.create_table(
        "alert_event",
        _id(),
        _tenant(),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("message", sa.String(2000), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "kind", "subject"),
    )
    op.create_index("ix_alert_event_tenant_id", "alert_event", ["tenant_id"])


def downgrade() -> None:
    for table in (
        "alert_event",
        "reconciliation",
        "daily_estimate",
        "unmapped_meter",
        "sync_state",
    ):
        op.drop_table(table)
    with op.batch_alter_table("price") as batch:
        for col in ("tokens_per_unit", "ai_deployment_type", "ai_direction", "ai_model"):
            batch.drop_column(col)
    with op.batch_alter_table("ai_deployment") as batch:
        batch.drop_column("region")
    with op.batch_alter_table("cloud_account") as batch:
        for col in ("discovered_at", "access_status", "tracked"):
            batch.drop_column(col)
