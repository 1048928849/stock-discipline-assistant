"""Close watchlist monitoring state and invalidation semantics.

Revision ID: 20260727_0014
Revises: 20260727_0013
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql


revision = "20260727_0014"
down_revision = "20260727_0013"
branch_labels = None
depends_on = None


PRECISE_DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def _columns(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    watchlist_columns = _columns("watchlist_items")
    if "invalidation_rule_specs" not in watchlist_columns:
        with op.batch_alter_table("watchlist_items") as batch:
            batch.add_column(
                sa.Column("invalidation_rule_specs", sa.JSON(), nullable=True)
            )
        table = sa.table(
            "watchlist_items",
            sa.column("invalidation_rule_specs", sa.JSON()),
        )
        op.execute(
            table.update()
            .where(table.c.invalidation_rule_specs.is_(None))
            .values(invalidation_rule_specs=[])
        )
        with op.batch_alter_table("watchlist_items") as batch:
            batch.alter_column(
                "invalidation_rule_specs",
                existing_type=sa.JSON(),
                nullable=False,
            )
    if "industry_name" not in watchlist_columns:
        with op.batch_alter_table("watchlist_items") as batch:
            batch.add_column(sa.Column("industry_name", sa.String(200)))
            batch.create_index("ix_watchlist_items_industry_name", ["industry_name"])

    regime_columns = _columns("market_regime_snapshots")
    with op.batch_alter_table("market_regime_snapshots") as batch:
        if "quality_status" not in regime_columns:
            batch.add_column(sa.Column("quality_status", sa.String(20)))
        if "quality_bindings" not in regime_columns:
            batch.add_column(sa.Column("quality_bindings", sa.JSON(), nullable=True))
    if "quality_bindings" not in regime_columns:
        table = sa.table(
            "market_regime_snapshots",
            sa.column("quality_bindings", sa.JSON()),
        )
        op.execute(
            table.update()
            .where(table.c.quality_bindings.is_(None))
            .values(quality_bindings=[])
        )
        with op.batch_alter_table("market_regime_snapshots") as batch:
            batch.alter_column(
                "quality_bindings",
                existing_type=sa.JSON(),
                nullable=False,
            )

    if not inspect(op.get_bind()).has_table("industry_analysis_snapshots"):
        op.create_table(
            "industry_analysis_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("industry_name", sa.String(200), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("classification", sa.String(20), nullable=False),
            sa.Column("product_snapshot_hash", sa.String(64), nullable=False),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.Column("quality_status", sa.String(20), nullable=False),
            sa.Column("quality_bindings", sa.JSON(), nullable=False),
            sa.UniqueConstraint(
                "industry_name",
                "trade_date",
                name="uq_industry_analysis_name_date",
            ),
        )
        op.create_index(
            "ix_industry_analysis_snapshots_industry_name",
            "industry_analysis_snapshots",
            ["industry_name"],
        )
        op.create_index(
            "ix_industry_analysis_snapshots_trade_date",
            "industry_analysis_snapshots",
            ["trade_date"],
        )
        op.create_index(
            "ix_industry_analysis_snapshots_classification",
            "industry_analysis_snapshots",
            ["classification"],
        )


def downgrade() -> None:
    if inspect(op.get_bind()).has_table("industry_analysis_snapshots"):
        op.drop_table("industry_analysis_snapshots")
    watchlist_columns = _columns("watchlist_items")
    with op.batch_alter_table("watchlist_items") as batch:
        if "industry_name" in watchlist_columns:
            batch.drop_index("ix_watchlist_items_industry_name")
            batch.drop_column("industry_name")
        if "invalidation_rule_specs" in watchlist_columns:
            batch.drop_column("invalidation_rule_specs")
    regime_columns = _columns("market_regime_snapshots")
    with op.batch_alter_table("market_regime_snapshots") as batch:
        if "quality_bindings" in regime_columns:
            batch.drop_column("quality_bindings")
        if "quality_status" in regime_columns:
            batch.drop_column("quality_status")
