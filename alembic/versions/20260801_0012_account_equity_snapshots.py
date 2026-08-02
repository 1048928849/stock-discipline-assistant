"""Add account equity snapshots for drawdown controls.

Revision ID: 20260801_0012
Revises: 20260723_0011
"""

import sqlalchemy as sa

from alembic import op

revision = "20260801_0012"
down_revision = "20260723_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "account_equity_snapshots" in inspector.get_table_names():
        return
    op.create_table(
        "account_equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("equity", sa.Numeric(20, 4), nullable=False),
        sa.Column("source", sa.String(30), nullable=False, server_default="account_update"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("account_id", "snapshot_date", name="uq_account_equity_date"),
    )
    op.create_index(
        "ix_account_equity_snapshots_account_id", "account_equity_snapshots", ["account_id"]
    )
    op.create_index(
        "ix_account_equity_snapshots_snapshot_date",
        "account_equity_snapshots",
        ["snapshot_date"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "account_equity_snapshots" in inspector.get_table_names():
        op.drop_table("account_equity_snapshots")
