"""Initial schema: every table in db/models.py, token_usage as a TimescaleDB hypertable.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BigId = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
Money = sa.Numeric(20, 10)


def _timescale_available() -> bool:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    found = bind.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
    ).first()
    return found is not None


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "cloud_account",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("cloud", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("has_ai_resources", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "cloud", "account_id"),
    )
    op.create_index("ix_cloud_account_tenant_id", "cloud_account", ["tenant_id"])

    op.create_table(
        "ai_deployment",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("cloud", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("resource_id", sa.String(512), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=True),
        sa.Column("sku", sa.String(64), nullable=True),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "cloud", "resource_id", "name"),
    )
    op.create_index("ix_ai_deployment_tenant_id", "ai_deployment", ["tenant_id"])
    op.create_index("ix_ai_deployment_account_id", "ai_deployment", ["account_id"])

    op.create_table(
        "cost_line",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("cloud", sa.String(16), nullable=False),
        sa.Column("sub_account_id", sa.String(128), nullable=False),
        sa.Column("resource_id", sa.String(512), nullable=False),
        sa.Column("charge_period_start", sa.Date(), nullable=False),
        sa.Column("service_name", sa.String(128), nullable=True),
        sa.Column("sku_meter", sa.String(256), nullable=False),
        sa.Column("consumed_quantity", Money, nullable=True),
        sa.Column("billed_cost", Money, nullable=False),
        sa.Column("billing_currency", sa.String(3), nullable=False),
        sa.Column("ai_model", sa.String(128), nullable=True),
        sa.Column("ai_direction", sa.String(32), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "cloud",
            "sub_account_id",
            "resource_id",
            "sku_meter",
            "charge_period_start",
        ),
    )
    op.create_index("ix_cost_line_tenant_id", "cost_line", ["tenant_id"])
    op.create_index("ix_cost_line_charge_period_start", "cost_line", ["charge_period_start"])

    op.create_table(
        "token_usage",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("cloud", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("resource_id", sa.String(512), nullable=False),
        sa.Column("deployment", sa.String(200), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("requests", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "cloud", "resource_id", "deployment", "bucket_start", "source"
        ),
    )
    op.create_index("ix_token_usage_tenant_id", "token_usage", ["tenant_id"])
    op.create_index("ix_token_usage_bucket_start", "token_usage", ["bucket_start"])

    op.create_table(
        "price",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("cloud", sa.String(16), nullable=False),
        sa.Column("sku_meter", sa.String(256), nullable=False),
        sa.Column("region", sa.String(64), nullable=False),
        sa.Column("unit", sa.String(64), nullable=False),
        sa.Column("unit_price", Money, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.UniqueConstraint("cloud", "sku_meter", "region", "valid_from"),
    )

    if _timescale_available():
        _make_token_usage_hypertable()


def _make_token_usage_hypertable() -> None:
    """Convert token_usage to a hypertable partitioned on bucket_start.

    TimescaleDB requires every unique index, the primary key included, to contain the
    partitioning column. The idempotency unique constraint already does; the surrogate
    ``id`` PK does not, so here (and only here) it becomes ``(id, bucket_start)``. ``id``
    stays unique in practice because it is still drawn from its own sequence. The model
    keeps the plain ``id`` PK so SQLite tests and non-Timescale Postgres are unchanged.
    """
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.drop_constraint("token_usage_pkey", "token_usage", type_="primary")
    op.create_primary_key("token_usage_pkey", "token_usage", ["id", "bucket_start"])
    # ix_token_usage_bucket_start already covers the time column; skip Timescale's default.
    op.execute(
        "SELECT create_hypertable('token_usage', by_range('bucket_start'), "
        "create_default_indexes => FALSE)"
    )


def downgrade() -> None:
    # Dropping a hypertable drops its chunks too. The timescaledb extension is left in place:
    # it is database-wide and other objects may depend on it.
    op.drop_table("price")
    op.drop_index("ix_token_usage_bucket_start", table_name="token_usage")
    op.drop_index("ix_token_usage_tenant_id", table_name="token_usage")
    op.drop_table("token_usage")
    op.drop_index("ix_cost_line_charge_period_start", table_name="cost_line")
    op.drop_index("ix_cost_line_tenant_id", table_name="cost_line")
    op.drop_table("cost_line")
    op.drop_index("ix_ai_deployment_account_id", table_name="ai_deployment")
    op.drop_index("ix_ai_deployment_tenant_id", table_name="ai_deployment")
    op.drop_table("ai_deployment")
    op.drop_index("ix_cloud_account_tenant_id", table_name="cloud_account")
    op.drop_table("cloud_account")
    op.drop_table("tenant")
