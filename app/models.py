from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


MONEY = Numeric(20, 4)
PRICE = Numeric(18, 4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    total_assets: Mapped[Decimal] = mapped_column(MONEY)
    cash: Mapped[Decimal] = mapped_column(MONEY)
    available_cash: Mapped[Decimal] = mapped_column(MONEY)
    holdings: Mapped[list["Holding"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Holding(TimestampMixin, Base):
    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("account_id", "symbol", name="uq_holding_account_symbol"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(Integer)
    cost_price: Mapped[Decimal] = mapped_column(PRICE)
    current_price: Mapped[Decimal] = mapped_column(PRICE)
    sector: Mapped[str | None] = mapped_column(String(100))
    buy_reason: Mapped[str | None] = mapped_column(Text)
    invalidation_condition: Mapped[str | None] = mapped_column(Text)
    stop_loss_price: Mapped[Decimal | None] = mapped_column(PRICE)
    target_price: Mapped[Decimal | None] = mapped_column(PRICE)
    max_position_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    price_source: Mapped[str] = mapped_column(String(30), default="manual")
    price_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    account: Mapped[Account] = relationship(back_populates="holdings")


class Trade(TimestampMixin, Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(PRICE)
    fee: Mapped[Decimal] = mapped_column(MONEY, default=0)
    traded_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    is_planned: Mapped[bool] = mapped_column(Boolean, default=True)
    emotion: Mapped[str | None] = mapped_column(String(50))
    expectation: Mapped[str | None] = mapped_column(Text)
    invalidation_condition: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    import_key: Mapped[str | None] = mapped_column(String(64), unique=True)


class TradeReview(TimestampMixin, Base):
    __tablename__ = "trade_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), index=True)
    period_type: Mapped[str] = mapped_column(String(10))
    period_start: Mapped[date] = mapped_column(Date)
    conclusion: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict | None] = mapped_column(JSON)


class DisciplineRule(TimestampMixin, Base):
    __tablename__ = "discipline_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    severity: Mapped[str] = mapped_column(String(10))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class DisciplineEvent(TimestampMixin, Base):
    __tablename__ = "discipline_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("discipline_rules.id"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"))
    severity: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    trigger_data: Mapped[dict | None] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MarketQuote(TimestampMixin, Base):
    __tablename__ = "market_quotes"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), unique=True)
    name: Mapped[str | None] = mapped_column(String(100))
    price: Mapped[Decimal] = mapped_column(PRICE)
    source: Mapped[str] = mapped_column(String(50))
    source_api: Mapped[str] = mapped_column(String(100))
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class MarketDailyBar(Base):
    __tablename__ = "market_daily_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", "source", name="uq_bar_symbol_date_source"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    trade_date: Mapped[date] = mapped_column(Date)
    open: Mapped[Decimal] = mapped_column(PRICE)
    high: Mapped[Decimal] = mapped_column(PRICE)
    low: Mapped[Decimal] = mapped_column(PRICE)
    close: Mapped[Decimal] = mapped_column(PRICE)
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    source: Mapped[str] = mapped_column(String(50))
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class MarketSourceLog(Base):
    __tablename__ = "market_source_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(50))
    api_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    error: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer, default=0)


class DataProviderCallLog(Base):
    __tablename__ = "data_provider_call_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(80), index=True)
    capability: Mapped[str] = mapped_column(String(80), index=True)
    operation: Mapped[str] = mapped_column(String(100))
    symbol: Mapped[str | None] = mapped_column(String(12), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    cache_used: Mapped[bool] = mapped_column(Boolean, default=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class XWatchAccount(TimestampMixin, Base):
    __tablename__ = "x_watch_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_post_id: Mapped[str | None] = mapped_column(String(50))


class XWatchQuery(TimestampMixin, Base):
    __tablename__ = "x_watch_queries"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    expression: Mapped[str] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class XPost(Base):
    __tablename__ = "x_posts"
    __table_args__ = (UniqueConstraint("platform", "post_id", name="uq_xpost_platform_post"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(20), default="x")
    post_id: Mapped[str] = mapped_column(String(50))
    author: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime)
    metrics: Mapped[dict | None] = mapped_column(JSON)
    url: Mapped[str] = mapped_column(String(500))
    collected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AIAnalysis(TimestampMixin, Base):
    __tablename__ = "ai_analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    content_type: Mapped[str] = mapped_column(String(30))
    content_id: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str | None] = mapped_column(String(500))
    source_time: Mapped[datetime | None] = mapped_column(DateTime)
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class BacktestRun(TimestampMixin, Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12))
    strategy: Mapped[str] = mapped_column(String(50))
    date_from: Mapped[date] = mapped_column(Date)
    date_to: Mapped[date] = mapped_column(Date)
    parameters: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))
    warning: Mapped[str | None] = mapped_column(Text)


