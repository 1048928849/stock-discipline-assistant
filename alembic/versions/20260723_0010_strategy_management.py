"""Add strategy definitions, versions and trade plan references.

Revision ID: 20260723_0010
Revises: 20260723_0009
"""

import sqlalchemy as sa

from alembic import op

revision = "20260723_0010"
down_revision = "20260723_0009"
branch_labels = None
depends_on = None

STRATEGY_ID = "platform_breakout_pullback"
PLATFORM_BREAKOUT_PARAMETERS = {
    "default_account_equity": 300000,
    "default_risk_pct": 0.5,
    "max_single_position_pct": 30,
    "max_total_position_pct": 80,
    "max_industry_position_pct": 40,
    "position_tranches": 3,
    "market_high_risk_total_cap_pct": 30,
    "market_neutral_total_cap_pct": 60,
    "platform_min_days": 20,
    "breakout_pct": 1.0,
    "breakout_volume_multiple": 1.5,
    "pullback_tolerance_pct": 3.0,
    "pullback_volume_ratio": 0.8,
    "ma_periods": [5, 20, 60, 250],
    "atr_buffer_multiple": 0.5,
    "minimum_reward_risk": 2.0,
    "maximum_stop_distance_pct": 8.0,
    "freshness_days": 5,
    "trial_position_ratio": 0.3333,
    "pullback_confirmed_ratio": 0.7,
}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "strategies" not in tables:
        op.create_table(
            "strategies",
            sa.Column("id", sa.String(80), primary_key=True),
            sa.Column("name", sa.String(150), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("category", sa.String(80), nullable=False),
            sa.Column("owner", sa.String(100), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_strategies_category", "strategies", ["category"])
        op.create_index("ix_strategies_status", "strategies", ["status"])
    if "strategy_versions" not in tables:
        op.create_table(
            "strategy_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "strategy_id",
                sa.String(80),
                sa.ForeignKey("strategies.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.String(30), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("rule_snapshot", sa.JSON(), nullable=False),
            sa.Column("parameter_snapshot", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("activated_at", sa.DateTime()),
            sa.Column("retired_at", sa.DateTime()),
            sa.UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),
        )
        op.create_index("ix_strategy_versions_strategy_id", "strategy_versions", ["strategy_id"])
        op.create_index("ix_strategy_versions_status", "strategy_versions", ["status"])
    if "strategy_lifecycle_events" not in tables:
        op.create_table(
            "strategy_lifecycle_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "strategy_id",
                sa.String(80),
                sa.ForeignKey("strategies.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "strategy_version_id",
                sa.Integer(),
                sa.ForeignKey("strategy_versions.id", ondelete="SET NULL"),
            ),
            sa.Column("from_status", sa.String(20)),
            sa.Column("to_status", sa.String(20), nullable=False),
            sa.Column("event_type", sa.String(50), nullable=False),
            sa.Column("reason", sa.Text()),
            sa.Column("occurred_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        for column in ("strategy_id", "strategy_version_id", "to_status"):
            op.create_index(
                f"ix_strategy_lifecycle_events_{column}",
                "strategy_lifecycle_events",
                [column],
            )

    columns = {item["name"] for item in inspector.get_columns("trade_plans")}
    if "strategy_id" not in columns:
        op.add_column("trade_plans", sa.Column("strategy_id", sa.String(80)))
        op.create_index("ix_trade_plans_strategy_id", "trade_plans", ["strategy_id"])
    if "strategy_version_id" not in columns:
        op.add_column("trade_plans", sa.Column("strategy_version_id", sa.Integer()))
        op.create_index(
            "ix_trade_plans_strategy_version_id", "trade_plans", ["strategy_version_id"]
        )

    bind = op.get_bind()
    strategies = sa.table(
        "strategies",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("category", sa.String),
        sa.column("owner", sa.String),
        sa.column("status", sa.String),
    )
    if bind.execute(sa.select(strategies.c.id).where(strategies.c.id == STRATEGY_ID)).first() is None:
        bind.execute(
            strategies.insert().values(
                id=STRATEGY_ID,
                name="平台突破-回踩确认",
                description="日线平台放量突破、缩量回踩并再次转强的纪律策略。",
                category="趋势波段",
                owner="system",
                status="ACTIVE",
            )
        )
    versions = sa.table(
        "strategy_versions",
        sa.column("id", sa.Integer),
        sa.column("strategy_id", sa.String),
        sa.column("version", sa.String),
        sa.column("status", sa.String),
        sa.column("rule_snapshot", sa.JSON),
        sa.column("parameter_snapshot", sa.JSON),
        sa.column("activated_at", sa.DateTime),
    )
    version_id = bind.execute(
        sa.select(versions.c.id).where(
            versions.c.strategy_id == STRATEGY_ID,
            versions.c.version == "1.0.0",
        )
    ).scalar()
    if version_id is None:
        bind.execute(
            versions.insert().values(
                strategy_id=STRATEGY_ID,
                version="1.0.0",
                status="ACTIVE",
                rule_snapshot={
                    "engine": "PlatformBreakoutPullbackStrategy",
                    "required_rules": [
                        "platform_data_sufficiency",
                        "large_cycle_direction",
                        "platform_structure",
                        "breakout_volume_confirmation",
                        "pullback_structure",
                        "turn_stronger_confirmation",
                    ],
                },
                parameter_snapshot=PLATFORM_BREAKOUT_PARAMETERS,
                activated_at=sa.func.now(),
            )
        )
        version_id = bind.execute(
            sa.select(versions.c.id).where(
                versions.c.strategy_id == STRATEGY_ID,
                versions.c.version == "1.0.0",
            )
        ).scalar_one()
    events = sa.table(
        "strategy_lifecycle_events",
        sa.column("strategy_id", sa.String),
        sa.column("strategy_version_id", sa.Integer),
        sa.column("from_status", sa.String),
        sa.column("to_status", sa.String),
        sa.column("event_type", sa.String),
        sa.column("reason", sa.Text),
    )
    event_exists = bind.execute(
        sa.select(events.c.strategy_version_id).where(
            events.c.strategy_version_id == version_id,
            events.c.event_type == "existing_strategy_registered",
        )
    ).first()
    if event_exists is None:
        bind.execute(
            events.insert().values(
                strategy_id=STRATEGY_ID,
                strategy_version_id=version_id,
                from_status=None,
                to_status="ACTIVE",
                event_type="existing_strategy_registered",
                reason="Registered existing Strategy Engine implementation during migration",
            )
        )
    trade_plans = sa.table(
        "trade_plans",
        sa.column("strategy_id", sa.String),
        sa.column("strategy_version_id", sa.Integer),
    )
    bind.execute(
        trade_plans.update()
        .where(trade_plans.c.strategy_id.is_(None))
        .values(strategy_id=STRATEGY_ID, strategy_version_id=version_id)
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("trade_plans")}
    if "strategy_version_id" in columns:
        op.drop_index("ix_trade_plans_strategy_version_id", table_name="trade_plans")
        op.drop_column("trade_plans", "strategy_version_id")
    if "strategy_id" in columns:
        op.drop_index("ix_trade_plans_strategy_id", table_name="trade_plans")
        op.drop_column("trade_plans", "strategy_id")
    tables = set(inspector.get_table_names())
    for table in ("strategy_lifecycle_events", "strategy_versions", "strategies"):
        if table in tables:
            op.drop_table(table)
