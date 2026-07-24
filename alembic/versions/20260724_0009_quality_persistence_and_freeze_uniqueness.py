"""Persist provider quality and enforce formal-plan uniqueness.

Revision ID: 20260724_0009
Revises: 20260723_0008
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_0009"
down_revision = "20260723_0008"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_exists(table: str) -> bool:
    return _inspector().has_table(table)


def _columns(table: str) -> set[str]:
    return {item["name"] for item in _inspector().get_columns(table)}


def _index_names(table: str) -> set[str]:
    return {item["name"] for item in _inspector().get_indexes(table)}


def _constraint_names(table: str) -> set[str]:
    inspector = _inspector()
    names = {
        item["name"]
        for item in inspector.get_unique_constraints(table)
        if item.get("name")
    }
    names.update(
        item["name"]
        for item in inspector.get_foreign_keys(table)
        if item.get("name")
    )
    return names


def _has_foreign_key(
    table: str,
    constrained_columns: list[str],
    referred_table: str,
    referred_columns: list[str],
) -> bool:
    return any(
        item.get("constrained_columns") == constrained_columns
        and item.get("referred_table") == referred_table
        and item.get("referred_columns") == referred_columns
        for item in _inspector().get_foreign_keys(table)
    )


def _add_missing_columns(table: str, columns: list[sa.Column]) -> None:
    missing = [column for column in columns if column.name not in _columns(table)]
    if not missing:
        return
    with op.batch_alter_table(table) as batch:
        for column in missing:
            batch.add_column(column)


def _ensure_index(table: str, name: str, columns: list[str]) -> None:
    if name not in _index_names(table):
        op.create_index(name, table, columns)


def _ensure_unique_constraint(table: str, name: str, columns: list[str]) -> None:
    if name not in _constraint_names(table):
        with op.batch_alter_table(table) as batch:
            batch.create_unique_constraint(name, columns)


def _ensure_foreign_key(
    table: str,
    name: str,
    constrained_columns: list[str],
    referred_table: str,
    referred_columns: list[str],
) -> None:
    if not _has_foreign_key(
        table, constrained_columns, referred_table, referred_columns
    ):
        with op.batch_alter_table(table) as batch:
            batch.create_foreign_key(
                name,
                referred_table,
                constrained_columns,
                referred_columns,
            )


def upgrade() -> None:
    if not _table_exists("data_quality_records"):
        op.create_table(
            "data_quality_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(40)),
            sa.Column("capability", sa.String(80), nullable=False),
            sa.Column("quality_status", sa.String(20), nullable=False),
            sa.Column("observed_at", sa.DateTime()),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            sa.Column("cached_at", sa.DateTime()),
            sa.Column("provider_id", sa.String(80), nullable=False),
            sa.Column("provider_observations", sa.JSON(), nullable=False),
            sa.Column("normalized_digest", sa.String(64)),
            sa.Column("conflict_fields", sa.JSON(), nullable=False),
            sa.Column("adjustment", sa.String(20)),
            sa.Column("price_unit", sa.String(20)),
            sa.Column("volume_unit", sa.String(20)),
            sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("cache_used", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("trusted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("persisted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("scan_start", sa.DateTime()),
            sa.Column("scan_end", sa.DateTime()),
            sa.Column("checked_at", sa.DateTime()),
            sa.Column("latest_content_at", sa.DateTime()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    for column in ("symbol", "capability", "quality_status", "trusted", "persisted"):
        _ensure_index(
            "data_quality_records",
            f"ix_data_quality_records_{column}",
            [column],
        )

    _add_missing_columns(
        "market_quotes",
        [
            sa.Column("quote_type", sa.String(20), nullable=False, server_default="realtime"),
            sa.Column(
                "observed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
            ),
            sa.Column("price_unit", sa.String(20), nullable=False, server_default="CNY"),
            sa.Column(
                "quality_status",
                sa.String(20),
                nullable=False,
                server_default="SINGLE_SOURCE",
            ),
            sa.Column("quality_record_id", sa.Integer()),
        ],
    )
    _ensure_foreign_key(
        "market_quotes",
        "fk_market_quotes_quality_record",
        ["quality_record_id"],
        "data_quality_records",
        ["id"],
    )
    _ensure_index(
        "market_quotes",
        "ix_market_quotes_quality_record_id",
        ["quality_record_id"],
    )

    _add_missing_columns(
        "market_daily_bars",
        [
            sa.Column("adjustment", sa.String(20), nullable=False, server_default="qfq"),
            sa.Column("price_unit", sa.String(20), nullable=False, server_default="CNY"),
            sa.Column("volume_unit", sa.String(20), nullable=False, server_default="share"),
            sa.Column(
                "observed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "quality_status",
                sa.String(20),
                nullable=False,
                server_default="SINGLE_SOURCE",
            ),
            sa.Column("quality_record_id", sa.Integer()),
        ],
    )
    _ensure_foreign_key(
        "market_daily_bars",
        "fk_market_daily_bars_quality_record",
        ["quality_record_id"],
        "data_quality_records",
        ["id"],
    )
    _ensure_index(
        "market_daily_bars",
        "ix_market_daily_bars_quality_record_id",
        ["quality_record_id"],
    )

    _add_missing_columns(
        "company_research_refreshes",
        [
            sa.Column("quality_status", sa.String(20)),
            sa.Column("observed_at", sa.DateTime()),
            sa.Column("fetched_at", sa.DateTime()),
            sa.Column("checked_at", sa.DateTime()),
            sa.Column("scan_start", sa.DateTime()),
            sa.Column("scan_end", sa.DateTime()),
            sa.Column("normalized_digest", sa.String(64)),
            sa.Column("provider_observations", sa.JSON()),
            sa.Column("conflict_fields", sa.JSON()),
        ],
    )

    _add_missing_columns("trade_plans", [sa.Column("analysis_run_id", sa.Integer())])
    _ensure_foreign_key(
        "trade_plans",
        "fk_trade_plans_analysis_run",
        ["analysis_run_id"],
        "plan_analysis_runs",
        ["id"],
    )
    _ensure_index("trade_plans", "ix_trade_plans_analysis_run_id", ["analysis_run_id"])
    _ensure_unique_constraint(
        "trade_plans", "uq_trade_plan_analysis_run", ["analysis_run_id"]
    )
    _ensure_unique_constraint(
        "trade_plans",
        "uq_trade_plan_account_symbol_version",
        ["account_id", "symbol", "plan_version"],
    )


def downgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        if "uq_trade_plan_account_symbol_version" in _constraint_names("trade_plans"):
            batch.drop_constraint(
                "uq_trade_plan_account_symbol_version", type_="unique"
            )
        if "uq_trade_plan_analysis_run" in _constraint_names("trade_plans"):
            batch.drop_constraint("uq_trade_plan_analysis_run", type_="unique")
        if "ix_trade_plans_analysis_run_id" in _index_names("trade_plans"):
            batch.drop_index("ix_trade_plans_analysis_run_id")
        if "analysis_run_id" in _columns("trade_plans"):
            trade_plan_fks = _inspector().get_foreign_keys("trade_plans")
            if any(
                item.get("name") == "fk_trade_plans_analysis_run"
                for item in trade_plan_fks
            ):
                batch.drop_constraint(
                    "fk_trade_plans_analysis_run", type_="foreignkey"
                )
            batch.drop_column("analysis_run_id")

    with op.batch_alter_table("company_research_refreshes") as batch:
        for column in (
            "conflict_fields",
            "provider_observations",
            "normalized_digest",
            "scan_end",
            "scan_start",
            "checked_at",
            "fetched_at",
            "observed_at",
            "quality_status",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("market_daily_bars") as batch:
        if "ix_market_daily_bars_quality_record_id" in _index_names(
            "market_daily_bars"
        ):
            batch.drop_index("ix_market_daily_bars_quality_record_id")
        daily_fks = _inspector().get_foreign_keys("market_daily_bars")
        if any(
            item.get("name") == "fk_market_daily_bars_quality_record"
            for item in daily_fks
        ):
            batch.drop_constraint(
                "fk_market_daily_bars_quality_record", type_="foreignkey"
            )
        for column in (
            "quality_record_id",
            "quality_status",
            "observed_at",
            "volume_unit",
            "price_unit",
            "adjustment",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("market_quotes") as batch:
        if "ix_market_quotes_quality_record_id" in _index_names("market_quotes"):
            batch.drop_index("ix_market_quotes_quality_record_id")
        quote_fks = _inspector().get_foreign_keys("market_quotes")
        if any(
            item.get("name") == "fk_market_quotes_quality_record"
            for item in quote_fks
        ):
            batch.drop_constraint(
                "fk_market_quotes_quality_record", type_="foreignkey"
            )
        for column in (
            "quality_record_id",
            "quality_status",
            "price_unit",
            "observed_at",
            "quote_type",
        ):
            batch.drop_column(column)

    op.drop_table("data_quality_records")
