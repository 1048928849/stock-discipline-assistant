"""Add one-click plan analysis audit runs.

Revision ID: 20260723_0007
Revises: 20260722_0006
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0007"
down_revision = "20260722_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("plan_analysis_runs"):
        return
    op.create_table(
        "plan_analysis_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("position_mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("request_snapshot", sa.JSON(), nullable=False),
        sa.Column("pipeline_steps", sa.JSON(), nullable=False),
        sa.Column("result_snapshot", sa.JSON()),
        sa.Column(
            "ai_analysis_id",
            sa.Integer(),
            sa.ForeignKey("trade_plan_ai_analyses.id"),
        ),
        sa.Column("user_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confirmed_plan_id", sa.Integer(), sa.ForeignKey("trade_plans.id")),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    for column in ("symbol", "account_id", "status", "ai_analysis_id", "confirmed_plan_id"):
        op.create_index(f"ix_plan_analysis_runs_{column}", "plan_analysis_runs", [column])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("plan_analysis_runs"):
        op.drop_table("plan_analysis_runs")
