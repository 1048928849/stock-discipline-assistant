"""Standardize market semantics and persist plan execution eligibility.

Revision ID: 20260723_0009
Revises: 20260723_0008
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0009"
down_revision = "20260723_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    quote_columns = {item["name"] for item in inspector.get_columns("market_quotes")}
    quote_additions = (
        sa.Column("previous_close", sa.Numeric(18, 4)),
        sa.Column("trading_date", sa.Date()),
        sa.Column("quote_time", sa.DateTime()),
        sa.Column(
            "market_status", sa.String(30), nullable=False, server_default="unknown"
        ),
        sa.Column(
            "price_type",
            sa.String(30),
            nullable=False,
            server_default="intraday_snapshot",
        ),
        sa.Column("provider_id", sa.String(80)),
        sa.Column("data_as_of", sa.DateTime()),
    )
    for column in quote_additions:
        if column.name not in quote_columns:
            op.add_column("market_quotes", column)
    quote_indexes = {item["name"] for item in inspector.get_indexes("market_quotes")}
    if "ix_market_quotes_trading_date" not in quote_indexes:
        op.create_index("ix_market_quotes_trading_date", "market_quotes", ["trading_date"])

    bar_columns = {item["name"] for item in inspector.get_columns("market_daily_bars")}
    bar_additions = (
        sa.Column("frequency", sa.String(20), nullable=False, server_default="daily"),
        sa.Column(
            "adjustment", sa.String(20), nullable=False, server_default="unknown"
        ),
        sa.Column(
            "price_type", sa.String(30), nullable=False, server_default="official_close"
        ),
        sa.Column("provider_id", sa.String(80)),
        sa.Column("data_as_of", sa.DateTime()),
    )
    for column in bar_additions:
        if column.name not in bar_columns:
            op.add_column("market_daily_bars", column)
    op.execute(
        "UPDATE market_daily_bars SET adjustment = 'qfq' "
        "WHERE source LIKE '%qfq%'"
    )

    plan_columns = {item["name"] for item in inspector.get_columns("trade_plans")}
    plan_additions = (
        sa.Column("plan_kind", sa.String(30), nullable=False, server_default="watch"),
        sa.Column(
            "can_execute", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("execution_blocked_reasons", sa.JSON()),
    )
    for column in plan_additions:
        if column.name not in plan_columns:
            op.add_column("trade_plans", column)
    plan_indexes = {item["name"] for item in inspector.get_indexes("trade_plans")}
    if "ix_trade_plans_plan_kind" not in plan_indexes:
        op.create_index("ix_trade_plans_plan_kind", "trade_plans", ["plan_kind"])


def downgrade() -> None:
    op.drop_index("ix_trade_plans_plan_kind", table_name="trade_plans")
    op.drop_column("trade_plans", "execution_blocked_reasons")
    op.drop_column("trade_plans", "can_execute")
    op.drop_column("trade_plans", "plan_kind")

    op.drop_column("market_daily_bars", "data_as_of")
    op.drop_column("market_daily_bars", "provider_id")
    op.drop_column("market_daily_bars", "price_type")
    op.drop_column("market_daily_bars", "adjustment")
    op.drop_column("market_daily_bars", "frequency")

    op.drop_index("ix_market_quotes_trading_date", table_name="market_quotes")
    op.drop_column("market_quotes", "data_as_of")
    op.drop_column("market_quotes", "provider_id")
    op.drop_column("market_quotes", "price_type")
    op.drop_column("market_quotes", "market_status")
    op.drop_column("market_quotes", "quote_time")
    op.drop_column("market_quotes", "trading_date")
    op.drop_column("market_quotes", "previous_close")
