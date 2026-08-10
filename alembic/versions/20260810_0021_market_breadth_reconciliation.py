"""Bind market breadth snapshots to a canonical reconciled universe.

Revision ID: 20260810_0021
Revises: 20260809_0020
"""

from alembic import op
import sqlalchemy as sa


revision = "20260810_0021"
down_revision = "20260809_0020"
branch_labels = None
depends_on = None


TABLE = "market_breadth_snapshots"


def _columns() -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(TABLE)}


def _unique_names() -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_unique_constraints(TABLE)
        if item.get("name")
    }


def upgrade():
    columns = _columns()
    additions = (
        ("universe_id", sa.String(40), "A_SHARE_SH_SZ"),
        ("universe_version", sa.String(20), "1.0.0"),
        ("membership_digest", sa.String(64), ""),
        ("reconciliation_digest", sa.String(64), ""),
        ("reconciliation_status", sa.String(48), "INCOMPLETE"),
        ("primary_count", sa.Integer(), "0"),
        ("supplementary_count", sa.Integer(), "0"),
        ("source_lineage", sa.JSON(), "{}"),
    )
    for name, type_, default in additions:
        if name not in columns:
            op.add_column(
                TABLE,
                sa.Column(name, type_, nullable=False, server_default=default),
            )

    unique_names = _unique_names()
    with op.batch_alter_table(TABLE) as batch:
        if "uq_breadth_market_date" in unique_names:
            batch.drop_constraint("uq_breadth_market_date", type_="unique")
        if "uq_breadth_market_universe_date" not in unique_names:
            batch.create_unique_constraint(
                "uq_breadth_market_universe_date",
                ["market_id", "universe_id", "universe_version", "trade_date"],
            )


def downgrade():
    columns = _columns()
    unique_names = _unique_names()
    with op.batch_alter_table(TABLE) as batch:
        if "uq_breadth_market_universe_date" in unique_names:
            batch.drop_constraint("uq_breadth_market_universe_date", type_="unique")
        if "uq_breadth_market_date" not in unique_names:
            batch.create_unique_constraint("uq_breadth_market_date", ["market_id", "trade_date"])
    for name in (
        "source_lineage",
        "supplementary_count",
        "primary_count",
        "reconciliation_status",
        "reconciliation_digest",
        "membership_digest",
        "universe_version",
        "universe_id",
    ):
        if name in columns:
            op.drop_column(TABLE, name)
