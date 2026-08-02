"""Add strategy research, evidence and validation records.

Revision ID: 20260723_0011
Revises: 20260723_0010
"""

import sqlalchemy as sa

from alembic import op

revision = "20260723_0011"
down_revision = "20260723_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "strategy_research_records" not in tables:
        op.create_table(
            "strategy_research_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "strategy_version_id",
                sa.Integer(),
                sa.ForeignKey("strategy_versions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("hypothesis", sa.Text(), nullable=False),
            sa.Column("thesis", sa.Text(), nullable=False),
            sa.Column("causal_chain", sa.JSON(), nullable=False),
            sa.Column("market_conditions", sa.JSON(), nullable=False),
            sa.Column("applicable_scenarios", sa.JSON(), nullable=False),
            sa.Column("failure_conditions", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("strategy_version_id", name="uq_strategy_research_version"),
        )
        op.create_index(
            "ix_strategy_research_records_strategy_version_id",
            "strategy_research_records",
            ["strategy_version_id"],
        )
    if "strategy_evidence_records" not in tables:
        op.create_table(
            "strategy_evidence_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "strategy_version_id",
                sa.Integer(),
                sa.ForeignKey("strategy_versions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("type", sa.String(30), nullable=False),
            sa.Column("source", sa.String(200), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("reference", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index(
            "ix_strategy_evidence_records_strategy_version_id",
            "strategy_evidence_records",
            ["strategy_version_id"],
        )
        op.create_index(
            "ix_strategy_evidence_records_type", "strategy_evidence_records", ["type"]
        )
    if "strategy_validation_records" not in tables:
        op.create_table(
            "strategy_validation_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "strategy_version_id",
                sa.Integer(),
                sa.ForeignKey("strategy_versions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("validation_type", sa.String(80), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("result_summary", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        for column in ("strategy_version_id", "validation_type", "status"):
            op.create_index(
                f"ix_strategy_validation_records_{column}",
                "strategy_validation_records",
                [column],
            )

    bind = op.get_bind()
    versions = sa.table(
        "strategy_versions",
        sa.column("id", sa.Integer),
        sa.column("strategy_id", sa.String),
        sa.column("version", sa.String),
    )
    version_id = bind.execute(
        sa.select(versions.c.id).where(
            versions.c.strategy_id == "platform_breakout_pullback",
            versions.c.version == "1.0.0",
        )
    ).scalar()
    if version_id is None:
        return
    research = sa.table(
        "strategy_research_records",
        sa.column("strategy_version_id", sa.Integer),
        sa.column("hypothesis", sa.Text),
        sa.column("thesis", sa.Text),
        sa.column("causal_chain", sa.JSON),
        sa.column("market_conditions", sa.JSON),
        sa.column("applicable_scenarios", sa.JSON),
        sa.column("failure_conditions", sa.JSON),
    )
    exists = bind.execute(
        sa.select(research.c.strategy_version_id).where(
            research.c.strategy_version_id == version_id
        )
    ).first()
    if exists is None:
        bind.execute(
            research.insert().values(
                strategy_version_id=version_id,
                hypothesis="有效平台突破后缩量回踩、再次转强，能提供风险边界明确的趋势试仓机会。",
                thesis="以平台结构确认供需平衡，以放量突破和缩量回踩过滤弱突破，并用再次转强确认执行。",
                causal_chain=[
                    "平台整理形成可观察边界",
                    "放量突破显示需求增强",
                    "缩量回踩验证抛压减弱",
                    "再次转强触发试仓",
                    "结构失效或硬止损触发退出",
                ],
                market_conditions=["市场非明显下降", "周线非明显下降", "行情数据完整且新鲜"],
                applicable_scenarios=["日线趋势波段", "平台突破后的首次有效回踩"],
                failure_conditions=[
                    "数据不足或过期",
                    "未形成有效平台",
                    "突破后放量跌回平台",
                    "趋势或平台结构被破坏",
                    "止损距离或风险收益不合格",
                ],
            )
        )
    evidence = sa.table(
        "strategy_evidence_records",
        sa.column("strategy_version_id", sa.Integer),
        sa.column("type", sa.String),
        sa.column("source", sa.String),
        sa.column("content", sa.Text),
        sa.column("reference", sa.Text),
    )
    evidence_exists = bind.execute(
        sa.select(evidence.c.strategy_version_id).where(
            evidence.c.strategy_version_id == version_id,
            evidence.c.source == "existing_strategy_migration",
        )
    ).first()
    if evidence_exists is None:
        bind.execute(
            evidence.insert().values(
                strategy_version_id=version_id,
                type="MANUAL",
                source="existing_strategy_migration",
                content="来自现有平台突破-回踩确认规则、Golden Master和纪律方法说明的结构化研究基线。",
                reference="PlatformBreakoutPullbackStrategy 1.0.0",
            )
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in (
        "strategy_validation_records",
        "strategy_evidence_records",
        "strategy_research_records",
    ):
        if table in tables:
            op.drop_table(table)
