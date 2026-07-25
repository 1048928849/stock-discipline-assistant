from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class TradePlanCreate(BaseModel):
    account_id: int
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str | None = Field(default=None, max_length=100)
    trade_mode: Literal["日线趋势波段", "短线", "中长线"] = "日线趋势波段"
    decision_level: Literal["日线", "周线", "月线", "60分钟"] = "日线"
    market_state: Literal["上升", "震荡", "下降", "无法判断"] | None = None
    sector_state: Literal["强", "中性", "弱", "无法判断"] | None = None
    large_cycle_direction: Literal["向上", "震荡", "向下", "无法判断"] | None = None
    industry_logic: str | None = Field(default=None, max_length=5000)
    company_logic: str | None = Field(default=None, max_length=5000)
    technical_structure: str | None = Field(default=None, max_length=5000)
    buy_zone_low: Decimal = Field(gt=0)
    buy_zone_high: Decimal = Field(gt=0)
    initial_stop: Decimal = Field(gt=0)
    invalidation_condition: str = Field(min_length=1, max_length=5000)
    target_plan: str | None = Field(default=None, max_length=5000)
    risk_pct: Decimal = Field(default=Decimal("1"), gt=0, le=10)
    max_position_pct: Decimal = Field(default=Decimal("25"), gt=0, le=100)
    add_condition: str | None = Field(default=None, max_length=5000)
    reduce_condition: str | None = Field(default=None, max_length=5000)
    exit_condition: str | None = Field(default=None, max_length=5000)
    no_trade_condition: str | None = Field(default=None, max_length=5000)
    data_date: date = Field(default_factory=date.today)

    @model_validator(mode="after")
    def validate_prices(self):
        if self.buy_zone_high < self.buy_zone_low:
            raise ValueError("计划买入区上限不能低于下限")
        if self.initial_stop >= self.buy_zone_low:
            raise ValueError("初始止损必须低于计划买入区下限")
        return self


class PositionAssessment(BaseModel):
    stage: Literal["观察", "试错", "确认", "趋势", "转弱"] | None = None
    logic_status: Literal["成立", "部分成立", "不成立", "无法判断"] = "无法判断"
    invalidation_triggered: bool | None = None
    supporting_evidence: list[str] = Field(default_factory=list, max_length=20)
    opposing_evidence: list[str] = Field(default_factory=list, max_length=20)
    next_action: str | None = Field(default=None, max_length=2000)


class RuleVersionUpdate(BaseModel):
    single_trade_risk_pct: Decimal = Field(ge=Decimal("0.1"), le=Decimal("10"))
    max_single_position_pct: Decimal = Field(ge=Decimal("1"), le=Decimal("100"))
    max_account_drawdown_pct: Decimal = Field(ge=Decimal("1"), le=Decimal("50"))
    beginner_min_holdings: int = Field(ge=1, le=20)
    beginner_max_holdings: int = Field(ge=1, le=50)

    @model_validator(mode="after")
    def validate_range(self):
        if self.beginner_min_holdings > self.beginner_max_holdings:
            raise ValueError("最少持仓数不能大于最多持仓数")
        return self


class TradePlanPreviewRequest(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    account_id: int
    trade_mode: Literal["日线趋势波段"] = "日线趋势波段"
    risk_pct: Decimal = Field(default=Decimal("0.5"), gt=0, le=10)
    max_position_pct: Decimal = Field(default=Decimal("30"), gt=0, le=100)
    max_total_position_pct: Decimal = Field(default=Decimal("80"), gt=0, le=100)
    max_industry_position_pct: Decimal = Field(default=Decimal("35"), gt=0, le=100)
    market_state: Literal["上升", "震荡", "下降", "无法判断"] = "无法判断"
    sector_state: Literal["强", "中性", "弱", "无法判断"] = "无法判断"
    logic_invalidation: str | None = Field(default=None, max_length=3000)
    position_mode: Literal["空仓", "持仓"] | None = None
    holding_quantity: int | None = Field(default=None, ge=100)
    holding_cost_price: Decimal | None = Field(default=None, gt=0)
    market_evidence: str | None = Field(default=None, max_length=2000)
    market_source: str | None = Field(default=None, max_length=200)
    market_data_time: str | None = Field(default=None, max_length=80)
    sector_evidence: str | None = Field(default=None, max_length=2000)
    sector_source: str | None = Field(default=None, max_length=200)
    sector_data_time: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def validate_position_input(self):
        if self.position_mode == "持仓" and (
            self.holding_quantity is None or self.holding_cost_price is None
        ):
            raise ValueError("选择已经持有时必须填写持仓数量和持仓成本")
        return self


class TradePlanSaveRequest(TradePlanPreviewRequest):
    preview_hash: str = Field(min_length=64, max_length=64)
    ai_analysis_id: int | None = None


class EvidenceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(
        min_length=1,
        max_length=20,
        validation_alias=AliasChoices("evidence_ids", "source_ids"),
    )
    confidence: Literal["high", "medium", "low"]


class RawFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(
        min_length=1,
        max_length=20,
        validation_alias=AliasChoices("evidence_ids", "source_ids"),
    )
    as_of: str | None = Field(default=None, max_length=40)
    confidence: Literal["high", "medium", "low"]


class RuleConclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=80)
    status: str = Field(min_length=1, max_length=40)
    conclusion: str = Field(min_length=1, max_length=1000)
    basis: str = Field(min_length=1, max_length=2000)


