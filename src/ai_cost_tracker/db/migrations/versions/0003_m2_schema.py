"""M2 schema: budgets, notifications, PTU utilisation, allocation and split rules, API keys,
audit log; alert severity and notification state; invoice period day.

Revision ID: 0003_m2_schema
Revises: 0002_m1_schema
Create Date: 2026-10-16 11:18:49.124745
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_m2_schema"
down_revision: str | None = "0002_m1_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "allocation_rule",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("match_type", sa.String(length=16), nullable=False),
        sa.Column("match_key", sa.String(length=200), nullable=True),
        sa.Column("match_value", sa.String(length=512), nullable=False),
        sa.Column("cost_centre", sa.String(length=200), nullable=True),
        sa.Column("team", sa.String(length=200), nullable=True),
        sa.Column("app", sa.String(length=200), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_allocation_rule_tenant_id"), "allocation_rule", ["tenant_id"], unique=False
    )
    op.create_table(
        "api_key",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index(op.f("ix_api_key_prefix"), "api_key", ["prefix"], unique=False)
    op.create_index(op.f("ix_api_key_tenant_id"), "api_key", ["tenant_id"], unique=False)
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("actor_name", sa.String(length=200), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=512), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_action"), "audit_log", ["action"], unique=False)
    op.create_index(op.f("ix_audit_log_at"), "audit_log", ["at"], unique=False)
    op.create_index(op.f("ix_audit_log_tenant_id"), "audit_log", ["tenant_id"], unique=False)
    op.create_table(
        "budget",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_value", sa.String(length=512), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("thresholds", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_budget_tenant_id"), "budget", ["tenant_id"], unique=False)
    op.create_table(
        "notification_channel",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=2000), nullable=False),
        sa.Column("signing_secret", sa.String(length=200), nullable=True),
        sa.Column("alert_kinds", sa.JSON(), nullable=False),
        sa.Column("min_severity", sa.String(length=16), nullable=False),
        sa.Column("digest", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notification_channel_tenant_id"),
        "notification_channel",
        ["tenant_id"],
        unique=False,
    )
    op.create_table(
        "ptu_utilization",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("cloud", sa.String(length=16), nullable=False),
        sa.Column("account_id", sa.String(length=128), nullable=False),
        sa.Column("resource_id", sa.String(length=512), nullable=False),
        sa.Column("deployment", sa.String(length=200), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("utilization", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "cloud", "resource_id", "deployment", "bucket_start"),
    )
    op.create_index(
        op.f("ix_ptu_utilization_bucket_start"), "ptu_utilization", ["bucket_start"], unique=False
    )
    op.create_index(
        op.f("ix_ptu_utilization_tenant_id"), "ptu_utilization", ["tenant_id"], unique=False
    )
    op.create_table(
        "split_rule",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.String(length=512), nullable=False),
        sa.Column("deployment", sa.String(length=200), nullable=False),
        sa.Column("allocations", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "resource_id", "deployment"),
    )
    op.create_index(op.f("ix_split_rule_tenant_id"), "split_rule", ["tenant_id"], unique=False)
    op.create_table(
        "notification_delivery",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column(
            "alert_event_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=2000), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["alert_event_id"],
            ["alert_event.id"],
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channel.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notification_delivery_tenant_id"),
        "notification_delivery",
        ["tenant_id"],
        unique=False,
    )
    op.add_column(
        "alert_event",
        sa.Column("severity", sa.String(length=16), server_default="warning", nullable=False),
    )
    op.add_column(
        "alert_event", sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("cloud_account", sa.Column("billing_period_day", sa.Integer(), nullable=True))
    # Alerts raised before notifications existed are treated as already handled, so the
    # first notifier run doesn't send the whole backlog.
    op.execute(sa.text("UPDATE alert_event SET notified_at = created_at WHERE notified_at IS NULL"))


def downgrade() -> None:
    with op.batch_alter_table("cloud_account") as batch:
        batch.drop_column("billing_period_day")
    with op.batch_alter_table("alert_event") as batch:
        batch.drop_column("notified_at")
        batch.drop_column("severity")
    op.drop_index(op.f("ix_notification_delivery_tenant_id"), table_name="notification_delivery")
    op.drop_table("notification_delivery")
    op.drop_index(op.f("ix_split_rule_tenant_id"), table_name="split_rule")
    op.drop_table("split_rule")
    op.drop_index(op.f("ix_ptu_utilization_tenant_id"), table_name="ptu_utilization")
    op.drop_index(op.f("ix_ptu_utilization_bucket_start"), table_name="ptu_utilization")
    op.drop_table("ptu_utilization")
    op.drop_index(op.f("ix_notification_channel_tenant_id"), table_name="notification_channel")
    op.drop_table("notification_channel")
    op.drop_index(op.f("ix_budget_tenant_id"), table_name="budget")
    op.drop_table("budget")
    op.drop_index(op.f("ix_audit_log_tenant_id"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_at"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_action"), table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index(op.f("ix_api_key_tenant_id"), table_name="api_key")
    op.drop_index(op.f("ix_api_key_prefix"), table_name="api_key")
    op.drop_table("api_key")
    op.drop_index(op.f("ix_allocation_rule_tenant_id"), table_name="allocation_rule")
    op.drop_table("allocation_rule")
