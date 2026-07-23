"""Add P0 planning, rules and position workflow tables.

Revision ID: 20260722_0005
Revises: 20260722_0004
"""

from alembic import op
import sqlalchemy as sa


revision = "20260722_0005"
down_revision = "20260722_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("rule_sets"):
        op.create_table(
            "rule_sets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(80), nullable=False, unique=True),
            sa.Column("name", sa.String(150), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("source_name", sa.String(200), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_rule_sets_code", "rule_sets", ["code"], unique=True)
    if not inspector.has_table("rule_versions"):
        op.create_table(
            "rule_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("rule_set_id", sa.Integer(), sa.ForeignKey("rule_sets.id"), nullable=False),
            sa.Column("version", sa.String(30), nullable=False),
            sa.Column("parameters", sa.JSON(), nullable=False),
            sa.Column("rules", sa.JSON(), nullable=False),
            sa.Column("change_note", sa.Text(), nullable=False),
            sa.Column("effective_from", sa.Date(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("rule_set_id", "version", name="uq_rule_version"),
        )
        op.create_index("ix_rule_versions_rule_set_id", "rule_versions", ["rule_set_id"])
    if not inspector.has_table("trade_plans"):
        op.create_table(
            "trade_plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column(
                "rule_version_id", sa.Integer(), sa.ForeignKey("rule_versions.id"), nullable=False
            ),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("name", sa.String(100)),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("trade_mode", sa.String(50), nullable=False),
            sa.Column("decision_level", sa.String(30), nullable=False),
            sa.Column("market_state", sa.String(30)),
            sa.Column("sector_state", sa.String(30)),
            sa.Column("large_cycle_direction", sa.String(30)),
            sa.Column("industry_logic", sa.Text()),
            sa.Column("company_logic", sa.Text()),
            sa.Column("technical_structure", sa.Text()),
            sa.Column("buy_zone_low", sa.Numeric(18, 4), nullable=False),
            sa.Column("buy_zone_high", sa.Numeric(18, 4), nullable=False),
            sa.Column("initial_stop", sa.Numeric(18, 4), nullable=False),
            sa.Column("invalidation_condition", sa.Text(), nullable=False),
            sa.Column("target_plan", sa.Text()),
            sa.Column("account_equity", sa.Numeric(20, 4), nullable=False),
            sa.Column("risk_pct", sa.Numeric(8, 4), nullable=False),
            sa.Column("max_position_pct", sa.Numeric(8, 4), nullable=False),
            sa.Column("planned_quantity", sa.Integer(), nullable=False),
            sa.Column("planned_position_value", sa.Numeric(20, 4), nullable=False),
            sa.Column("planned_risk_amount", sa.Numeric(20, 4), nullable=False),
            sa.Column("add_condition", sa.Text()),
            sa.Column("reduce_condition", sa.Text()),
            sa.Column("exit_condition", sa.Text()),
            sa.Column("no_trade_condition", sa.Text()),
            sa.Column("next_action", sa.Text(), nullable=False),
            sa.Column("data_status", sa.String(30), nullable=False),
            sa.Column("data_date", sa.Date(), nullable=False),
            sa.Column("source", sa.String(200), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )
        for column in ("account_id", "rule_version_id", "symbol", "status"):
            op.create_index(f"ix_trade_plans_{column}", "trade_plans", [column])
    if not inspector.has_table("trade_plan_checks"):
        op.create_table(
            "trade_plan_checks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "trade_plan_id",
                sa.Integer(),
                sa.ForeignKey("trade_plans.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("gate_code", sa.String(50), nullable=False),
            sa.Column("gate_name", sa.String(100), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("basis", sa.Text(), nullable=False),
            sa.Column("missing_data", sa.JSON(), nullable=False),
            sa.Column("rule_version", sa.String(30), nullable=False),
            sa.Column("checked_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("trade_plan_id", "gate_code", name="uq_trade_plan_gate"),
        )
        op.create_index(
            "ix_trade_plan_checks_trade_plan_id", "trade_plan_checks", ["trade_plan_id"]
        )
    if not inspector.has_table("position_snapshots"):
        op.create_table(
            "position_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "holding_id",
                sa.Integer(),
                sa.ForeignKey("holdings.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("trade_plan_id", sa.Integer(), sa.ForeignKey("trade_plans.id")),
            sa.Column("snapshot_date", sa.Date(), nullable=False),
            sa.Column("stage", sa.String(30), nullable=False),
            sa.Column("logic_status", sa.String(30), nullable=False),
            sa.Column("hard_stop_triggered", sa.Boolean(), nullable=False),
            sa.Column("invalidation_triggered", sa.Boolean()),
            sa.Column("allow_add", sa.Boolean(), nullable=False),
            sa.Column("risk_amount", sa.Numeric(20, 4)),
            sa.Column("risk_exposure_pct", sa.Numeric(8, 4)),
            sa.Column("supporting_evidence", sa.JSON(), nullable=False),
            sa.Column("opposing_evidence", sa.JSON(), nullable=False),
            sa.Column("next_action", sa.Text(), nullable=False),
            sa.Column("source", sa.String(200), nullable=False),
            sa.Column("data_date", sa.Date(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
        )
        for column in ("holding_id", "trade_plan_id", "snapshot_date"):
            op.create_index(f"ix_position_snapshots_{column}", "position_snapshots", [column])


def downgrade() -> None:
    for table in (
        "position_snapshots",
        "trade_plan_checks",
        "trade_plans",
        "rule_versions",
        "rule_sets",
    ):
        if sa.inspect(op.get_bind()).has_table(table):
            op.drop_table(table)
