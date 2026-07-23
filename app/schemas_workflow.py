from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    max_position_pct: Decimal = Field(default=Decimal("20"), gt=0, le=100)
    max_total_position_pct: Decimal = Field(default=Decimal("80"), gt=0, le=100)
    max_industry_position_pct: Decimal = Field(default=Decimal("35"), gt=0, le=100)
    market_state: Literal["上升", "震荡", "下降", "无法判断"] = "无法判断"
    sector_state: Literal["强", "中性", "弱", "无法判断"] = "无法判断"
    logic_invalidation: str | None = Field(default=None, max_length=3000)


class TradePlanSaveRequest(TradePlanPreviewRequest):
    preview_hash: str = Field(min_length=64, max_length=64)
    ai_analysis_id: int | None = None


class EvidenceClaim(BaseModel):
    claim: str = Field(min_length=1, max_length=2000)
    source_ids: list[str] = Field(min_length=1, max_length=20)
    confidence: Literal["high", "medium", "low"]


class TradePlanAIResult(BaseModel):
    company_summary: str = Field(default="", max_length=3000)
    business_drivers: list[str] = Field(default_factory=list, max_length=20)
    financial_findings: list[str] = Field(default_factory=list, max_length=20)
    industry_findings: list[str] = Field(default_factory=list, max_length=20)
    valuation_findings: list[str] = Field(default_factory=list, max_length=20)
    supporting_evidence: list[EvidenceClaim] = Field(default_factory=list, max_length=20)
    counter_evidence: list[EvidenceClaim] = Field(default_factory=list, max_length=20)
    risk_events: list[str] = Field(default_factory=list, max_length=20)
    logic_invalidation_conditions: list[str] = Field(default_factory=list, max_length=20)
    missing_information: list[str] = Field(default_factory=list, max_length=20)
    conflicting_information: list[str] = Field(default_factory=list, max_length=20)
    questions_to_verify: list[str] = Field(default_factory=list, max_length=20)
    plain_language_summary: str = Field(default="", max_length=5000)


class TradePlanAIRequest(TradePlanPreviewRequest):
    preview_hash: str = Field(min_length=64, max_length=64)
