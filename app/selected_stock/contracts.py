from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategyMode(str, Enum):
    PRODUCT_V1 = "PRODUCT_V1"
    CSV_V2_SHADOW = "CSV_V2_SHADOW"
    CSV_V2_ADVISORY = "CSV_V2_ADVISORY"


class DataStatus(str, Enum):
    FRESH = "FRESH"
    STALE_ONE_SESSION = "STALE_ONE_SESSION"
    STALE = "STALE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    CONFLICTED_DATA = "CONFLICTED_DATA"


class ContextStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    BREADTH_UNAVAILABLE = "BREADTH_UNAVAILABLE"
    INDUSTRY_CONTEXT_UNAVAILABLE = "INDUSTRY_CONTEXT_UNAVAILABLE"


class CycleState(str, Enum):
    PREPARATION = "PREPARATION"
    PROBE = "PROBE"
    START_CONFIRMED = "START_CONFIRMED"
    MARKUP = "MARKUP"
    DIVERGENCE = "DIVERGENCE"
    CONCENTRATION = "CONCENTRATION"
    CLIMAX = "CLIMAX"
    DECLINE = "DECLINE"
    TRANSITION = "TRANSITION"
    UNKNOWN = "UNKNOWN"


class StockRole(str, Enum):
    LEADER = "LEADER"
    CAPACITY_CORE = "CAPACITY_CORE"
    BRANCH_CORE = "BRANCH_CORE"
    TREND_CORE = "TREND_CORE"
    ROTATION_FRONT = "ROTATION_FRONT"
    FOLLOWER = "FOLLOWER"
    EVENT_DRIVEN = "EVENT_DRIVEN"
    UNKNOWN = "UNKNOWN"


class TradeMode(str, Enum):
    CORE_TREND_PULLBACK = "CORE_TREND_PULLBACK"
    EARLY_BREAKOUT_CONFIRMATION = "EARLY_BREAKOUT_CONFIRMATION"
    HIGH_LEVEL_DEFENSE = "HIGH_LEVEL_DEFENSE"


