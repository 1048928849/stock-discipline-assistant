"""Expand bootstrap adapter identity for multiple providers.

Revision ID: 20260728_0017
Revises: 20260728_0016
"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_0017"
down_revision = "20260728_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("historical_data_bootstrap_runs") as batch:
        batch.alter_column(
            "adapter_version",
            existing_type=sa.String(20),
            type_=sa.String(64),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("historical_data_bootstrap_runs") as batch:
        batch.alter_column(
            "adapter_version",
            existing_type=sa.String(64),
            type_=sa.String(20),
            existing_nullable=False,
        )
