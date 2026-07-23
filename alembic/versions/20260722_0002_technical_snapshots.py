"""增加持仓技术状态快照。

Revision ID: 20260722_0002
Revises: 20260722_0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260722_0002"
down_revision = "20260722_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if inspect(op.get_bind()).has_table("technical_snapshots"):
        return
    op.create_table(
        "technical_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("holding_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=12), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("data_source", sa.String(length=50), nullable=False),
        sa.Column("indicators", sa.JSON(), nullable=False),
        sa.Column("signals", sa.JSON(), nullable=False),
        sa.Column("support_levels", sa.JSON(), nullable=False),
        sa.Column("resistance_levels", sa.JSON(), nullable=False),
        sa.Column("conflicts", sa.JSON(), nullable=False),
        sa.Column("conclusion", sa.Text(), nullable=False),
        sa.Column("risk_notice", sa.Text(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["holding_id"], ["holdings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("holding_id", "trade_date", name="uq_technical_holding_date"),
    )
    op.create_index(
        op.f("ix_technical_snapshots_holding_id"),
        "technical_snapshots",
        ["holding_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_technical_snapshots_symbol"), "technical_snapshots", ["symbol"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_technical_snapshots_symbol"), table_name="technical_snapshots")
    op.drop_index(op.f("ix_technical_snapshots_holding_id"), table_name="technical_snapshots")
    op.drop_table("technical_snapshots")
