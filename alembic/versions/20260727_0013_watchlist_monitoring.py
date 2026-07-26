"""Add watchlist lifecycle and deterministic monitoring tables.

Revision ID: 20260727_0013
Revises: 20260726_0012
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql


revision = "20260727_0013"
down_revision = "20260726_0012"
branch_labels = None
depends_on = None


PRECISE_DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
MONEY = sa.Numeric(20, 4)
PRICE = sa.Numeric(18, 4)


def upgrade() -> None:
    # Revision 0001 historically creates the then-current model metadata on a fresh
    # database. On that legacy path these explicit tables already exist; databases
    # genuinely parked at 0012 still execute the definitions below.
    if inspect(op.get_bind()).has_table("watchlist_items"):
        return
    op.create_table(
        "watchlist_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(12), nullable=False),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("monitoring_health", sa.String(30), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_reference", sa.String(100), nullable=False),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=True),
        sa.Column("strategy_version", sa.String(40), nullable=True),
        sa.Column("strategy_implementation_hash", sa.String(64), nullable=True),
        sa.Column("strategy_parameter_hash", sa.String(64), nullable=True),
        sa.Column("strategy_signal_hash", sa.String(64), nullable=True),
        sa.Column("strategy_binding_hash", sa.String(64), nullable=True),
        sa.Column("analysis_capital", MONEY, nullable=False),
        sa.Column("entry_low", PRICE, nullable=True),
        sa.Column("entry_high", PRICE, nullable=True),
        sa.Column("hard_stop", PRICE, nullable=True),
        sa.Column("waiting_conditions", sa.JSON(), nullable=False),
        sa.Column("invalidation_conditions", sa.JSON(), nullable=False),
        sa.Column("latest_snapshot_hash", sa.String(64), nullable=True),
        sa.Column("latest_package_hash", sa.String(64), nullable=True),
        sa.Column("latest_analysis_id", sa.Integer(), nullable=True),
        sa.Column("monitoring_enabled", sa.Boolean(), nullable=False),
        sa.Column("current_price", PRICE, nullable=True),
        sa.Column("current_price_observed_at", PRECISE_DATETIME, nullable=True),
        sa.Column("market_state", sa.String(30), nullable=True),
        sa.Column("industry_state", sa.String(30), nullable=True),
        sa.Column("data_quality", sa.String(20), nullable=True),
        sa.Column("last_analyzed_at", PRECISE_DATETIME, nullable=True),
        sa.Column("last_scanned_at", PRECISE_DATETIME, nullable=True),
        sa.Column("next_scan_at", PRECISE_DATETIME, nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        sa.Column("updated_at", PRECISE_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["latest_analysis_id"], ["plan_analysis_runs.id"]),
        sa.UniqueConstraint(
            "source_type", "source_reference", name="uq_watchlist_source_reference"
        ),
    )
    op.create_index("ix_watchlist_account_id", "watchlist_items", ["account_id"])
    op.create_index("ix_watchlist_symbol", "watchlist_items", ["symbol"])
    op.create_index("ix_watchlist_status", "watchlist_items", ["status"])
    op.create_index(
        "ix_watchlist_monitoring_health", "watchlist_items", ["monitoring_health"]
    )
    op.create_index(
        "ix_watchlist_monitoring_enabled", "watchlist_items", ["monitoring_enabled"]
    )
    op.create_index("ix_watchlist_next_scan_at", "watchlist_items", ["next_scan_at"])
    op.create_index(
        "ix_watchlist_latest_analysis_id", "watchlist_items", ["latest_analysis_id"]
    )
    op.create_index(
        "ix_watchlist_account_symbol", "watchlist_items", ["account_id", "symbol"]
    )

    op.create_table(
        "watchlist_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("watchlist_item_id", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("previous_revision_number", sa.Integer(), nullable=True),
        sa.Column("change_reason", sa.String(100), nullable=False),
        sa.Column("changed_by", sa.String(50), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(
            ["watchlist_item_id"], ["watchlist_items.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "watchlist_item_id",
            "revision_number",
            name="uq_watchlist_revision_item_number",
        ),
    )
    op.create_index(
        "ix_watchlist_revision_item", "watchlist_revisions", ["watchlist_item_id"]
    )

    op.create_table(
        "watchlist_transitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("watchlist_item_id", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(30), nullable=False),
        sa.Column("to_status", sa.String(30), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("evidence_references", sa.JSON(), nullable=False),
        sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
        sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(
            ["watchlist_item_id"], ["watchlist_items.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_watchlist_transition_item", "watchlist_transitions", ["watchlist_item_id"]
    )
    op.create_index(
        "ix_watchlist_transition_status", "watchlist_transitions", ["to_status"]
    )
    op.create_index(
        "ix_watchlist_transition_observed", "watchlist_transitions", ["observed_at"]
    )

    op.create_table(
        "reanalysis_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("watchlist_item_id", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("requested_at", PRECISE_DATETIME, nullable=False),
        sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(
            ["watchlist_item_id"], ["watchlist_items.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_reanalysis_request_dedupe"),
    )
    op.create_index(
        "ix_reanalysis_request_item", "reanalysis_requests", ["watchlist_item_id"]
    )
    op.create_index("ix_reanalysis_request_status", "reanalysis_requests", ["status"])
    op.create_index(
        "ix_reanalysis_request_requested", "reanalysis_requests", ["requested_at"]
    )

    op.create_table(
        "reanalysis_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("watchlist_item_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("analysis_run_id", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", PRECISE_DATETIME, nullable=False),
        sa.Column("finished_at", PRECISE_DATETIME, nullable=True),
        sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"], ["reanalysis_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["watchlist_item_id"], ["watchlist_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["plan_analysis_runs.id"]),
        sa.UniqueConstraint("request_id", name="uq_reanalysis_run_request"),
    )
    op.create_index("ix_reanalysis_run_request", "reanalysis_runs", ["request_id"])
    op.create_index("ix_reanalysis_run_item", "reanalysis_runs", ["watchlist_item_id"])
    op.create_index("ix_reanalysis_run_status", "reanalysis_runs", ["status"])
    op.create_index(
        "ix_reanalysis_run_analysis", "reanalysis_runs", ["analysis_run_id"]
    )

    op.create_table(
        "monitoring_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("watchlist_item_id", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
        sa.Column("created_at", PRECISE_DATETIME, nullable=False),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("acknowledged_at", PRECISE_DATETIME, nullable=True),
        sa.Column("reanalysis_required", sa.Boolean(), nullable=False),
        sa.Column("reanalysis_run_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["watchlist_item_id"], ["watchlist_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["reanalysis_run_id"], ["reanalysis_runs.id"]),
        sa.UniqueConstraint("dedupe_key", name="uq_monitoring_event_dedupe"),
    )
    op.create_index("ix_monitoring_event_item", "monitoring_events", ["watchlist_item_id"])
    op.create_index("ix_monitoring_event_type", "monitoring_events", ["event_type"])
    op.create_index("ix_monitoring_event_severity", "monitoring_events", ["severity"])
    op.create_index("ix_monitoring_event_observed", "monitoring_events", ["observed_at"])
    op.create_index(
        "ix_monitoring_event_reanalysis", "monitoring_events", ["reanalysis_run_id"]
    )

    op.create_table(
        "watchlist_monitor_leases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("owner_token", sa.String(64), nullable=True),
        sa.Column("acquired_until", PRECISE_DATETIME, nullable=True),
        sa.Column("lease_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", PRECISE_DATETIME, nullable=False),
    )
    op.create_index(
        "ix_watchlist_lease_owner", "watchlist_monitor_leases", ["owner_token"]
    )
    op.create_index(
        "ix_watchlist_lease_until", "watchlist_monitor_leases", ["acquired_until"]
    )


def downgrade() -> None:
    op.drop_table("watchlist_monitor_leases")
    op.drop_table("monitoring_events")
    op.drop_table("reanalysis_runs")
    op.drop_table("reanalysis_requests")
    op.drop_table("watchlist_transitions")
    op.drop_table("watchlist_revisions")
    op.drop_table("watchlist_items")
