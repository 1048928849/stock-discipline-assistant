from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.dialects.mysql import VARCHAR as MYSQL_VARCHAR
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.database import Base
from app.domain.quality_subject import canonical_semantic_key


MONEY = Numeric(20, 4)
PRICE = Numeric(18, 4)
PRECISE_DATETIME = DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")
ANNOUNCEMENT_URL = String(1000).with_variant(
    MYSQL_VARCHAR(1000, charset="ascii", collation="ascii_bin"),
    "mysql",
)


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
    quote_type: Mapped[str] = mapped_column(String(20), default="realtime")
    observed_at: Mapped[datetime] = mapped_column(
        PRECISE_DATETIME, default=datetime.now
    )
    price_unit: Mapped[str] = mapped_column(String(20), default="CNY")
    quality_status: Mapped[str] = mapped_column(String(20), default="SINGLE_SOURCE")
    quality_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_quality_records.id"), index=True
    )
    source: Mapped[str] = mapped_column(String(50))
    source_api: Mapped[str] = mapped_column(String(100))
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


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
    adjustment: Mapped[str] = mapped_column(String(20), default="qfq")
    price_unit: Mapped[str] = mapped_column(String(20), default="CNY")
    volume_unit: Mapped[str] = mapped_column(String(20), default="share")
    observed_at: Mapped[datetime] = mapped_column(
        PRECISE_DATETIME, default=datetime.now
    )
    quality_status: Mapped[str] = mapped_column(String(20), default="SINGLE_SOURCE")
    quality_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_quality_records.id"), index=True
    )
    source: Mapped[str] = mapped_column(String(50))
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class MarketIntradayBar(Base):
    __tablename__ = "market_intraday_bars"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "bar_start", "adjustment", name="uq_intraday_symbol_start_adjustment"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    bar_start: Mapped[datetime] = mapped_column(PRECISE_DATETIME, index=True)
    bar_end: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    open: Mapped[Decimal] = mapped_column(PRICE)
    high: Mapped[Decimal] = mapped_column(PRICE)
    low: Mapped[Decimal] = mapped_column(PRICE)
    close: Mapped[Decimal] = mapped_column(PRICE)
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    turnover_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    adjustment: Mapped[str] = mapped_column(String(20), default="qfq")
    price_unit: Mapped[str] = mapped_column(String(20), default="CNY")
    volume_unit: Mapped[str] = mapped_column(String(20), default="share")
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    source: Mapped[str] = mapped_column(String(80))
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class MarketTurnoverSnapshot(Base):
    __tablename__ = "market_turnover_snapshots"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_turnover_symbol_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    turnover_rate: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    source: Mapped[str] = mapped_column(String(80))
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class MarketBreadthSnapshot(Base):
    __tablename__ = "market_breadth_snapshots"
    __table_args__ = (
        UniqueConstraint("market_id", "trade_date", name="uq_breadth_market_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[str] = mapped_column(String(20), default="CN-A")
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    advancing: Mapped[int] = mapped_column(Integer)
    declining: Mapped[int] = mapped_column(Integer)
    unchanged: Mapped[int] = mapped_column(Integer)
    limit_up: Mapped[int] = mapped_column(Integer)
    limit_down: Mapped[int] = mapped_column(Integer)
    new_highs: Mapped[int | None] = mapped_column(Integer)
    new_lows: Mapped[int | None] = mapped_column(Integer)
    median_change_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    above_ma20_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    above_ma50_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    source: Mapped[str] = mapped_column(String(80))
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class MarketAmountSnapshot(Base):
    __tablename__ = "market_amount_snapshots"
    __table_args__ = (
        UniqueConstraint("market_id", "trade_date", name="uq_amount_market_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[str] = mapped_column(String(20), default="CN-A")
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    source: Mapped[str] = mapped_column(String(80))
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class IndustryMarketSnapshot(Base):
    __tablename__ = "industry_market_snapshots"
    __table_args__ = (
        UniqueConstraint("industry_key", "trade_date", name="uq_industry_market_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    industry_key: Mapped[str] = mapped_column(String(20), index=True)
    industry_name: Mapped[str] = mapped_column(String(200), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    amount_share: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    advance_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    limit_up_count: Mapped[int | None] = mapped_column(Integer)
    leader_strength: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    new_high_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    source: Mapped[str] = mapped_column(String(80))
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class IndustryConstituentSnapshot(Base):
    __tablename__ = "industry_constituent_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "industry_key", "symbol", "snapshot_date", name="uq_industry_member_date"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    industry_key: Mapped[str] = mapped_column(String(20), index=True)
    industry_name: Mapped[str] = mapped_column(String(200), index=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str] = mapped_column(String(100))
    weight: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latest_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    high_52w: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    is_new_high: Mapped[bool | None] = mapped_column(Boolean)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    source: Mapped[str] = mapped_column(String(80))
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class IndustryTaxonomyBinding(Base):
    __tablename__ = "industry_taxonomy_bindings"
    __table_args__ = (
        UniqueConstraint(
            "quality_record_id",
            "provider_industry_id",
            name="uq_industry_taxonomy_binding_identity",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    classification_system: Mapped[str] = mapped_column(String(40), index=True)
    provider_id: Mapped[str] = mapped_column(String(80), index=True)
    provider_industry_id: Mapped[str] = mapped_column(String(40), index=True)
    provider_industry_code: Mapped[str | None] = mapped_column(String(40))
    provider_industry_name: Mapped[str] = mapped_column(String(200), index=True)
    level: Mapped[str] = mapped_column(String(40))
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    source_reference: Mapped[str] = mapped_column(String(500))
    response_digest: Mapped[str] = mapped_column(String(64))
    membership_evidence: Mapped[str] = mapped_column(Text)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class IndustryCapitalFlowSnapshot(Base):
    __tablename__ = "industry_capital_flow_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "industry_key", "trade_date", name="uq_industry_capital_flow_date"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    industry_key: Mapped[str] = mapped_column(String(40), index=True)
    industry_name: Mapped[str] = mapped_column(String(200), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    net_inflow_1d: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    net_inflow_5d: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    net_inflow_10d: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    amount_unit: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(100))
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class MarketEventPoolSnapshot(Base):
    __tablename__ = "market_event_pool_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "trade_date", "event_type", "symbol", name="uq_market_event_pool_symbol"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str] = mapped_column(String(100))
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    event_type: Mapped[str] = mapped_column(String(20), index=True)
    first_event_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    last_event_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    sealed_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    sealed_amount_unit: Mapped[str] = mapped_column(String(10))
    turnover_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    turnover_rate_unit: Mapped[str] = mapped_column(String(20))
    consecutive_days: Mapped[int] = mapped_column(Integer)
    industry_name: Mapped[str | None] = mapped_column(String(200), index=True)
    reason_summary: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(100))
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class MarketRegimeSnapshot(Base):
    __tablename__ = "market_regime_snapshots"
    __table_args__ = (
        UniqueConstraint("market_id", "trade_date", name="uq_market_regime_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[str] = mapped_column(String(20), default="CN-A")
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    state: Mapped[str] = mapped_column(String(20), index=True)
    previous_state: Mapped[str] = mapped_column(String(20))
    transition: Mapped[str] = mapped_column(String(50))
    product_snapshot_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_status: Mapped[str | None] = mapped_column(String(20))
    quality_bindings: Mapped[list] = mapped_column(JSON, default=list)


class IndustryAnalysisSnapshot(Base):
    __tablename__ = "industry_analysis_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "industry_name", "trade_date", name="uq_industry_analysis_name_date"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    industry_name: Mapped[str] = mapped_column(String(200), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    classification: Mapped[str] = mapped_column(String(20), index=True)
    product_snapshot_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_status: Mapped[str] = mapped_column(String(20))
    quality_bindings: Mapped[list] = mapped_column(JSON, default=list)


class Concept(Base):
    __tablename__ = "concepts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    source: Mapped[str] = mapped_column(String(80))
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class CompanyConcept(Base):
    __tablename__ = "company_concepts"
    __table_args__ = (
        UniqueConstraint("symbol", "concept_id", name="uq_company_concept"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    concept_id: Mapped[int] = mapped_column(ForeignKey("concepts.id"), index=True)
    relevance: Mapped[str] = mapped_column(String(40))
    evidence_summary: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class IndustryChain(Base):
    __tablename__ = "industry_chains"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    source: Mapped[str] = mapped_column(String(80))
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class IndustryChainNode(Base):
    __tablename__ = "industry_chain_nodes"
    __table_args__ = (
        UniqueConstraint("chain_id", "name", name="uq_industry_chain_node"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    stage: Mapped[str] = mapped_column(String(40))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class CompanyChainPosition(Base):
    __tablename__ = "company_chain_positions"
    __table_args__ = (
        UniqueConstraint("symbol", "node_id", name="uq_company_chain_position"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("industry_chain_nodes.id"), index=True)
    relevance: Mapped[str] = mapped_column(String(40))
    primary_products: Mapped[list | None] = mapped_column(JSON)
    revenue_relevance: Mapped[str] = mapped_column(String(40), default="unknown")
    core_level: Mapped[str | None] = mapped_column(String(40))
    substitutability: Mapped[str | None] = mapped_column(String(40))
    competitive_position: Mapped[str | None] = mapped_column(String(80))
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class MappingEvidence(Base):
    __tablename__ = "mapping_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    evidence_key: Mapped[str] = mapped_column(String(64), unique=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    mapping_type: Mapped[str] = mapped_column(String(30), index=True)
    source_name: Mapped[str] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    excerpt: Mapped[str] = mapped_column(Text)
    raw_data: Mapped[dict | None] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=False, index=True
    )


class MarketSourceLog(Base):
    __tablename__ = "market_source_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(50))
    api_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    error: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer, default=0)


class DataQualityRecord(Base):
    """Immutable quality decision for a provider refresh or persisted cache."""

    __tablename__ = "data_quality_records"
    __table_args__ = (
        Index(
            "ix_data_quality_records_subject_scope",
            "capability",
            "subject_type",
            "subject_id",
            "semantic_key",
            "id",
        ),
        Index(
            "ix_data_quality_records_supersedes_record_id",
            "supersedes_record_id",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(40), index=True)
    capability: Mapped[str] = mapped_column(String(80), index=True)
    subject_type: Mapped[str | None] = mapped_column(String(20))
    subject_id: Mapped[str | None] = mapped_column(String(160))
    semantic_key: Mapped[str | None] = mapped_column(String(200), default="")
    supersedes_record_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "data_quality_records.id",
            name="fk_data_quality_records_supersedes_record",
            use_alter=True,
        )
    )
    quality_status: Mapped[str] = mapped_column(String(20), index=True)
    observed_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    cached_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    provider_id: Mapped[str] = mapped_column(String(80))
    provider_observations: Mapped[list] = mapped_column(JSON, default=list)
    normalized_digest: Mapped[str | None] = mapped_column(String(64))
    conflict_fields: Mapped[list] = mapped_column(JSON, default=list)
    adjustment: Mapped[str | None] = mapped_column(String(20))
    price_unit: Mapped[str | None] = mapped_column(String(20))
    volume_unit: Mapped[str | None] = mapped_column(String(20))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    cache_used: Mapped[bool] = mapped_column(Boolean, default=False)
    trusted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    persisted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    scan_start: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    scan_end: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    checked_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    latest_content_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    created_at: Mapped[datetime] = mapped_column(
        PRECISE_DATETIME, server_default=func.now()
    )

    @validates("semantic_key")
    def normalize_semantic_key(self, _key: str, value: str | None) -> str:
        return canonical_semantic_key(value)


class DataQualitySubjectHead(Base):
    __tablename__ = "data_quality_subject_heads"
    __table_args__ = (
        UniqueConstraint(
            "capability",
            "subject_type",
            "subject_id",
            "semantic_key",
            name="uq_data_quality_subject_head_scope",
        ),
        Index(
            "ix_data_quality_subject_heads_scope_generation",
            "capability",
            "subject_type",
            "subject_id",
            "semantic_key",
            "generation",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    capability: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(160), nullable=False)
    semantic_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    current_record_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_records.id", name="fk_quality_subject_head_current_record"),
        nullable=False,
        index=True,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        PRECISE_DATETIME,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    @validates("semantic_key")
    def normalize_semantic_key(self, _key: str, value: str | None) -> str:
        return canonical_semantic_key(value)


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
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    quality_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=True, index=True
    )


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
    fetched_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


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
    url: Mapped[str] = mapped_column(ANNOUNCEMENT_URL)
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
    last_attempt_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    last_success_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    data_date: Mapped[date | None] = mapped_column(Date)
    stale_after: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    quality_status: Mapped[str | None] = mapped_column(String(20))
    observed_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    fetched_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    checked_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    scan_start: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    scan_end: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    normalized_digest: Mapped[str | None] = mapped_column(String(64))
    provider_observations: Mapped[list | None] = mapped_column(JSON)
    conflict_fields: Mapped[list | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    quality_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_quality_records.id"), nullable=True, index=True
    )


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
    __table_args__ = (
        UniqueConstraint("analysis_run_id", name="uq_trade_plan_analysis_run"),
        UniqueConstraint(
            "account_id",
            "symbol",
            "plan_version",
            name="uq_trade_plan_account_symbol_version",
        ),
    )
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
    analysis_run_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "plan_analysis_runs.id",
            name="fk_trade_plans_analysis_run",
            use_alter=True,
        ),
        index=True,
    )
    # SQLite 无法在不重建既有计划表的情况下添加自关联约束；服务层校验父版本。
    parent_plan_id: Mapped[int | None] = mapped_column(Integer)
    preview_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    engine_snapshot: Mapped[dict | None] = mapped_column(JSON)
    market_snapshot: Mapped[dict | None] = mapped_column(JSON)
    account_snapshot: Mapped[dict | None] = mapped_column(JSON)
    source_snapshot: Mapped[list | None] = mapped_column(JSON)
    execution_status: Mapped[str] = mapped_column(String(30), index=True, default="draft")
    execution_summary: Mapped[dict | None] = mapped_column(JSON)


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


class SelectedStockAnalysisRun(Base):
    """Immutable CSV_V2 advisory snapshot for one selected stock."""

    __tablename__ = "selected_stock_analysis_runs"
    __table_args__ = (
        UniqueConstraint(
            "analysis_identity_hash",
            name="uq_selected_stock_analysis_identity",
        ),
        Index(
            "ix_selected_stock_symbol_date",
            "symbol",
            "analysis_date",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    analysis_date: Mapped[date] = mapped_column(Date, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(30), nullable=False)
    strategy_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    quality_bindings: Mapped[list] = mapped_column(JSON, nullable=False)
    source_lineage: Mapped[list] = mapped_column(JSON, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, nullable=False)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, nullable=False)


class WatchlistItem(TimestampMixin, Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint(
            "source_type", "source_reference", name="uq_watchlist_source_reference"
        ),
        Index("ix_watchlist_account_symbol", "account_id", "symbol"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str | None] = mapped_column(String(100))
    market: Mapped[str] = mapped_column(String(10), default="CN")
    status: Mapped[str] = mapped_column(String(30), index=True)
    monitoring_health: Mapped[str] = mapped_column(String(30), index=True)
    source_type: Mapped[str] = mapped_column(String(30))
    source_reference: Mapped[str] = mapped_column(String(100))
    thesis: Mapped[str] = mapped_column(Text)
    strategy_id: Mapped[str | None] = mapped_column(String(64))
    strategy_version: Mapped[str | None] = mapped_column(String(40))
    strategy_implementation_hash: Mapped[str | None] = mapped_column(String(64))
    strategy_parameter_hash: Mapped[str | None] = mapped_column(String(64))
    strategy_signal_hash: Mapped[str | None] = mapped_column(String(64))
    strategy_binding_hash: Mapped[str | None] = mapped_column(String(64))
    analysis_capital: Mapped[Decimal] = mapped_column(MONEY)
    entry_low: Mapped[Decimal | None] = mapped_column(PRICE)
    entry_high: Mapped[Decimal | None] = mapped_column(PRICE)
    hard_stop: Mapped[Decimal | None] = mapped_column(PRICE)
    waiting_conditions: Mapped[list] = mapped_column(JSON, default=list)
    invalidation_conditions: Mapped[list] = mapped_column(JSON, default=list)
    invalidation_rule_specs: Mapped[list] = mapped_column(JSON, default=list)
    latest_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    latest_package_hash: Mapped[str | None] = mapped_column(String(64))
    latest_analysis_id: Mapped[int | None] = mapped_column(
        ForeignKey("plan_analysis_runs.id"), index=True
    )
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    current_price: Mapped[Decimal | None] = mapped_column(PRICE)
    current_price_observed_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    market_state: Mapped[str | None] = mapped_column(String(30))
    industry_name: Mapped[str | None] = mapped_column(String(200), index=True)
    industry_state: Mapped[str | None] = mapped_column(String(30))
    data_quality: Mapped[str | None] = mapped_column(String(20))
    last_analyzed_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    last_scanned_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    next_scan_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class WatchlistRevision(Base):
    __tablename__ = "watchlist_revisions"
    __table_args__ = (
        UniqueConstraint(
            "watchlist_item_id",
            "revision_number",
            name="uq_watchlist_revision_item_number",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    watchlist_item_id: Mapped[int] = mapped_column(
        ForeignKey("watchlist_items.id", ondelete="CASCADE"), index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer)
    previous_revision_number: Mapped[int | None] = mapped_column(Integer)
    change_reason: Mapped[str] = mapped_column(String(100))
    changed_by: Mapped[str] = mapped_column(String(50))
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class WatchlistTransition(Base):
    __tablename__ = "watchlist_transitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    watchlist_item_id: Mapped[int] = mapped_column(
        ForeignKey("watchlist_items.id", ondelete="CASCADE"), index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer)
    from_status: Mapped[str] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30), index=True)
    reason_codes: Mapped[list] = mapped_column(JSON)
    evidence_references: Mapped[list] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, index=True)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class ReanalysisRequest(Base):
    __tablename__ = "reanalysis_requests"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_reanalysis_request_dedupe"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    watchlist_item_id: Mapped[int] = mapped_column(
        ForeignKey("watchlist_items.id", ondelete="CASCADE"), index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer)
    reason_codes: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), index=True, default="PENDING")
    dedupe_key: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, index=True)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class ReanalysisRun(Base):
    __tablename__ = "reanalysis_runs"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_reanalysis_run_request"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("reanalysis_requests.id", ondelete="CASCADE"), index=True
    )
    watchlist_item_id: Mapped[int] = mapped_column(
        ForeignKey("watchlist_items.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), index=True)
    analysis_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("plan_analysis_runs.id"), index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    finished_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class CandidateDiscoveryRun(Base):
    __tablename__ = "candidate_discovery_runs"
    __table_args__ = (
        UniqueConstraint(
            "market",
            "trade_date",
            "algorithm_version",
            "config_hash",
            name="uq_candidate_discovery_run_identity",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(10), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    algorithm_id: Mapped[str] = mapped_column(String(64))
    algorithm_version: Mapped[str] = mapped_column(String(20))
    config_hash: Mapped[str] = mapped_column(String(64))
    input_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    market_state: Mapped[str | None] = mapped_column(String(20))
    quality_status: Mapped[str] = mapped_column(String(20))
    blocked_reasons: Mapped[list] = mapped_column(JSON, default=list)
    quality_bindings: Mapped[list] = mapped_column(JSON, default=list)
    industries_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    candidates_generated: Mapped[int] = mapped_column(Integer, default=0)
    total_constituents: Mapped[int] = mapped_column(Integer, default=0)
    historical_data_ready: Mapped[int] = mapped_column(Integer, default=0)
    historical_data_missing: Mapped[int] = mapped_column(Integer, default=0)
    coverage_ratio: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    started_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    completed_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class CandidateIndustryAssessment(Base):
    __tablename__ = "candidate_industry_assessments"
    __table_args__ = (
        UniqueConstraint(
            "discovery_run_id", "industry_key", name="uq_candidate_industry_run"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    discovery_run_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_discovery_runs.id", ondelete="CASCADE"), index=True
    )
    industry_key: Mapped[str] = mapped_column(String(40), index=True)
    industry_name: Mapped[str] = mapped_column(String(200), index=True)
    classification: Mapped[str] = mapped_column(String(20), index=True)
    score: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    rank: Mapped[int | None] = mapped_column(Integer)
    metrics: Mapped[dict] = mapped_column(JSON)
    reason_codes: Mapped[list] = mapped_column(JSON)
    evidence_references: Mapped[list] = mapped_column(JSON)
    quality_status: Mapped[str] = mapped_column(String(20))


class DiscoveryCandidate(Base):
    __tablename__ = "discovery_candidates"
    __table_args__ = (
        UniqueConstraint(
            "discovery_run_id", "symbol", name="uq_discovery_candidate_run_symbol"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    discovery_run_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_discovery_runs.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str] = mapped_column(String(100))
    industry_key: Mapped[str] = mapped_column(String(40), index=True)
    industry_name: Mapped[str] = mapped_column(String(200), index=True)
    candidate_type: Mapped[str] = mapped_column(String(30), index=True)
    score: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    rank: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), index=True)
    current_price: Mapped[Decimal] = mapped_column(PRICE)
    technical_metrics: Mapped[dict] = mapped_column(JSON)
    reason_codes: Mapped[list] = mapped_column(JSON)
    risk_flags: Mapped[list] = mapped_column(JSON)
    evidence_references: Mapped[list] = mapped_column(JSON)
    quality_status: Mapped[str] = mapped_column(String(20))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, index=True)
    promoted_watchlist_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("watchlist_items.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    updated_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class HistoricalDataBootstrapRun(Base):
    __tablename__ = "historical_data_bootstrap_runs"
    __table_args__ = (
        UniqueConstraint(
            "purpose",
            "market",
            "trade_date",
            "plan_hash",
            name="uq_history_bootstrap_run_identity",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(40), index=True)
    market: Mapped[str] = mapped_column(String(10), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    provider_id: Mapped[str] = mapped_column(String(80))
    adapter_version: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    plan_hash: Mapped[str] = mapped_column(String(64))
    required_symbols: Mapped[list] = mapped_column(JSON, default=list)
    ready_symbols: Mapped[int] = mapped_column(Integer, default=0)
    failed_symbols: Mapped[int] = mapped_column(Integer, default=0)
    benchmark_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    total_rows_written: Mapped[int] = mapped_column(Integer, default=0)
    coverage_ratio: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    amount_coverage_ratio: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    started_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    completed_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    blocked_reasons: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


class HistoricalDataBootstrapItem(Base):
    __tablename__ = "historical_data_bootstrap_items"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "symbol",
            "capability",
            name="uq_history_bootstrap_item_scope",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("historical_data_bootstrap_runs.id", ondelete="CASCADE"),
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    capability: Mapped[str] = mapped_column(String(80), index=True)
    adjustment: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), index=True)
    requested_start: Mapped[date] = mapped_column(Date)
    requested_end: Mapped[date] = mapped_column(Date)
    rows_received: Mapped[int] = mapped_column(Integer, default=0)
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    first_trade_date: Mapped[date | None] = mapped_column(Date)
    last_trade_date: Mapped[date | None] = mapped_column(Date)
    quality_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_quality_records.id"), index=True
    )
    normalized_digest: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    completed_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)


class MonitoringEvent(Base):
    __tablename__ = "monitoring_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_monitoring_event_dedupe"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    watchlist_item_id: Mapped[int] = mapped_column(
        ForeignKey("watchlist_items.id", ondelete="CASCADE"), index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(String(300))
    reason_codes: Mapped[list] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME, index=True)
    created_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)
    dedupe_key: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    acknowledged_at: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME)
    reanalysis_required: Mapped[bool] = mapped_column(Boolean, default=False)
    reanalysis_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("reanalysis_runs.id"), index=True
    )


class WatchlistMonitorLease(Base):
    __tablename__ = "watchlist_monitor_leases"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    owner_token: Mapped[str | None] = mapped_column(String(64), index=True)
    acquired_until: Mapped[datetime | None] = mapped_column(PRECISE_DATETIME, index=True)
    lease_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(PRECISE_DATETIME)


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
