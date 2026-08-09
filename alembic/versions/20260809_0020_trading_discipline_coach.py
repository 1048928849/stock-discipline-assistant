"""Add trading discipline coaching models.

Revision ID: 20260809_0020
Revises: 20260809_0019
"""

from alembic import op
import sqlalchemy as sa

revision = "20260809_0020"
down_revision = "20260809_0019"
branch_labels = None
depends_on = None


def upgrade():
    # Revision 0001 historically materializes current metadata on a fresh database.
    # Remove only the exact P1T tables that can leak from that behavior, then create
    # the revision-owned schema below. Existing databases at 0019 do not have them.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in (
        "trade_discipline_reviews",
        "trading_training_programs",
        "pretrade_discipline_checks",
        "trade_thesis_snapshots",
        "source_evidence_records",
        "market_stage_snapshots",
        "trading_playbooks",
    ):
        if table in existing:
            op.drop_table(table)
    op.create_table(
        "trading_playbooks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("version", sa.String(30), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("code", "version", name="uq_trading_playbook_version"),
    )
    op.create_index("ix_trading_playbooks_code", "trading_playbooks", ["code"])
    op.create_index("ix_trading_playbooks_active", "trading_playbooks", ["active"])
    op.create_table(
        "market_stage_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("source_analysis_run_id", sa.Integer()),
        sa.Column("evidence_status", sa.String(30), nullable=False),
        sa.Column("rule_version", sa.String(30), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "symbol", "as_of", "rule_version", name="uq_market_stage_symbol_asof_rule"
        ),
    )
    for name, cols in (
        ("ix_market_stage_snapshots_symbol", ["symbol"]),
        ("ix_market_stage_snapshots_stage", ["stage"]),
        ("ix_market_stage_snapshots_as_of", ["as_of"]),
        ("ix_market_stage_snapshots_source_analysis_run_id", ["source_analysis_run_id"]),
    ):
        op.create_index(name, "market_stage_snapshots", cols)
    op.create_table(
        "source_evidence_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(12)),
        sa.Column("scope", sa.String(100)),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("content_summary", sa.Text(), nullable=False),
        sa.Column("tier", sa.String(20), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("source_reference", sa.String(1000)),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("added_by", sa.String(100), nullable=False),
        sa.Column("before_or_after_entry", sa.String(20), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("quality", sa.String(30), nullable=False),
        sa.Column("lineage", sa.JSON(), nullable=False),
        sa.Column("related_thesis_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    for col in (
        "symbol",
        "scope",
        "tier",
        "published_at",
        "observed_at",
        "before_or_after_entry",
        "related_thesis_id",
    ):
        op.create_index(f"ix_source_evidence_records_{col}", "source_evidence_records", [col])
    op.create_table(
        "trade_thesis_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("thesis_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parent_snapshot_id", sa.Integer(), sa.ForeignKey("trade_thesis_snapshots.id")),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column(
            "playbook_id", sa.Integer(), sa.ForeignKey("trading_playbooks.id"), nullable=False
        ),
        sa.Column("entry_reasons", sa.JSON(), nullable=False),
        sa.Column("invalidation_conditions", sa.JSON(), nullable=False),
        sa.Column("expected_behavior", sa.JSON(), nullable=False),
        sa.Column("hard_stop", sa.Numeric(18, 4), nullable=False),
        sa.Column("initial_position", sa.Integer(), nullable=False),
        sa.Column("max_position", sa.Integer(), nullable=False),
        sa.Column("fact_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("analysis_hypotheses", sa.JSON(), nullable=False),
        sa.Column("strategy_run_refs", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("snapshot_hash", name="uq_trade_thesis_snapshot_hash"),
    )
    for col in ("thesis_id", "parent_snapshot_id", "symbol", "account_id", "playbook_id"):
        op.create_index(f"ix_trade_thesis_snapshots_{col}", "trade_thesis_snapshots", [col])
    op.create_table(
        "pretrade_discipline_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("decision_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("category_scores", sa.JSON(), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("rule_version", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("snapshot_hash", name="uq_pretrade_check_hash"),
    )
    for col in ("account_id", "symbol", "decision_at", "status"):
        op.create_index(f"ix_pretrade_discipline_checks_{col}", "pretrade_discipline_checks", [col])
    op.create_table(
        "trading_training_programs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column(
            "playbook_id", sa.Integer(), sa.ForeignKey("trading_playbooks.id"), nullable=False
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("target_samples", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("valid_samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("excluded_samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    for col in ("account_id", "playbook_id", "status"):
        op.create_index(f"ix_trading_training_programs_{col}", "trading_training_programs", [col])
    op.create_table(
        "trade_discipline_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trade_id", sa.Integer(), sa.ForeignKey("trades.id"), nullable=False),
        sa.Column(
            "training_program_id", sa.Integer(), sa.ForeignKey("trading_training_programs.id")
        ),
        sa.Column(
            "pretrade_check_id", sa.Integer(), sa.ForeignKey("pretrade_discipline_checks.id")
        ),
        sa.Column("thesis_snapshot_id", sa.Integer(), sa.ForeignKey("trade_thesis_snapshots.id")),
        sa.Column("stage_at_decision", sa.String(30), nullable=False),
        sa.Column("planned_position", sa.Integer()),
        sa.Column("actual_position", sa.Integer()),
        sa.Column("planned_entry", sa.JSON()),
        sa.Column("actual_entry", sa.Numeric(18, 4)),
        sa.Column("planned_stop", sa.Numeric(18, 4)),
        sa.Column("deviations", sa.JSON(), nullable=False),
        sa.Column("post_entry_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("pnl_pct", sa.Numeric(12, 6)),
        sa.Column("mfe_pct", sa.Numeric(12, 6)),
        sa.Column("mae_pct", sa.Numeric(12, 6)),
        sa.Column("exit_reason", sa.String(100)),
        sa.Column("compliance_result", sa.String(50), nullable=False),
        sa.Column("error_codes", sa.JSON(), nullable=False),
        sa.Column("rule_version", sa.String(30), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("trade_id", "rule_version", name="uq_trade_discipline_review_rule"),
    )
    for col in (
        "trade_id",
        "training_program_id",
        "pretrade_check_id",
        "thesis_snapshot_id",
        "compliance_result",
    ):
        op.create_index(f"ix_trade_discipline_reviews_{col}", "trade_discipline_reviews", [col])
    op.bulk_insert(
        sa.table(
            "trading_playbooks",
            sa.column("code", sa.String),
            sa.column("version", sa.String),
            sa.column("name", sa.String),
            sa.column("description", sa.String),
            sa.column("active", sa.Boolean),
            sa.column("rules", sa.JSON),
        ),
        [
            {
                "code": "CORE_STATE_CHANGE_V1",
                "version": "1.0.0",
                "name": "核心状态变化训练法",
                "description": "核心股：状态变化 → 二次确认 → 第一次有效分歧",
                "active": True,
                "rules": {"formal_strategy": False, "target_samples": 20},
            }
        ],
    )


def downgrade():
    for table in (
        "trade_discipline_reviews",
        "trading_training_programs",
        "pretrade_discipline_checks",
        "trade_thesis_snapshots",
        "source_evidence_records",
        "market_stage_snapshots",
        "trading_playbooks",
    ):
        op.drop_table(table)
