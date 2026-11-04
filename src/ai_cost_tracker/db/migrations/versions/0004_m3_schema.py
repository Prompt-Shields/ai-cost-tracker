"""M3 schema: daily FX reference rates for the multi-cloud reporting currency.

Revision ID: 0004_m3_schema
Revises: 0003_m2_schema
Create Date: 2026-11-04 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_m3_schema"
down_revision: str | None = "0003_m2_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fx_rate",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("base", sa.String(length=3), nullable=False),
        sa.Column("quote", sa.String(length=3), nullable=False),
        sa.Column("rate", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day", "base", "quote"),
    )
    op.create_index(op.f("ix_fx_rate_day"), "fx_rate", ["day"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_fx_rate_day"), table_name="fx_rate")
    op.drop_table("fx_rate")
