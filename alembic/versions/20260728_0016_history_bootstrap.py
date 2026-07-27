"""Add resumable historical data bootstrap storage.

Revision ID: 20260728_0016
Revises: 20260727_0015
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql


revision = "20260728_0016"
down_revision = "20260727_0015"
branch_labels = None
depends_on = None


PRECISE_DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
TABLE_NAMES = (
    "historical_data_bootstrap_runs",
    "historical_data_bootstrap_items",
)


def upgrade() -> None:
    existing = {name for name in TABLE_NAMES if inspect(op.get_bind()).has_table(name)}
    if existing == set(TABLE_NAMES):
        return
    if existing:
        raise RuntimeError(f"partial history bootstrap schema: {sorted(existing)}")
    op.create_table(
        "historical_data_bootstrap_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider_id", sa.String(80), nullable=False),
        sa.Column("adapter_version", sa.String(20), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("required_symbols", sa.JSON(), nullable=False),
        sa.Column("ready_symbols", sa.Integer(), nullable=False),
        sa.Column("failed_symbols", sa.Integer(), nullable=False),
        sa.Column("benchmark_ready", sa.Boolean(), nullable=False),
        sa.Column("total_rows_written", sa.Integer(), nullable=False),
        sa.Column("coverage_ratio", sa.Numeric(12, 6), nullable=False),
        sa.Column("started_at", PRECISE_DATETIME, nullable=False),
        sa.Column("completed_at", PRECISE_DATETIME),
        sa.Column("blocked_reasons", sa.JSON(), nullable=False),
        sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        sa.UniqueConstraint(
            "purpose",
            "market",
            "trade_date",
            "plan_hash",
            name="uq_history_bootstrap_run_identity",
        ),
    )
    for column in ("purpose", "market", "trade_date", "status"):
        op.create_index(
            f"ix_historical_data_bootstrap_runs_{column}",
            "historical_data_bootstrap_runs",
            [column],
        )
    op.create_table(
        "historical_data_bootstrap_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("capability", sa.String(80), nullable=False),
        sa.Column("adjustment", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("requested_start", sa.Date(), nullable=False),
        sa.Column("requested_end", sa.Date(), nullable=False),
        sa.Column("rows_received", sa.Integer(), nullable=False),
        sa.Column("rows_written", sa.Integer(), nullable=False),
        sa.Column("first_trade_date", sa.Date()),
        sa.Column("last_trade_date", sa.Date()),
        sa.Column("quality_record_id", sa.Integer()),
        sa.Column("normalized_digest", sa.String(64)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", PRECISE_DATETIME),
        sa.Column("completed_at", PRECISE_DATETIME),
        sa.ForeignKeyConstraint(
            ["run_id"], ["historical_data_bootstrap_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["quality_record_id"], ["data_quality_records.id"]),
        sa.UniqueConstraint(
            "run_id",
            "symbol",
            "capability",
            name="uq_history_bootstrap_item_scope",
        ),
    )
    for column in ("run_id", "symbol", "capability", "status", "quality_record_id"):
        op.create_index(
            f"ix_historical_data_bootstrap_items_{column}",
            "historical_data_bootstrap_items",
            [column],
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    for table_name in reversed(TABLE_NAMES):
        if inspector.has_table(table_name):
            op.drop_table(table_name)