class AIStatement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=3000)
    evidence_ids: list[str] = Field(
        default_factory=list,
        max_length=20,
        validation_alias=AliasChoices("evidence_ids", "source_ids"),
    )
    confidence: Literal["high", "medium", "low"]


class InformationConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(
        min_length=2,
        max_length=20,
        validation_alias=AliasChoices("evidence_ids", "source_ids"),
    )


class DataFreshnessItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(min_length=1, max_length=80)
    latest_at: str | None = Field(default=None, max_length=40)
    stale: bool
    evidence_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        validation_alias=AliasChoices("evidence_ids", "source_ids"),
    )


class ProviderStatusItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=1, max_length=80)
    status: str = Field(min_length=1, max_length=40)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    message: str | None = Field(default=None, max_length=1000)


class TradePlanAIResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["2.0"]
    computed_results: dict
    raw_facts: list[RawFact] = Field(default_factory=list, max_length=80)
    rule_conclusions: list[RuleConclusion] = Field(default_factory=list, max_length=30)
    ai_summaries: list[AIStatement] = Field(default_factory=list, max_length=20)
    ai_inferences: list[AIStatement] = Field(default_factory=list, max_length=20)
    supporting_evidence: list[EvidenceClaim] = Field(default_factory=list, max_length=20)
    opposing_evidence: list[EvidenceClaim] = Field(default_factory=list, max_length=20)
    conflicts: list[InformationConflict] = Field(default_factory=list, max_length=20)
    missing_data: list[str] = Field(default_factory=list, max_length=50)
    risk_events: list[EvidenceClaim] = Field(default_factory=list, max_length=20)
    invalidation_conditions: list[EvidenceClaim] = Field(default_factory=list, max_length=20)
    data_freshness: list[DataFreshnessItem] = Field(default_factory=list, max_length=30)
    provider_status: list[ProviderStatusItem] = Field(default_factory=list, max_length=30)


class TradePlanAIRequest(TradePlanPreviewRequest):
    preview_hash: str = Field(min_length=64, max_length=64)


class OneClickPlanRequest(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    position_mode: Literal["空仓", "持仓"] = "空仓"
    account_id: int | None = None
    plan_capital: Decimal | None = Field(default=Decimal("300000"), gt=0)
    available_cash: Decimal | None = Field(default=None, ge=0)
    holding_quantity: int | None = Field(default=None, ge=100)
    holding_cost_price: Decimal | None = Field(default=None, gt=0)
    refresh: bool = False
    enable_ai: bool = True
    risk_pct: Decimal = Field(default=Decimal("0.5"), gt=0, le=10)
    max_position_pct: Decimal = Field(default=Decimal("30"), gt=0, le=100)
    max_total_position_pct: Decimal = Field(default=Decimal("80"), gt=0, le=100)
    max_industry_position_pct: Decimal = Field(default=Decimal("40"), gt=0, le=100)
    logic_invalidation: str | None = Field(default=None, max_length=3000)
    required_research_capabilities: list[
        Literal["financials", "valuation", "news", "social_clues"]
    ] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_one_click_input(self):
        if self.position_mode == "持仓" and (
            self.holding_quantity is None or self.holding_cost_price is None
        ):
            raise ValueError("选择已经持有时必须填写持仓数量和持仓成本")
        if self.available_cash is not None and self.plan_capital is not None:
            if self.available_cash > self.plan_capital:
                raise ValueError("可用资金不能大于本次计划资金")
        return self


class OneClickConfirmRequest(BaseModel):
    user_confirmed: bool = True


class PlanExecutionFillCreate(BaseModel):
    side: Literal["买入", "卖出"]
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(default=Decimal("0"), ge=0)
    executed_at: datetime = Field(default_factory=datetime.now)
    reason: Literal["首次试仓", "确认加仓", "减仓", "止盈", "硬止损", "逻辑退出", "其他"]
    trigger_confirmed: bool | None = None
    is_test: bool = False
    notes: str | None = Field(default=None, max_length=2000)


class PlanExecutionEvaluate(BaseModel):
    current_price: Decimal | None = Field(default=None, gt=0)
    entry_triggered: bool = False
    add_triggered: bool = False
    reduce_triggered: bool = False
    stop_triggered: bool = False
    take_profit_triggered: bool = False
    invalidated: bool = False
    evidence: str | None = Field(default=None, max_length=3000)