class PlanStatus(str, Enum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NO_TRADE = "NO_TRADE"
    WAIT_FOR_TRIGGER = "WAIT_FOR_TRIGGER"
    ENTRY_ALLOWED = "ENTRY_ALLOWED"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"


class CatalystType(str, Enum):
    INDUSTRY_SUPPLY_DEMAND = "INDUSTRY_SUPPLY_DEMAND"
    EARNINGS = "EARNINGS"
    POLICY = "POLICY"
    COMPANY_ANNOUNCEMENT = "COMPANY_ANNOUNCEMENT"
    EVENT = "EVENT"
    RUMOR = "RUMOR"
    UNKNOWN = "UNKNOWN"


class CatalystContext(ContractModel):
    catalyst_type: CatalystType = CatalystType.UNKNOWN
    summary: str = Field(min_length=1, max_length=2000)
    source_reference: str | None = Field(default=None, max_length=1000)


class SelectedStockAnalysisRequest(ContractModel):
    stock_code: str = Field(pattern=r"^\d{6}$")
    analysis_date: date | None = None
    current_price: Decimal | None = Field(default=None, gt=0)
    current_price_observed_at: datetime | None = None
    account_size: Decimal | None = Field(default=None, gt=0)
    current_position_quantity: int | None = Field(default=None, ge=0)
    current_position_pct: Decimal | None = Field(default=None, ge=0, le=100)
    average_cost: Decimal | None = Field(default=None, gt=0)
    available_cash: Decimal | None = Field(default=None, ge=0)
    risk_budget: Decimal | None = Field(default=None, gt=0)
    max_position_pct: Decimal | None = Field(default=None, gt=0, le=100)
    strategy_mode: StrategyMode = StrategyMode.CSV_V2_ADVISORY
    user_focus: str | None = Field(default=None, max_length=2000)
    catalyst_context: CatalystContext | None = None

    @field_validator("current_price_observed_at")
    @classmethod
    def aware_price_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("current_price_observed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def complete_optional_inputs(self):
        if (self.current_price is None) != (self.current_price_observed_at is None):
            raise ValueError(
                "current_price and current_price_observed_at must be provided together"
            )
        if self.available_cash is not None and self.account_size is None:
            raise ValueError("available_cash requires account_size")
        if self.current_position_quantity and self.average_cost is None:
            raise ValueError("current_position_quantity requires average_cost")
        return self


class GateResult(ContractModel):
    code: str
    passed: bool
    reason_code: str
    evidence: tuple[str, ...] = ()


class ScoreComponent(ContractModel):
    score: Decimal = Field(ge=0)
    maximum: Decimal = Field(gt=0)
    reason_codes: tuple[str, ...]
    evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    score_ceiling: Decimal = Field(ge=0)
    confidence: Decimal = Field(ge=0, le=1)


class ScoreCard(ContractModel):
    market_cycle: ScoreComponent
    industry_continuity: ScoreComponent
    stock_role_relative_strength: ScoreComponent
    trend_volume: ScoreComponent
    location_trigger: ScoreComponent
    risk_invalidation: ScoreComponent
    total: Decimal = Field(ge=0, le=100)
    grade: Literal["A", "B", "C", "D"]


class PricePlan(ContractModel):
    support_zone_low: Decimal | None = None
    support_zone_high: Decimal | None = None
    entry_zone_low: Decimal | None = None
    entry_zone_high: Decimal | None = None
    trigger_price: Decimal | None = None
    stop_loss: Decimal | None = None
    first_take_profit: Decimal | None = None
    second_take_profit: Decimal | None = None
    invalidation_price: Decimal | None = None
    risk_reward_ratio: Decimal | None = None


class PositionPlan(ContractModel):
    initial_position_pct: Decimal = Field(ge=0, le=100)
    max_position_pct: Decimal = Field(ge=0, le=100)
    quantity: int | None = Field(default=None, ge=0)
    max_quantity: int | None = Field(default=None, ge=0)
    risk_amount: Decimal | None = Field(default=None, ge=0)
    add_conditions: tuple[str, ...]
    reduce_conditions: tuple[str, ...]


class HoldingPlan(ContractModel):
    unrealized_pnl: Decimal | None
    risk_amount: Decimal | None
    risk_pct: Decimal | None
    allow_hold: bool
    allow_add: bool
    stop_distance_pct: Decimal | None
    maximum_risk_after_add: Decimal | None
    plan_invalidated: bool
    next_session_plan: tuple[str, ...]


class QualityBinding(ContractModel):
    capability: str
    subject_type: str
    subject_id: str
    semantic_key: str
    quality_record_id: int = Field(ge=1)
    quality_status: str
    observed_at: datetime
    normalized_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("observed_at")
    @classmethod
    def aware_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("quality binding observed_at must be aware")
        return value


class SourceLineage(ContractModel):
    capability: str
    provider_id: str
    source: str
    adjustment: str | None = None
    price_unit: str | None = None
    volume_unit: str | None = None
    row_count: int = Field(ge=0)
    observed_at: datetime | None = None
    fetched_at: datetime | None = None
    request_digest: str | None = None
    response_digest: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class StrategyComparison(ContractModel):
    conflict_status: Literal[
        "ALIGNED",
        "MORE_CONSERVATIVE",
        "MORE_AGGRESSIVE",
        "STRUCTURAL_CONFLICT",
        "DATA_CONFLICT",
    ]
    product_v1_status: str
    csv_v2_status: str
    differences: tuple[str, ...]
    formal_execution_owner: Literal["PRODUCT_V1"] = "PRODUCT_V1"


class SelectedStockAnalysisResult(ContractModel):
    analysis_run_id: int | None = None
    strategy_id: Literal["cycle_structure_validation_v2"]
    strategy_version: Literal["2.0.0"]
    strategy_mode: StrategyMode
    stock_code: str
    analysis_date: date
    generated_at: datetime
    data_status: DataStatus
    market_context_status: ContextStatus
    industry_context_status: ContextStatus
    industry_name: str | None
    benchmark_symbol: Literal["CSI000300"] = "CSI000300"
    cycle_state: CycleState
    stock_role: StockRole
    trade_mode: TradeMode
    plan_status: PlanStatus
    executable: bool
    confidence: Decimal = Field(ge=0, le=1)
    hard_gates: tuple[GateResult, ...]
    scores: ScoreCard
    technical_evidence: dict[str, Any]
    passed_conditions: tuple[str, ...]
    failed_conditions: tuple[str, ...]
    pending_conditions: tuple[str, ...]
    price_plan: PricePlan
    position_plan: PositionPlan
    holding_plan: HoldingPlan | None
    invalidation_conditions: tuple[str, ...]
    next_check_condition: tuple[str, ...]
    execution_blockers: tuple[str, ...]
    quality_bindings: tuple[QualityBinding, ...]
    source_lineage: tuple[SourceLineage, ...]
    product_v1_comparison: StrategyComparison
    explanation: dict[str, Any]
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("generated_at")
    @classmethod
    def aware_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def execution_is_advisory_only(self):
        if self.strategy_mode != StrategyMode.PRODUCT_V1 and self.executable:
            raise ValueError("CSV_V2 cannot receive formal execution permission")
        return self


PROGRESS_STATES = (
    "VALIDATING_INPUT",
    "FETCHING_STOCK_HISTORY",
    "FETCHING_INDUSTRY_MAPPING",
    "FETCHING_INDUSTRY_HISTORY",
    "FETCHING_BENCHMARK",
    "CALCULATING_INDICATORS",
    "EVALUATING_HARD_GATES",
    "EVALUATING_CONTEXT",
    "EVALUATING_TRIGGERS",
    "BUILDING_RISK_PLAN",
    "COMPARING_STRATEGIES",
    "COMPLETED",
    "FAILED",
)


__all__ = [
    "CatalystContext",
    "ContextStatus",
    "CycleState",
    "DataStatus",
    "GateResult",
    "HoldingPlan",
    "PlanStatus",
    "PositionPlan",
    "PricePlan",
    "PROGRESS_STATES",
    "QualityBinding",
    "ScoreCard",
    "ScoreComponent",
    "SelectedStockAnalysisRequest",
    "SelectedStockAnalysisResult",
    "SourceLineage",
    "StockRole",
    "StrategyComparison",
    "StrategyMode",
    "TradeMode",
]
