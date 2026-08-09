"""selected stock strategy advisory snapshots

Revision ID: 20260731_0018
Revises: 20260728_0017
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME


revision = "20260731_0018"
down_revision = "20260728_0017"
branch_labels = None
depends_on = None


PRECISE_DATETIME = sa.DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")

TABLE_NAME = "selected_stock_analysis_runs"
EXPECTED_COLUMNS = {
    "id",
    "symbol",
    "analysis_date",
    "strategy_id",
    "strategy_version",
    "strategy_mode",
    "status",
    "request_hash",
    "snapshot_hash",
    "result_digest",
    "analysis_identity_hash",
    "request_snapshot",
    "result_snapshot",
    "quality_bindings",
    "source_lineage",
    "generated_at",
    "created_at",
}


def _validate_legacy_metadata_table() -> bool:
    """Accept only the exact table leaked by historical revision 0001 metadata."""

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE_NAME):
        return False
    columns = {item["name"] for item in inspector.get_columns(TABLE_NAME)}
    unique_constraints = {
        item.get("name"): tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints(TABLE_NAME)
    }
    indexes = {
        item.get("name"): tuple(item.get("column_names") or ())
        for item in inspector.get_indexes(TABLE_NAME)
    }
    if columns != EXPECTED_COLUMNS:
        raise RuntimeError(
            f"existing {TABLE_NAME} columns do not match revision 0018"
        )
    if unique_constraints.get("uq_selected_stock_analysis_identity") != (
        "analysis_identity_hash",
    ):
        raise RuntimeError(
            f"existing {TABLE_NAME} identity constraint does not match revision 0018"
        )
    if indexes.get("ix_selected_stock_symbol_date") != (
        "symbol",
        "analysis_date",
    ):
        raise RuntimeError(
            f"existing {TABLE_NAME} lookup index does not match revision 0018"
        )
    return True


def upgrade() -> None:
    if _validate_legacy_metadata_table():
        return
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=12), nullable=False),
        sa.Column("analysis_date", sa.Date(), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=30), nullable=False),
        sa.Column("strategy_mode", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("result_digest", sa.String(length=64), nullable=False),
        sa.Column("analysis_identity_hash", sa.String(length=64), nullable=False),
        sa.Column("request_snapshot", sa.JSON(), nullable=False),
        sa.Column("result_snapshot", sa.JSON(), nullable=False),
        sa.Column("quality_bindings", sa.JSON(), nullable=False),
        sa.Column("source_lineage", sa.JSON(), nullable=False),
        sa.Column("generated_at", PRECISE_DATETIME, nullable=False),
        sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "analysis_identity_hash",
            name="uq_selected_stock_analysis_identity",
        ),
    )
    op.create_index(
        "ix_selected_stock_symbol_date",
        TABLE_NAME,
        ["symbol", "analysis_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_selected_stock_symbol_date",
        table_name=TABLE_NAME,
    )
    op.drop_table(TABLE_NAME)
