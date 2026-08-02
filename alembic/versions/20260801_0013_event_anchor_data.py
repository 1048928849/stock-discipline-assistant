"""Add minute bars and institutional transaction evidence.

Revision ID: 20260801_0013
Revises: 20260801_0012
"""

import sqlalchemy as sa

from alembic import op

revision = "20260801_0013"
down_revision = "20260801_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "market_minute_bars" not in tables:
        op.create_table(
            "market_minute_bars",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("trade_time", sa.DateTime(), nullable=False),
            sa.Column("open", sa.Numeric(18, 4), nullable=False),
            sa.Column("high", sa.Numeric(18, 4), nullable=False),
            sa.Column("low", sa.Numeric(18, 4), nullable=False),
            sa.Column("close", sa.Numeric(18, 4), nullable=False),
            sa.Column("volume", sa.Numeric(24, 4), nullable=False),
            sa.Column("amount", sa.Numeric(24, 4)),
            sa.Column("source", sa.String(50), nullable=False),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("symbol", "trade_time", "source", name="uq_minute_bar_source"),
        )
        op.create_index("ix_market_minute_bars_symbol", "market_minute_bars", ["symbol"])
        op.create_index("ix_market_minute_bars_trade_time", "market_minute_bars", ["trade_time"])
    if "institutional_transaction_evidence" not in tables:
        op.create_table(
            "institutional_transaction_evidence",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("evidence_key", sa.String(100), nullable=False),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("event_date", sa.Date(), nullable=False),
            sa.Column("evidence_type", sa.String(30), nullable=False),
            sa.Column("price", sa.Numeric(18, 4)),
            sa.Column("institutional", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("source", sa.String(100), nullable=False),
            sa.Column("source_url", sa.String(1000)),
            sa.Column("raw_data", sa.JSON()),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("evidence_key", name="uq_institutional_evidence_key"),
        )
        for column in ("symbol", "event_date", "evidence_type"):
            op.create_index(
                f"ix_institutional_transaction_evidence_{column}",
                "institutional_transaction_evidence",
                [column],
            )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "institutional_transaction_evidence" in tables:
        op.drop_table("institutional_transaction_evidence")
    if "market_minute_bars" in tables:
        op.drop_table("market_minute_bars")
