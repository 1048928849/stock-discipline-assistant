"""Add immutable preview snapshots.

Revision ID: 20260723_0009
Revises: 20260723_0008
"""

import sqlalchemy as sa

from alembic import op

revision = "20260723_0009"
down_revision = "20260723_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "preview_snapshots" in inspector.get_table_names():
        return
    op.create_table(
        "preview_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("preview_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("strategy_snapshot", sa.JSON(), nullable=False),
        sa.Column("feature_snapshot", sa.JSON(), nullable=False),
        sa.Column("risk_snapshot", sa.JSON(), nullable=False),
        sa.Column("decision_snapshot", sa.JSON(), nullable=False),
        sa.Column("price_snapshot", sa.JSON(), nullable=False),
        sa.Column("rule_version_snapshot", sa.JSON(), nullable=False),
        sa.Column("account_snapshot", sa.JSON(), nullable=False),
        sa.Column("market_snapshot", sa.JSON(), nullable=False),
        sa.Column("preview_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "account_id", "symbol", "preview_hash", name="uq_preview_snapshot_scope"
        ),
    )
    for column in ("account_id", "symbol", "preview_hash"):
        op.create_index(f"ix_preview_snapshots_{column}", "preview_snapshots", [column])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "preview_snapshots" in inspector.get_table_names():
        op.drop_table("preview_snapshots")
