from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PriceBar(AnalysisModel):
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Field(ge=0)


class TurnoverPoint(AnalysisModel):
    trade_date: date
    turnover_rate: Decimal = Field(ge=0)


class TechnicalAnalysisInput(AnalysisModel):
    daily_bars: tuple[PriceBar, ...]
    intraday_bars: tuple[PriceBar, ...]
    turnover: tuple[TurnoverPoint, ...]


class TechnicalContext(AnalysisModel):
    intraday_trend: Literal["UP", "DOWN", "RANGE", "INSUFFICIENT"]
    daily_trend: Literal["UP", "DOWN", "RANGE", "INSUFFICIENT"]
    ma5: Decimal | None
    ma10: Decimal | None
    ma20: Decimal | None
    effective_high: Decimal | None
    effective_low: Decimal | None
    breakout: bool
    pullback: bool
    false_breakout: bool
    volume_breakout: bool
    low_volume_pullback: bool
    spike_fade: bool
    daily_intraday_aligned: bool
    current_turnover: Decimal | None
    average_turnover_5d: Decimal | None
    average_turnover_20d: Decimal | None
    relative_turnover_20d: Decimal | None
    high_position_abnormal_turnover: bool
    low_position_moderate_volume: bool
    volume_without_price_gain: bool
    low_volume_rise: bool
    data_completeness: Decimal = Field(ge=0, le=1)


class BreadthMetrics(AnalysisModel):
    trade_date: date
    advancing: int = Field(ge=0)
    declining: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    limit_up: int = Field(ge=0)
    limit_down: int = Field(ge=0)
    new_highs: int | None = Field(default=None, ge=0)
    new_lows: int | None = Field(default=None, ge=0)
    median_change_pct: Decimal | None = None
    above_ma20_ratio: Decimal | None = Field(default=None, ge=0, le=1)
    above_ma50_ratio: Decimal | None = Field(default=None, ge=0, le=1)


class MarketRegimeInput(AnalysisModel):
    current: BreadthMetrics
    previous_state: Literal[
        "PANIC", "REPAIR", "EXPANSION", "DIVERGENCE", "CONTRACTION"
    ]
    amount_history: tuple[Decimal, ...]
    index_changes: dict[str, Decimal]


class MarketRegime(AnalysisModel):
    state: Literal["PANIC", "REPAIR", "EXPANSION", "DIVERGENCE", "CONTRACTION"]
    previous_state: str
    transition: str
    data_completeness: Decimal = Field(ge=0, le=1)
    supporting_indicators: tuple[str, ...]
    conflicting_indicators: tuple[str, ...]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    offensive_allowed: bool
    market_position_cap: Decimal = Field(ge=0, le=1)
    advance_ratio: Decimal | None
    amount_ratio_5d: Decimal | None
    amount_ratio_20d: Decimal | None


class IndustryObservation(AnalysisModel):
    trade_date: date
    change_pct: Decimal | None
    amount: Decimal | None
    amount_share: Decimal | None
    advance_ratio: Decimal | None
    limit_up_count: int | None = Field(default=None, ge=0)
    leader_strength: Decimal | None
    new_high_ratio: Decimal | None


class IndustrySeries(AnalysisModel):
    name: str = Field(min_length=1, max_length=200)
    observations: tuple[IndustryObservation, ...]


class IndustryAnalysisInput(AnalysisModel):
    industries: tuple[IndustrySeries, ...]
    benchmark_changes: tuple[Decimal, ...]


class IndustryAssessment(AnalysisModel):
    name: str
    classification: Literal[
        "MAINLINE", "SECONDARY", "ROTATION", "DIVERGENCE", "FADING", "NONE"
    ]
    phase: Literal[
        "PULSE", "ROTATION", "PERSISTENT", "ACCELERATION", "HIGH_DIVERGENCE", "FADING", "NONE"
    ]
    daily_change: Decimal | None
    relative_strength_5d: Decimal | None
    relative_strength_10d: Decimal | None
    relative_strength_20d: Decimal | None
    amount: Decimal | None
    amount_share: Decimal | None
    advance_ratio: Decimal | None
    limit_up_count: int | None
    leader_strength: Decimal | None
    persistence_days: int
    max_drawdown: Decimal | None
    new_high_ratio: Decimal | None
    crowding_risk: bool
    supporting_indicators: tuple[str, ...]
    conflicting_indicators: tuple[str, ...]


class IndustryContext(AnalysisModel):
    industries: tuple[IndustryAssessment, ...]
    mainlines: tuple[str, ...]
    secondary: tuple[str, ...]
    fading_industries: tuple[str, ...]


Relevance = Literal[
    "CORE_BUSINESS",
    "IMPORTANT_BUSINESS",
    "MARGINAL_BENEFIT",
    "CONCEPT_ASSOCIATION",
    "INSUFFICIENT_EVIDENCE",
    "REJECTED",
]


class ConceptMapping(AnalysisModel):
    concept: str = Field(min_length=1, max_length=200)
    relevance: Relevance
    evidence_refs: tuple[str, ...]

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def stable_refs(cls, value):
        return tuple(sorted(set(value)))


class ChainPosition(AnalysisModel):
    chain: str
    node: str
    stage: str
    relevance: Relevance
    primary_products: tuple[str, ...] = ()
    revenue_relevance: str = "unknown"
    core_level: str | None = None
    substitutability: str | None = None
    competitive_position: str | None = None
    evidence_refs: tuple[str, ...]


class ConceptChainInput(AnalysisModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    concepts: tuple[ConceptMapping, ...]
    chain_positions: tuple[ChainPosition, ...]
    current_mainlines: tuple[str, ...]


class ConceptChainContext(AnalysisModel):
    core_concept: str | None
    relevance: Relevance
    chain: str | None
    chain_node: str | None
    primary_products: tuple[str, ...]
    revenue_relevance: str
    core_level: str | None
    substitutability: str | None
    competitive_position: str | None
    evidence_refs: tuple[str, ...]
    conflicts: tuple[str, ...]
    is_current_mainline: bool
    is_mainline_core_company: bool


__all__ = [
    "AnalysisModel",
    "BreadthMetrics",
    "ChainPosition",
    "ConceptChainContext",
    "ConceptChainInput",
    "ConceptMapping",
    "IndustryAnalysisInput",
    "IndustryAssessment",
    "IndustryContext",
    "IndustryObservation",
    "IndustrySeries",
    "MarketRegime",
    "MarketRegimeInput",
    "PriceBar",
    "TechnicalAnalysisInput",
    "TechnicalContext",
    "TurnoverPoint",
]
