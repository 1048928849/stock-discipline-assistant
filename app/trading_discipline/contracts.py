from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketStage(str, Enum):
    DORMANT = "DORMANT"
    STATE_CHANGE = "STATE_CHANGE"
    WATCHING = "WATCHING"
    SECOND_CONFIRMATION = "SECOND_CONFIRMATION"
    FERMENTATION = "FERMENTATION"
    ACCELERATION = "ACCELERATION"
    DIVERGENCE = "DIVERGENCE"
    WEAKENING = "WEAKENING"
    CLIMAX = "CLIMAX"
    FADING = "FADING"
    INVALIDATED = "INVALIDATED"
    UNKNOWN = "UNKNOWN"


class DetectionStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    CANDIDATE = "CANDIDATE"
    NOT_PRESENT = "NOT_PRESENT"
    NOT_EVALUATED = "NOT_EVALUATED"


class GateStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"
    NOT_EVALUATED = "NOT_EVALUATED"


class ProposedAction(str, Enum):
    BUY = "BUY"
    ADD = "ADD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"


class EvidenceTier(str, Enum):
    FACT = "FACT"
    ANALYSIS = "ANALYSIS"
    SENTIMENT = "SENTIMENT"


class StressStatus(str, Enum):
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class PriceImpactEfficiency(str, Enum):
    IMPROVING = "IMPROVING"
    STABLE = "STABLE"
    DECLINING = "DECLINING"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class DivergenceAssessment(str, Enum):
    STRONG_DIVERGENCE = "STRONG_DIVERGENCE"
    WEAKENING = "WEAKENING"
    UNKNOWN = "UNKNOWN"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class RuleEvaluation(Contract):
    rule_code: str
    status: GateStatus
    required_inputs: list[str] = Field(default_factory=list)
    evaluated_inputs: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    threshold: Any = None
    actual_value: Any = None
    reason_code: str
    effect_on_action: str
    source_refs: list[str] = Field(default_factory=list)


class StageEvidence(Contract):
    as_of: date
    prior_base: bool | None = None
    above_ma_cluster: bool | None = None
    breakout: bool | None = None
    volume_ratio: Decimal | None = None
    close_position: Decimal | None = None
    relative_strength_change: Decimal | None = None
    abnormal_return: bool | None = None
    industry_participation: bool | None = None
    sessions_since_state_change: int | None = None
    held_breakout: bool | None = None
    higher_low: bool | None = None
    distance_from_structure_pct: Decimal | None = None


class StageAssessment(Contract):
    stage: MarketStage
    result: DetectionStatus
    evidence_groups: list[str]
    reason_codes: list[str]
    as_of: date
    rule_version: str = "CORE_STATE_CHANGE_V1.0.0"


class PreTradeContext(Contract):
    account_id: int
    symbol: str
    action: ProposedAction
    decision_at: datetime
    playbook_code: str | None = None
    playbook_selected_at: datetime | None = None
    entry_evidence_at: datetime | None = None
    invalidation_defined_at: datetime | None = None
    hard_stop: Decimal | None = None
    intended_quantity: int | None = None
    proposed_quantity: int
    current_quantity: int = 0
    cost_price: Decimal | None = None
    current_price: Decimal
    planned_entry_low: Decimal | None = None
    planned_entry_high: Decimal | None = None
    planned_max_quantity: int | None = None
    market_stage: MarketStage = MarketStage.UNKNOWN
    chase_risk: bool | None = None
    thesis_changed_after_entry: bool = False
    new_primary_evidence_tier: EvidenceTier | None = None
    product_v1_status: str | None = None
    csv_v2_executable: bool = False
    survival_blocks: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class PreTradeResult(Contract):
    status: GateStatus
    action: ProposedAction
    rules: list[RuleEvaluation]
    score: int = Field(ge=0, le=100)
    category_scores: dict[str, int]
    reason_codes: list[str]
    executable: bool = False
    formal_authority: str = "DISCIPLINE_ONLY"
    snapshot_hash: str


class PositionStressInput(Contract):
    total_assets: Decimal = Field(gt=0)
    available_cash: Decimal = Field(ge=0)
    current_quantity: int = Field(ge=0)
    proposed_quantity: int = Field(ge=0)
    current_price: Decimal = Field(gt=0)
    hard_stop: Decimal | None = None
    planned_max_position_pct: Decimal | None = None
    single_name_limit_pct: Decimal = Decimal("20")
    sector_value: Decimal = Decimal("0")
    sector_limit_pct: Decimal = Decimal("35")
    total_exposure_value: Decimal = Decimal("0")
    total_limit_pct: Decimal = Decimal("80")
    t1_gap_risk_pct: Decimal = Decimal("5")


class PositionStressResult(Contract):
    status: StressStatus
    metrics: dict[str, Decimal]
    reason_codes: list[str]
    add_blocked: bool
    risk_reduction_allowed: bool = True
    reanalysis_required: bool


class BuyImpactObservation(Contract):
    observed_at: datetime
    side: str
    notional: Decimal
    quantity: Decimal
    start_price: Decimal
    peak_price_after: Decimal | None = None
    price_after_window: Decimal | None = None
    prior_high: Decimal | None = None
    breakout: bool | None = None
    held_breakout: bool | None = None
    response_pct: Decimal | None = None
    decay_pct: Decimal | None = None
    data_quality: str

    def efficiency(self) -> PriceImpactEfficiency:
        if self.data_quality not in {"VERIFIED_TICK", "VERIFIED_L2", "VERIFIED_MINUTE"}:
            return PriceImpactEfficiency.INSUFFICIENT_DATA
        if self.response_pct is None or self.decay_pct is None:
            return PriceImpactEfficiency.INSUFFICIENT_DATA
        if self.response_pct > 0 and self.decay_pct <= Decimal("0.25") and self.held_breakout:
            return PriceImpactEfficiency.IMPROVING
        if self.decay_pct >= Decimal("0.60"):
            return PriceImpactEfficiency.DECLINING
        return PriceImpactEfficiency.STABLE


class ThesisSnapshotInput(Contract):
    thesis_id: str
    symbol: str
    account_id: int
    playbook_code: str
    entry_reasons: list[str]
    invalidation_conditions: list[str]
    expected_behavior: list[str]
    hard_stop: Decimal
    initial_position: int
    max_position: int
    fact_evidence_ids: list[int] = Field(default_factory=list)
    analysis_hypotheses: list[str] = Field(default_factory=list)
    strategy_run_refs: list[str] = Field(default_factory=list)
    created_at: datetime

    @model_validator(mode="after")
    def validate_position(self):
        if self.initial_position > self.max_position:
            raise ValueError("initial_position cannot exceed max_position")
        return self
