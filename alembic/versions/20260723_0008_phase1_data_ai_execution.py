"""Phase 1 provider audit, research refresh and plan execution tracking.

Revision ID: 20260723_0008
Revises: 20260723_0007
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0008"
down_revision = "20260723_0007"
branch_labels = None
depends_on = None


def _index(table: str, column: str) -> None:
    op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "data_provider_call_logs" not in tables:
        op.create_table(
            "data_provider_call_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider_id", sa.String(80), nullable=False),
            sa.Column("capability", sa.String(80), nullable=False),
            sa.Column("operation", sa.String(100), nullable=False),
            sa.Column("symbol", sa.String(12)),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("cache_used", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duration_ms", sa.Integer()),
            sa.Column("error", sa.Text()),
            sa.Column("requested_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime()),
        )
        for column in ("provider_id", "capability", "symbol", "status"):
            _index("data_provider_call_logs", column)
    if "company_research_refreshes" not in tables:
        op.create_table(
            "company_research_refreshes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("section", sa.String(50), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("provider_id", sa.String(80)),
            sa.Column("source_name", sa.String(200)),
            sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cache_used", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_attempt_at", sa.DateTime(), nullable=False),
            sa.Column("last_success_at", sa.DateTime()),
            sa.Column("data_date", sa.Date()),
            sa.Column("stale_after", sa.DateTime()),
            sa.Column("error", sa.Text()),
            sa.UniqueConstraint("symbol", "section", name="uq_company_research_refresh_section"),
        )
        _index("company_research_refreshes", "symbol")
        _index("company_research_refreshes", "section")
    columns = {item["name"] for item in inspector.get_columns("trade_plans")}
    if "execution_status" not in columns:
        op.add_column(
            "trade_plans",
            sa.Column("execution_status", sa.String(30), nullable=False, server_default="draft"),
        )
        _index("trade_plans", "execution_status")
    if "execution_summary" not in columns:
        op.add_column("trade_plans", sa.Column("execution_summary", sa.JSON()))
    if "plan_execution_events" not in tables:
        op.create_table(
            "plan_execution_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "trade_plan_id",
                sa.Integer(),
                sa.ForeignKey("trade_plans.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("event_type", sa.String(50), nullable=False),
            sa.Column("from_status", sa.String(30)),
            sa.Column("to_status", sa.String(30), nullable=False),
            sa.Column("event_time", sa.DateTime(), nullable=False),
            sa.Column("source", sa.String(30), nullable=False, server_default="manual"),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        for column in ("trade_plan_id", "event_type", "to_status", "event_time"):
            _index("plan_execution_events", column)
    if "plan_execution_fills" not in tables:
        op.create_table(
            "plan_execution_fills",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "trade_plan_id",
                sa.Integer(),
                sa.ForeignKey("trade_plans.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("side", sa.String(8), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("price", sa.Numeric(18, 4), nullable=False),
            sa.Column("fee", sa.Numeric(20, 4), nullable=False, server_default="0"),
            sa.Column("executed_at", sa.DateTime(), nullable=False),
            sa.Column("reason", sa.String(100), nullable=False),
            sa.Column("trigger_confirmed", sa.Boolean()),
            sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        _index("plan_execution_fills", "trade_plan_id")
        _index("plan_execution_fills", "executed_at")


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "plan_execution_fills" in tables:
        op.drop_table("plan_execution_fills")
    if "plan_execution_events" in tables:
        op.drop_table("plan_execution_events")
    columns = {item["name"] for item in inspector.get_columns("trade_plans")}
    if "execution_summary" in columns:
        op.drop_column("trade_plans", "execution_summary")
    if "execution_status" in columns:
        op.drop_column("trade_plans", "execution_status")
    if "company_research_refreshes" in tables:
        op.drop_table("company_research_refreshes")
    if "data_provider_call_logs" in tables:
        op.drop_table("data_provider_call_logs")
