"""增加公司研究中心统一数据模型。

Revision ID: 20260722_0003
Revises: 20260722_0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260722_0003"
down_revision = "20260722_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table("company_profiles"):
        op.create_table(
            "company_profiles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("industry", sa.String(200)),
            sa.Column("market", sa.String(100)),
            sa.Column("main_business", sa.Text()),
            sa.Column("business_scope", sa.Text()),
            sa.Column("website", sa.String(500)),
            sa.Column("source", sa.String(100), nullable=False),
            sa.Column("source_url", sa.String(500)),
            sa.Column("raw_data", sa.JSON()),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.UniqueConstraint("symbol"),
        )
        op.create_index("ix_company_profiles_symbol", "company_profiles", ["symbol"], unique=True)
    if not inspector.has_table("company_financial_periods"):
        op.create_table(
            "company_financial_periods",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("report_date", sa.Date(), nullable=False),
            sa.Column("period_label", sa.String(30), nullable=False),
            *[
                sa.Column(name, sa.Numeric(24, 4))
                for name in (
                    "revenue",
                    "operating_cost",
                    "net_profit",
                    "parent_net_profit",
                    "operating_cash_flow",
                    "total_assets",
                    "total_liabilities",
                    "equity",
                    "accounts_receivable",
                    "inventory",
                    "research_expense",
                    "revenue_single_quarter",
                    "profit_single_quarter",
                    "cash_flow_single_quarter",
                )
            ],
            sa.Column("gross_margin", sa.Numeric(12, 6)),
            sa.Column("roe", sa.Numeric(12, 6)),
            sa.Column("debt_ratio", sa.Numeric(12, 6)),
            sa.Column("source", sa.String(100), nullable=False),
            sa.Column("source_url", sa.String(500)),
            sa.Column("raw_data", sa.JSON()),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("symbol", "report_date", name="uq_company_financial_symbol_period"),
        )
        op.create_index(
            "ix_company_financial_periods_symbol", "company_financial_periods", ["symbol"]
        )
        op.create_index(
            "ix_company_financial_periods_report_date", "company_financial_periods", ["report_date"]
        )
    if not inspector.has_table("company_announcements"):
        op.create_table(
            "company_announcements",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("announcement_category", sa.String(50), nullable=False),
            sa.Column("risk_level", sa.String(10), nullable=False),
            sa.Column("published_date", sa.Date(), nullable=False),
            sa.Column("catalog_source", sa.String(100), nullable=False),
            sa.Column("exchange", sa.String(30), nullable=False),
            sa.Column("url", sa.String(1000), nullable=False),
            sa.Column("source_document_url", sa.String(1000)),
            sa.Column("raw_data", sa.JSON()),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("symbol", "url", name="uq_company_announcement_url"),
        )
        for column in ("symbol", "announcement_category", "risk_level", "published_date"):
            op.create_index(f"ix_company_announcements_{column}", "company_announcements", [column])
    if not inspector.has_table("company_valuation_snapshots"):
        op.create_table(
            "company_valuation_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("market_cap", sa.Numeric(24, 4)),
            sa.Column("pe_ttm", sa.Numeric(18, 6)),
            sa.Column("pb", sa.Numeric(18, 6)),
            sa.Column("ps_ttm", sa.Numeric(18, 6)),
            sa.Column("pe_percentile", sa.Numeric(12, 6)),
            sa.Column("pb_percentile", sa.Numeric(12, 6)),
            sa.Column("ps_percentile", sa.Numeric(12, 6)),
            sa.Column("industry_comparison", sa.JSON()),
            sa.Column("implied_growth", sa.JSON()),
            sa.Column("source", sa.String(100), nullable=False),
            sa.Column("source_url", sa.String(500)),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("symbol", "trade_date", name="uq_company_valuation_symbol_date"),
        )
        op.create_index(
            "ix_company_valuation_snapshots_symbol", "company_valuation_snapshots", ["symbol"]
        )
        op.create_index(
            "ix_company_valuation_snapshots_trade_date",
            "company_valuation_snapshots",
            ["trade_date"],
        )
    if not inspector.has_table("company_research_evidence"):
        op.create_table(
            "company_research_evidence",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("evidence_key", sa.String(64), nullable=False, unique=True),
            sa.Column("symbol", sa.String(12), nullable=False),
            sa.Column("topic", sa.String(50), nullable=False),
            sa.Column("information_type", sa.String(30), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("source_name", sa.String(100), nullable=False),
            sa.Column("source_url", sa.String(1000)),
            sa.Column("source_date", sa.Date()),
            sa.Column("raw_data", sa.JSON()),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_company_research_evidence_symbol", "company_research_evidence", ["symbol"]
        )
        op.create_index(
            "ix_company_research_evidence_topic", "company_research_evidence", ["topic"]
        )


def downgrade() -> None:
    for table in (
        "company_research_evidence",
        "company_valuation_snapshots",
        "company_announcements",
        "company_financial_periods",
        "company_profiles",
    ):
        op.drop_table(table)
