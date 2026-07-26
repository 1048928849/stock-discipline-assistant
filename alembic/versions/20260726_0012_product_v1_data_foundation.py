"""Add Product V1 market, industry, concept, and chain storage.

Revision ID: 20260726_0012
Revises: 20260725_0011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql


revision = "20260726_0012"
down_revision = "20260725_0011"
branch_labels = None
depends_on = None


PRECISE_DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


TABLE_NAMES = (
    "market_intraday_bars",
    "market_turnover_snapshots",
    "market_breadth_snapshots",
    "market_amount_snapshots",
    "industry_market_snapshots",
    "industry_constituent_snapshots",
    "market_regime_snapshots",
    "concepts",
    "company_concepts",
    "industry_chains",
    "industry_chain_nodes",
    "company_chain_positions",
    "mapping_evidence",
)


def _missing(name: str) -> bool:
    return not inspect(op.get_bind()).has_table(name)


def _indexes(table: str, *columns: str) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    if _missing("market_intraday_bars"):
        op.create_table(
            "market_intraday_bars",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("bar_start", PRECISE_DATETIME, nullable=False),
            sa.Column("bar_end", PRECISE_DATETIME, nullable=False),
            sa.Column("open", sa.Numeric(18, 4), nullable=False),
            sa.Column("high", sa.Numeric(18, 4), nullable=False),
            sa.Column("low", sa.Numeric(18, 4), nullable=False),
            sa.Column("close", sa.Numeric(18, 4), nullable=False),
            sa.Column("volume", sa.Numeric(24, 4), nullable=False),
            sa.Column("amount", sa.Numeric(24, 4)),
            sa.Column("turnover_rate", sa.Numeric(12, 6)),
            sa.Column("adjustment", sa.String(20), nullable=False),
            sa.Column("price_unit", sa.String(20), nullable=False),
            sa.Column("volume_unit", sa.String(20), nullable=False),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.Column("source", sa.String(80), nullable=False),
            sa.Column("fetched_at", PRECISE_DATETIME, nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
            sa.UniqueConstraint(
                "symbol", "bar_start", "adjustment",
                name="uq_intraday_symbol_start_adjustment",
            ),
        )
        _indexes("market_intraday_bars", "symbol", "trade_date", "bar_start", "quality_record_id")

    if _missing("market_turnover_snapshots"):
        op.create_table(
            "market_turnover_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("turnover_rate", sa.Numeric(12, 6), nullable=False),
            sa.Column("amount", sa.Numeric(24, 4)),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.Column("source", sa.String(80), nullable=False),
            sa.Column("fetched_at", PRECISE_DATETIME, nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
            sa.UniqueConstraint("symbol", "trade_date", name="uq_turnover_symbol_date"),
        )
        _indexes("market_turnover_snapshots", "symbol", "trade_date", "quality_record_id")

    if _missing("market_breadth_snapshots"):
        op.create_table(
            "market_breadth_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("market_id", sa.String(20), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("advancing", sa.Integer(), nullable=False),
            sa.Column("declining", sa.Integer(), nullable=False),
            sa.Column("unchanged", sa.Integer(), nullable=False),
            sa.Column("limit_up", sa.Integer(), nullable=False),
            sa.Column("limit_down", sa.Integer(), nullable=False),
            sa.Column("new_highs", sa.Integer()),
            sa.Column("new_lows", sa.Integer()),
            sa.Column("median_change_pct", sa.Numeric(12, 6)),
            sa.Column("above_ma20_ratio", sa.Numeric(12, 6)),
            sa.Column("above_ma50_ratio", sa.Numeric(12, 6)),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.Column("source", sa.String(80), nullable=False),
            sa.Column("fetched_at", PRECISE_DATETIME, nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
            sa.UniqueConstraint("market_id", "trade_date", name="uq_breadth_market_date"),
        )
        _indexes("market_breadth_snapshots", "trade_date", "quality_record_id")

    if _missing("market_amount_snapshots"):
        op.create_table(
            "market_amount_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("market_id", sa.String(20), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("total_amount", sa.Numeric(24, 4), nullable=False),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.Column("source", sa.String(80), nullable=False),
            sa.Column("fetched_at", PRECISE_DATETIME, nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
            sa.UniqueConstraint("market_id", "trade_date", name="uq_amount_market_date"),
        )
        _indexes("market_amount_snapshots", "trade_date", "quality_record_id")

    if _missing("industry_market_snapshots"):
        op.create_table(
            "industry_market_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("industry_key", sa.String(20), nullable=False),
            sa.Column("industry_name", sa.String(200), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("change_pct", sa.Numeric(12, 6)),
            sa.Column("amount", sa.Numeric(24, 4)),
            sa.Column("amount_share", sa.Numeric(12, 6)),
            sa.Column("advance_ratio", sa.Numeric(12, 6)),
            sa.Column("limit_up_count", sa.Integer()),
            sa.Column("leader_strength", sa.Numeric(12, 6)),
            sa.Column("new_high_ratio", sa.Numeric(12, 6)),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.Column("source", sa.String(80), nullable=False),
            sa.Column("fetched_at", PRECISE_DATETIME, nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
            sa.UniqueConstraint("industry_key", "trade_date", name="uq_industry_market_date"),
        )
        _indexes("industry_market_snapshots", "industry_key", "industry_name", "trade_date", "quality_record_id")

    if _missing("industry_constituent_snapshots"):
        op.create_table(
            "industry_constituent_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("industry_key", sa.String(20), nullable=False),
            sa.Column("industry_name", sa.String(200), nullable=False),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("weight", sa.Numeric(12, 6)),
            sa.Column("change_pct", sa.Numeric(12, 6)),
            sa.Column("latest_price", sa.Numeric(18, 4)),
            sa.Column("high_52w", sa.Numeric(18, 4)),
            sa.Column("is_new_high", sa.Boolean()),
            sa.Column("snapshot_date", sa.Date(), nullable=False),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.Column("source", sa.String(80), nullable=False),
            sa.Column("fetched_at", PRECISE_DATETIME, nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
            sa.UniqueConstraint(
                "industry_key", "symbol", "snapshot_date",
                name="uq_industry_member_date",
            ),
        )
        _indexes("industry_constituent_snapshots", "industry_key", "industry_name", "symbol", "snapshot_date", "quality_record_id")

    if _missing("market_regime_snapshots"):
        op.create_table(
            "market_regime_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("market_id", sa.String(20), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("state", sa.String(20), nullable=False),
            sa.Column("previous_state", sa.String(20), nullable=False),
            sa.Column("transition", sa.String(50), nullable=False),
            sa.Column("product_snapshot_hash", sa.String(64), nullable=False),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.UniqueConstraint("market_id", "trade_date", name="uq_market_regime_date"),
        )
        _indexes("market_regime_snapshots", "trade_date", "state")

    if _missing("concepts"):
        op.create_table(
            "concepts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False, unique=True),
            sa.Column("source", sa.String(80), nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
        )
        _indexes("concepts", "quality_record_id")

    if _missing("company_concepts"):
        op.create_table(
            "company_concepts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("concept_id", sa.Integer(), nullable=False),
            sa.Column("relevance", sa.String(40), nullable=False),
            sa.Column("evidence_summary", sa.Text()),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["concept_id"], ["concepts.id"]),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
            sa.UniqueConstraint("symbol", "concept_id", name="uq_company_concept"),
        )
        _indexes("company_concepts", "symbol", "concept_id", "quality_record_id")

    if _missing("industry_chains"):
        op.create_table(
            "industry_chains",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False, unique=True),
            sa.Column("source", sa.String(80), nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
        )
        _indexes("industry_chains", "quality_record_id")

    if _missing("industry_chain_nodes"):
        op.create_table(
            "industry_chain_nodes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("chain_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("stage", sa.String(40), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["chain_id"], ["industry_chains.id"]),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
            sa.UniqueConstraint("chain_id", "name", name="uq_industry_chain_node"),
        )
        _indexes("industry_chain_nodes", "chain_id", "quality_record_id")

    if _missing("company_chain_positions"):
        op.create_table(
            "company_chain_positions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("node_id", sa.Integer(), nullable=False),
            sa.Column("relevance", sa.String(40), nullable=False),
            sa.Column("primary_products", sa.JSON()),
            sa.Column("revenue_relevance", sa.String(40), nullable=False),
            sa.Column("core_level", sa.String(40)),
            sa.Column("substitutability", sa.String(40)),
            sa.Column("competitive_position", sa.String(80)),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["node_id"], ["industry_chain_nodes.id"]),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
            sa.UniqueConstraint("symbol", "node_id", name="uq_company_chain_position"),
        )
        _indexes("company_chain_positions", "symbol", "node_id", "quality_record_id")

    if _missing("mapping_evidence"):
        op.create_table(
            "mapping_evidence",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("evidence_key", sa.String(64), nullable=False, unique=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("mapping_type", sa.String(30), nullable=False),
            sa.Column("source_name", sa.String(100), nullable=False),
            sa.Column("source_url", sa.String(1000)),
            sa.Column("excerpt", sa.Text(), nullable=False),
            sa.Column("raw_data", sa.JSON()),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
        )
        _indexes("mapping_evidence", "symbol", "mapping_type", "quality_record_id")


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    for name in reversed(TABLE_NAMES):
        if inspector.has_table(name):
            op.drop_table(name)