class SystemJob(TimestampMixin, Base):
    __tablename__ = "system_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class TechnicalSnapshot(Base):
    __tablename__ = "technical_snapshots"
    __table_args__ = (
        UniqueConstraint("holding_id", "trade_date", name="uq_technical_holding_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    holding_id: Mapped[int] = mapped_column(
        ForeignKey("holdings.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    trade_date: Mapped[date] = mapped_column(Date)
    data_source: Mapped[str] = mapped_column(String(50))
    indicators: Mapped[dict] = mapped_column(JSON)
    signals: Mapped[dict] = mapped_column(JSON)
    support_levels: Mapped[list] = mapped_column(JSON)
    resistance_levels: Mapped[list] = mapped_column(JSON)
    conflicts: Mapped[list] = mapped_column(JSON)
    conclusion: Mapped[str] = mapped_column(Text)
    risk_notice: Mapped[str] = mapped_column(Text)
    changes: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CompanyProfile(TimestampMixin, Base):
    __tablename__ = "company_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    industry: Mapped[str | None] = mapped_column(String(200))
    market: Mapped[str | None] = mapped_column(String(100))
    main_business: Mapped[str | None] = mapped_column(Text)
    business_scope: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(String(500))
    raw_data: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class CompanyFinancialPeriod(Base):
    __tablename__ = "company_financial_periods"
    __table_args__ = (
        UniqueConstraint("symbol", "report_date", name="uq_company_financial_symbol_period"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    period_label: Mapped[str] = mapped_column(String(30))
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    operating_cost: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    net_profit: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    parent_net_profit: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    operating_cash_flow: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    total_liabilities: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    equity: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    accounts_receivable: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    inventory: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    research_expense: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    revenue_single_quarter: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    profit_single_quarter: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    cash_flow_single_quarter: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    gross_margin: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    roe: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    debt_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    source: Mapped[str] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(String(500))
    raw_data: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class CompanyAnnouncement(Base):
    __tablename__ = "company_announcements"
    __table_args__ = (UniqueConstraint("symbol", "url", name="uq_company_announcement_url"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    title: Mapped[str] = mapped_column(Text)
    announcement_category: Mapped[str] = mapped_column(String(50), index=True)
    risk_level: Mapped[str] = mapped_column(String(10), index=True)
    published_date: Mapped[date] = mapped_column(Date, index=True)
    catalog_source: Mapped[str] = mapped_column(String(100))
    exchange: Mapped[str] = mapped_column(String(30))
    url: Mapped[str] = mapped_column(String(1000))
    source_document_url: Mapped[str | None] = mapped_column(String(1000))
    raw_data: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class CompanyValuationSnapshot(Base):
    __tablename__ = "company_valuation_snapshots"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_company_valuation_symbol_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    pe_ttm: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    pb: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    ps_ttm: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    pe_percentile: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    pb_percentile: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ps_percentile: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    industry_comparison: Mapped[dict | None] = mapped_column(JSON)
    implied_growth: Mapped[dict | None] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class CompanyResearchEvidence(Base):
    __tablename__ = "company_research_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    evidence_key: Mapped[str] = mapped_column(String(64), unique=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    topic: Mapped[str] = mapped_column(String(50), index=True)
    information_type: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    source_date: Mapped[date | None] = mapped_column(Date)
    raw_data: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class CompanyResearchRefresh(Base):
    __tablename__ = "company_research_refreshes"
    __table_args__ = (
        UniqueConstraint("symbol", "section", name="uq_company_research_refresh_section"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    section: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(30))
    provider_id: Mapped[str | None] = mapped_column(String(80))
    source_name: Mapped[str | None] = mapped_column(String(200))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    cache_used: Mapped[bool] = mapped_column(Boolean, default=False)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    data_date: Mapped[date | None] = mapped_column(Date)
    stale_after: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)


class RuleSet(TimestampMixin, Base):
    __tablename__ = "rule_sets"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class RuleVersion(TimestampMixin, Base):
    __tablename__ = "rule_versions"
    __table_args__ = (UniqueConstraint("rule_set_id", "version", name="uq_rule_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_set_id: Mapped[int] = mapped_column(ForeignKey("rule_sets.id"), index=True)
    version: Mapped[str] = mapped_column(String(30))
    parameters: Mapped[dict] = mapped_column(JSON)
    rules: Mapped[dict] = mapped_column(JSON)
    change_note: Mapped[str] = mapped_column(Text)
    effective_from: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class TradePlan(TimestampMixin, Base):
    __tablename__ = "trade_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    rule_version_id: Mapped[int] = mapped_column(ForeignKey("rule_versions.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), index=True, default="DRAFT")
    trade_mode: Mapped[str] = mapped_column(String(50))
    decision_level: Mapped[str] = mapped_column(String(30))
    market_state: Mapped[str | None] = mapped_column(String(30))
    sector_state: Mapped[str | None] = mapped_column(String(30))
    large_cycle_direction: Mapped[str | None] = mapped_column(String(30))
    industry_logic: Mapped[str | None] = mapped_column(Text)
    company_logic: Mapped[str | None] = mapped_column(Text)
    technical_structure: Mapped[str | None] = mapped_column(Text)
    buy_zone_low: Mapped[Decimal] = mapped_column(PRICE)
    buy_zone_high: Mapped[Decimal] = mapped_column(PRICE)
    initial_stop: Mapped[Decimal] = mapped_column(PRICE)
    invalidation_condition: Mapped[str] = mapped_column(Text)
    target_plan: Mapped[str | None] = mapped_column(Text)
    account_equity: Mapped[Decimal] = mapped_column(MONEY)
    risk_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    max_position_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    planned_quantity: Mapped[int] = mapped_column(Integer, default=0)
    planned_position_value: Mapped[Decimal] = mapped_column(MONEY, default=0)
    planned_risk_amount: Mapped[Decimal] = mapped_column(MONEY, default=0)
    add_condition: Mapped[str | None] = mapped_column(Text)
    reduce_condition: Mapped[str | None] = mapped_column(Text)
    exit_condition: Mapped[str | None] = mapped_column(Text)
    no_trade_condition: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str] = mapped_column(Text)
    data_status: Mapped[str] = mapped_column(String(30), default="user_entered")
    data_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(200))
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    # SQLite 无法在不重建既有计划表的情况下添加自关联约束；服务层校验父版本。
    parent_plan_id: Mapped[int | None] = mapped_column(Integer)
    preview_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    engine_snapshot: Mapped[dict | None] = mapped_column(JSON)
    market_snapshot: Mapped[dict | None] = mapped_column(JSON)
    account_snapshot: Mapped[dict | None] = mapped_column(JSON)
    source_snapshot: Mapped[list | None] = mapped_column(JSON)
    execution_status: Mapped[str] = mapped_column(String(30), index=True, default="draft")
    execution_summary: Mapped[dict | None] = mapped_column(JSON)


class PreviewSnapshotRecord(Base):
    __tablename__ = "preview_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "symbol", "preview_hash", name="uq_preview_snapshot_scope"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    preview_hash: Mapped[str] = mapped_column(String(64), index=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    strategy_snapshot: Mapped[dict] = mapped_column(JSON)
    feature_snapshot: Mapped[dict] = mapped_column(JSON)
    risk_snapshot: Mapped[dict] = mapped_column(JSON)
    decision_snapshot: Mapped[dict] = mapped_column(JSON)
    price_snapshot: Mapped[dict] = mapped_column(JSON)
    rule_version_snapshot: Mapped[dict] = mapped_column(JSON)
    account_snapshot: Mapped[dict] = mapped_column(JSON)
    market_snapshot: Mapped[dict] = mapped_column(JSON)
    preview_payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TradePlanCheck(Base):
    __tablename__ = "trade_plan_checks"
    __table_args__ = (UniqueConstraint("trade_plan_id", "gate_code", name="uq_trade_plan_gate"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_plan_id: Mapped[int] = mapped_column(
        ForeignKey("trade_plans.id", ondelete="CASCADE"), index=True
    )
    gate_code: Mapped[str] = mapped_column(String(50))
    gate_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    basis: Mapped[str] = mapped_column(Text)
    missing_data: Mapped[list] = mapped_column(JSON)
    rule_version: Mapped[str] = mapped_column(String(30))
    checked_at: Mapped[datetime] = mapped_column(DateTime)


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    holding_id: Mapped[int] = mapped_column(
        ForeignKey("holdings.id", ondelete="CASCADE"), index=True
    )
    trade_plan_id: Mapped[int | None] = mapped_column(ForeignKey("trade_plans.id"), index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    stage: Mapped[str] = mapped_column(String(30))
    logic_status: Mapped[str] = mapped_column(String(30))
    hard_stop_triggered: Mapped[bool] = mapped_column(Boolean)
    invalidation_triggered: Mapped[bool | None] = mapped_column(Boolean)
    allow_add: Mapped[bool] = mapped_column(Boolean)
    risk_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    risk_exposure_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    supporting_evidence: Mapped[list] = mapped_column(JSON)
    opposing_evidence: Mapped[list] = mapped_column(JSON)
    next_action: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(200))
    data_date: Mapped[date] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class TradePlanAIAnalysis(Base):
    __tablename__ = "trade_plan_ai_analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    trade_plan_id: Mapped[int | None] = mapped_column(ForeignKey("trade_plans.id"), index=True)
    evidence_hash: Mapped[str] = mapped_column(String(64), index=True)
    evidence_version: Mapped[str] = mapped_column(String(64))
    rule_version: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(30))
    evidence_package: Mapped[dict] = mapped_column(JSON)
    structured_output: Mapped[dict | None] = mapped_column(JSON)
    source_ids: Mapped[list] = mapped_column(JSON)
    validation_result: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), index=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlanAnalysisRun(TimestampMixin, Base):
    """一次一键分析的完整审计快照；预览与正式计划分离。"""

    __tablename__ = "plan_analysis_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    position_mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), index=True)
    request_snapshot: Mapped[dict] = mapped_column(JSON)
    pipeline_steps: Mapped[list] = mapped_column(JSON)
    result_snapshot: Mapped[dict | None] = mapped_column(JSON)
    ai_analysis_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_plan_ai_analyses.id"), index=True
    )
    user_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_plan_id: Mapped[int | None] = mapped_column(ForeignKey("trade_plans.id"), index=True)
    error: Mapped[str | None] = mapped_column(Text)


class PlanExecutionEvent(Base):
    __tablename__ = "plan_execution_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_plan_id: Mapped[int] = mapped_column(
        ForeignKey("trade_plans.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30), index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String(30), default="manual")
    details: Mapped[dict] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PlanExecutionFill(Base):
    __tablename__ = "plan_execution_fills"
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_plan_id: Mapped[int] = mapped_column(
        ForeignKey("trade_plans.id", ondelete="CASCADE"), index=True
    )
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(PRICE)
    fee: Mapped[Decimal] = mapped_column(MONEY, default=0)
    executed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    reason: Mapped[str] = mapped_column(String(100))
    trigger_confirmed: Mapped[bool | None] = mapped_column(Boolean)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
