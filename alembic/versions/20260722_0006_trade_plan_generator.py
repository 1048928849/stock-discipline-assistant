"""Add generator snapshots, plan versions and AI research audit.

Revision ID: 20260722_0006
Revises: 20260722_0005
"""

from alembic import op
import sqlalchemy as sa


revision = "20260722_0006"
down_revision = "20260722_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("trade_plans")}
    additions = (
        (
            "plan_version",
            sa.Column("plan_version", sa.Integer(), nullable=False, server_default="1"),
        ),
        (
            "parent_plan_id",
            # SQLite cannot add a foreign-key constraint with ALTER TABLE. The application
            # validates the parent id and the new audit table still uses real FKs.
            sa.Column("parent_plan_id", sa.Integer()),
        ),
        ("preview_hash", sa.Column("preview_hash", sa.String(64))),
        ("engine_snapshot", sa.Column("engine_snapshot", sa.JSON())),
        ("market_snapshot", sa.Column("market_snapshot", sa.JSON())),
        ("account_snapshot", sa.Column("account_snapshot", sa.JSON())),
        ("source_snapshot", sa.Column("source_snapshot", sa.JSON())),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("trade_plans", column)
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("trade_plans")}
    if "ix_trade_plans_preview_hash" not in indexes:
        op.create_index("ix_trade_plans_preview_hash", "trade_plans", ["preview_hash"])
    if not inspector.has_table("trade_plan_ai_analyses"):
        op.create_table(
            "trade_plan_ai_analyses",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("trade_plan_id", sa.Integer(), sa.ForeignKey("trade_plans.id")),
            sa.Column("evidence_hash", sa.String(64), nullable=False),
            sa.Column("evidence_version", sa.String(64), nullable=False),
            sa.Column("rule_version", sa.String(30), nullable=False),
            sa.Column("provider", sa.String(100), nullable=False),
            sa.Column("model", sa.String(100), nullable=False),
            sa.Column("prompt_version", sa.String(30), nullable=False),
            sa.Column("evidence_package", sa.JSON(), nullable=False),
            sa.Column("structured_output", sa.JSON()),
            sa.Column("source_ids", sa.JSON(), nullable=False),
            sa.Column("validation_result", sa.JSON()),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("prompt_tokens", sa.Integer()),
            sa.Column("completion_tokens", sa.Integer()),
            sa.Column("total_tokens", sa.Integer()),
            sa.Column("duration_ms", sa.Integer()),
            sa.Column("error", sa.Text()),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )
        for column in ("symbol", "trade_plan_id", "evidence_hash", "status"):
            op.create_index(
                f"ix_trade_plan_ai_analyses_{column}", "trade_plan_ai_analyses", [column]
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("trade_plan_ai_analyses"):
        op.drop_table("trade_plan_ai_analyses")
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("trade_plans")}
    for name in (
        "source_snapshot",
        "account_snapshot",
        "market_snapshot",
        "engine_snapshot",
        "preview_hash",
        "parent_plan_id",
        "plan_version",
    ):
        if name in columns:
            op.drop_column("trade_plans", name)
