from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.contracts import (
    ConceptChainContext,
    IndustryAssessment,
    MarketRegime,
    TechnicalContext,
)
from app.domain.quality import DataQualityStatus


class DecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BaseRulePlan(DecisionModel):
    rule_status: str
    buy_zone_low: Decimal
    buy_zone_high: Decimal
    hard_stop: Decimal
    final_quantity: int = Field(ge=0)
    trial_quantity: int = Field(ge=0)
    per_share_risk: Decimal = Field(ge=0)
    maximum_loss: Decimal = Field(ge=0)
    base_position_pct: Decimal = Field(ge=0, le=100)
    max_position_pct: Decimal = Field(ge=0, le=100)
    trigger_condition: str
    logical_invalidation: str


class TradeDecisionContext(DecisionModel):
    base_plan: BaseRulePlan
    technical: TechnicalContext
    market: MarketRegime
    industry: IndustryAssessment | None
    concept_chain: ConceptChainContext | None
    data_quality: DataQualityStatus
    announcement_risk: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"]


class BuyPointAssessment(DecisionModel):
    buy_point_type: Literal[
        "BREAKOUT",
        "PULLBACK_CONFIRMATION",
        "DIVERGENCE_TO_CONSENSUS",
        "TREND_CONTINUATION",
        "OVERSOLD_REPAIR_WATCH",
        "NO_VALID_ENTRY",
    ]
    valid: bool
    necessary_conditions: tuple[str, ...]
    confirmation_conditions: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    required_data: tuple[str, ...]
    quality_requirements: tuple[str, ...]
    prohibited_conditions: tuple[str, ...]


class PositionConstraints(DecisionModel):
    constraint_multipliers: dict[str, Decimal]
    effective_constraint: str
    effective_multiplier: Decimal = Field(ge=0, le=1)
    trial_quantity: int = Field(ge=0)
    target_quantity: int = Field(ge=0)
    maximum_quantity: int = Field(ge=0)
    maximum_position_pct: Decimal = Field(ge=0, le=100)
    lot_size: int = 100
    rounding_explanation: str


class RiskPlan(DecisionModel):
    hard_stop: Decimal
    per_share_risk: Decimal = Field(ge=0)
    maximum_plan_loss: Decimal = Field(ge=0)
    logical_invalidation: str
    add_condition: str
    no_add_condition: str


class ExitPlan(DecisionModel):
    first_take_profit: str
    second_take_profit: str
    trailing_stop: str
    reduce_condition: str
    exit_condition: str
    no_trade_condition: str


class TradeDecisionResult(DecisionModel):
    rule_status: str
    executable_status: Literal["READY", "WAIT"]
    buy_allowed: bool
    blocked_reasons: tuple[str, ...]
    buy_zone: tuple[Decimal, Decimal]
    trigger_condition: str
    buy_point_assessment: BuyPointAssessment
    position_constraints: PositionConstraints
    risk_plan: RiskPlan
    exit_plan: ExitPlan
    next_session_observations: tuple[str, ...]


__all__ = [
    "BaseRulePlan",
    "BuyPointAssessment",
    "ExitPlan",
    "PositionConstraints",
    "RiskPlan",
    "TradeDecisionContext",
    "TradeDecisionResult",
]
