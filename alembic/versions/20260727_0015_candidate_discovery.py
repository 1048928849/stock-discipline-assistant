"""Add deterministic candidate discovery storage.

Revision ID: 20260727_0015
Revises: 20260727_0014
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql


revision = "20260727_0015"
down_revision = "20260727_0014"
branch_labels = None
depends_on = None


PRECISE_DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
TABLE_NAMES = (
    "industry_capital_flow_snapshots",
    "market_event_pool_snapshots",
    "candidate_discovery_runs",
    "candidate_industry_assessments",
    "discovery_candidates",
)


def upgrade() -> None:
    existing = {name for name in TABLE_NAMES if inspect(op.get_bind()).has_table(name)}
    if existing == set(TABLE_NAMES):
        return
    if existing:
        raise RuntimeError(f"partial candidate discovery schema: {sorted(existing)}")
    op.create_table(
        "industry_capital_flow_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("industry_key", sa.String(40), nullable=False),
        sa.Column("industry_name", sa.String(200), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("net_inflow_1d", sa.Numeric(24, 4), nullable=False),
        sa.Column("net_inflow_5d", sa.Numeric(24, 4), nullable=False),
        sa.Column("net_inflow_10d", sa.Numeric(24, 4), nullable=False),
        sa.Column("amount", sa.Numeric(24, 4), nullable=False),
        sa.Column("amount_unit", sa.String(10), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
        sa.Column("fetched_at", PRECISE_DATETIME, nullable=False),
        sa.Column("quality_record_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
        sa.UniqueConstraint(
            "industry_key", "trade_date", name="uq_industry_capital_flow_date"
        ),
    )
    for column in ("industry_key", "industry_name", "trade_date", "quality_record_id"):
        op.create_index(
            f"ix_industry_capital_flow_snapshots_{column}",
            "industry_capital_flow_snapshots",
            [column],
        )

    op.create_table(
        "market_event_pool_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("first_event_at", PRECISE_DATETIME),
        sa.Column("last_event_at", PRECISE_DATETIME),
        sa.Column("sealed_amount", sa.Numeric(24, 4)),
        sa.Column("sealed_amount_unit", sa.String(10), nullable=False),
        sa.Column("turnover_rate", sa.Numeric(12, 6)),
        sa.Column("turnover_rate_unit", sa.String(20), nullable=False),
        sa.Column("consecutive_days", sa.Integer(), nullable=False),
        sa.Column("industry_name", sa.String(200)),
        sa.Column("reason_summary", sa.Text()),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
        sa.Column("fetched_at", PRECISE_DATETIME, nullable=False),
        sa.Column("quality_record_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
        sa.UniqueConstraint(
            "trade_date", "event_type", "symbol", name="uq_market_event_pool_symbol"
        ),
    )
    for column in (
        "symbol", "trade_date", "event_type", "industry_name", "quality_record_id"
    ):
        op.create_index(
            f"ix_market_event_pool_snapshots_{column}",
            "market_event_pool_snapshots",
            [column],
        )

    op.create_table(
        "candidate_discovery_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("algorithm_id", sa.String(64), nullable=False),
        sa.Column("algorithm_version", sa.String(20), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("input_snapshot_hash", sa.String(64)),
        sa.Column("market_state", sa.String(20)),
        sa.Column("quality_status", sa.String(20), nullable=False),
        sa.Column("blocked_reasons", sa.JSON(), nullable=False),
        sa.Column("quality_bindings", sa.JSON(), nullable=False),
        sa.Column("industries_evaluated", sa.Integer(), nullable=False),
        sa.Column("candidates_generated", sa.Integer(), nullable=False),
        sa.Column("total_constituents", sa.Integer(), nullable=False),
        sa.Column("historical_data_ready", sa.Integer(), nullable=False),
        sa.Column("historical_data_missing", sa.Integer(), nullable=False),
        sa.Column("coverage_ratio", sa.Numeric(12, 6), nullable=False),
        sa.Column("started_at", PRECISE_DATETIME, nullable=False),
        sa.Column("completed_at", PRECISE_DATETIME),
        sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        sa.UniqueConstraint(
            "market",
            "trade_date",
            "algorithm_version",
            "config_hash",
            name="uq_candidate_discovery_run_identity",
        ),
    )
    for column in ("market", "trade_date", "status"):
        op.create_index(
            f"ix_candidate_discovery_runs_{column}",
            "candidate_discovery_runs",
            [column],
        )

    op.create_table(
        "candidate_industry_assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("discovery_run_id", sa.Integer(), nullable=False),
        sa.Column("industry_key", sa.String(40), nullable=False),
        sa.Column("industry_name", sa.String(200), nullable=False),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column("score", sa.Numeric(18, 8)),
        sa.Column("rank", sa.Integer()),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("evidence_references", sa.JSON(), nullable=False),
        sa.Column("quality_status", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(
            ["discovery_run_id"], ["candidate_discovery_runs.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "discovery_run_id", "industry_key", name="uq_candidate_industry_run"
        ),
    )
    for column in ("discovery_run_id", "industry_key", "industry_name", "classification"):
        op.create_index(
            f"ix_candidate_industry_assessments_{column}",
            "candidate_industry_assessments",
            [column],
        )

    op.create_table(
        "discovery_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("discovery_run_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("industry_key", sa.String(40), nullable=False),
        sa.Column("industry_name", sa.String(200), nullable=False),
        sa.Column("candidate_type", sa.String(30), nullable=False),
        sa.Column("score", sa.Numeric(18, 8), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("current_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("technical_metrics", sa.JSON(), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("risk_flags", sa.JSON(), nullable=False),
        sa.Column("evidence_references", sa.JSON(), nullable=False),
        sa.Column("quality_status", sa.String(20), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", PRECISE_DATETIME, nullable=False),
        sa.Column("promoted_watchlist_item_id", sa.Integer()),
        sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        sa.Column("updated_at", PRECISE_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(
            ["discovery_run_id"], ["candidate_discovery_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["promoted_watchlist_item_id"], ["watchlist_items.id"]),
        sa.UniqueConstraint(
            "discovery_run_id", "symbol", name="uq_discovery_candidate_run_symbol"
        ),
    )
    for column in (
        "discovery_run_id", "symbol", "industry_key", "industry_name",
        "candidate_type", "status", "expires_at", "promoted_watchlist_item_id"
    ):
        op.create_index(
            f"ix_discovery_candidates_{column}", "discovery_candidates", [column]
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    for table_name in reversed(TABLE_NAMES):
        if inspector.has_table(table_name):
            op.drop_table(table_name)
