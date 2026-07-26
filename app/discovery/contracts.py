from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.hashing import canonical_hash


QualityStatus = Literal["VERIFIED", "SINGLE_SOURCE", "CONFLICTED", "STALE", "MISSING"]
MarketState = Literal["PANIC", "REPAIR", "EXPANSION", "DIVERGENCE", "CONTRACTION"]
IndustryClassification = Literal[
    "MAINLINE", "SECONDARY", "ROTATION", "DIVERGENCE", "FADING", "NONE"
]
CandidateType = Literal["WATCH_CANDIDATE", "LEADER_REFERENCE", "RESEARCH_ONLY"]
CandidateStatus = Literal["NEW", "REVIEWED", "PROMOTED", "REJECTED", "EXPIRED"]


class DiscoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiscoveryConfig(DiscoveryModel):
    algorithm_id: Literal["candidate.discovery"] = "candidate.discovery"
    algorithm_version: Literal["1.0.0"] = "1.0.0"
    max_industries: int = Field(default=3, ge=1, le=20)
    max_candidates_per_industry: int = Field(default=10, ge=1, le=100)
    max_candidates: int = Field(default=30, ge=1, le=500)
    minimum_history_rows: int = Field(default=50, ge=20, le=500)
    candidate_min_history_coverage_ratio: Decimal = Field(
        default=Decimal("0.8"), ge=0, le=1
    )
    minimum_average_amount: Decimal = Field(default=Decimal("50000000"), ge=0)
    minimum_turnover_rate: Decimal = Field(default=Decimal("0.5"), ge=0)
    maximum_turnover_rate: Decimal = Field(default=Decimal("15"), gt=0)
    max_return_5d_pct: Decimal = Field(default=Decimal("25"), gt=0)
    max_return_20d_pct: Decimal = Field(default=Decimal("15"), gt=0)
    max_distance_ma20_pct: Decimal = Field(default=Decimal("20"), gt=0)
    max_distance_ma50_pct: Decimal = Field(default=Decimal("35"), gt=0)
    weight_classification: Decimal = Field(default=Decimal("0.20"), ge=0)
    weight_relative_strength: Decimal = Field(default=Decimal("0.20"), ge=0)
    weight_amount_share: Decimal = Field(default=Decimal("0.10"), ge=0)
    weight_breadth: Decimal = Field(default=Decimal("0.10"), ge=0)
    weight_limit_up: Decimal = Field(default=Decimal("0.08"), ge=0)
    weight_leader_strength: Decimal = Field(default=Decimal("0.08"), ge=0)
    weight_new_highs: Decimal = Field(default=Decimal("0.06"), ge=0)
    weight_capital_flow: Decimal = Field(default=Decimal("0.12"), ge=0)
    weight_broken_limit: Decimal = Field(default=Decimal("0.06"), ge=0)
    candidate_weight_trend: Decimal = Field(default=Decimal("0.35"), ge=0)
    candidate_weight_relative_strength: Decimal = Field(default=Decimal("0.25"), ge=0)
    candidate_weight_liquidity: Decimal = Field(default=Decimal("0.20"), ge=0)
    candidate_weight_anti_chasing: Decimal = Field(default=Decimal("0.20"), ge=0)

    @model_validator(mode="after")
    def validate_limits_and_weights(self):
        if self.max_candidates < self.max_industries:
            raise ValueError("max_candidates must cover selected industries")
        industry_weights = (
            self.weight_classification
            + self.weight_relative_strength
            + self.weight_amount_share
            + self.weight_breadth
            + self.weight_limit_up
            + self.weight_leader_strength
            + self.weight_new_highs
            + self.weight_capital_flow
            + self.weight_broken_limit
        )
        candidate_weights = (
            self.candidate_weight_trend
            + self.candidate_weight_relative_strength
            + self.candidate_weight_liquidity
            + self.candidate_weight_anti_chasing
        )
        if industry_weights != Decimal("1") or candidate_weights != Decimal("1"):
            raise ValueError("discovery weights must sum to one")
        return self

    def config_hash(self) -> str:
        return canonical_hash(self)


class PriceHistoryPoint(DiscoveryModel):
    trade_date: date
    close: Decimal = Field(gt=0)
    amount: Decimal = Field(ge=0)
    turnover_rate: Decimal = Field(ge=0)


class CandidateStockInput(DiscoveryModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1, max_length=100)
    industry_key: str = Field(min_length=1, max_length=40)
    industry_name: str = Field(min_length=1, max_length=200)
    prices: tuple[PriceHistoryPoint, ...]
    is_st: bool
    suspended: bool
    limit_up: bool
    delisting: bool = False
    quality_status: QualityStatus
    evidence_references: tuple[str, ...]

    @field_validator("evidence_references", mode="before")
    @classmethod
    def stable_evidence(cls, value):
        return tuple(sorted(set(value)))


class IndustryDiscoveryInput(DiscoveryModel):
    industry_key: str = Field(min_length=1, max_length=40)
    industry_name: str = Field(min_length=1, max_length=200)
    classification: IndustryClassification
    relative_strength_5d: Decimal | None
    relative_strength_10d: Decimal | None
    relative_strength_20d: Decimal | None
    amount_share: Decimal | None
    advance_ratio: Decimal | None
    limit_up_count: int | None = Field(default=None, ge=0)
    leader_strength: Decimal | None
    new_high_ratio: Decimal | None
    net_inflow_1d: Decimal | None
    net_inflow_5d: Decimal | None
    net_inflow_10d: Decimal | None
    broken_limit_rate: Decimal | None
    quality_status: QualityStatus
    evidence_references: tuple[str, ...]
    total_constituents: int = Field(ge=0)
    historical_data_ready: int = Field(ge=0)
    historical_data_missing: int = Field(ge=0)
    coverage_ratio: Decimal = Field(ge=0, le=1)
    constituents: tuple[CandidateStockInput, ...]

    @field_validator("evidence_references", mode="before")
    @classmethod
    def stable_evidence(cls, value):
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_coverage(self):
        if self.historical_data_ready + self.historical_data_missing != self.total_constituents:
            raise ValueError("industry history coverage counts must match total constituents")
        expected = (
            (
                Decimal(self.historical_data_ready)
                / Decimal(self.total_constituents)
            ).quantize(Decimal("0.000001"))
            if self.total_constituents
            else Decimal("0")
        )
        if self.coverage_ratio != expected:
            raise ValueError("industry history coverage ratio must match coverage counts")
        return self


class MarketDiscoveryInput(DiscoveryModel):
    market: Literal["CN-A"] = "CN-A"
    trade_date: date
    state: MarketState
    quality_status: QualityStatus
    evidence_references: tuple[str, ...]

    @field_validator("evidence_references", mode="before")
    @classmethod
    def stable_evidence(cls, value):
        return tuple(sorted(set(value)))


class DiscoverySnapshot(DiscoveryModel):
    market: MarketDiscoveryInput
    industries: tuple[IndustryDiscoveryInput, ...]
    observed_at: datetime
    blocked_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_observed_at(self):
        if self.observed_at.tzinfo is None:
            raise ValueError("discovery observed_at must be timezone-aware")
        keys = [item.industry_key for item in self.industries]
        if len(keys) != len(set(keys)):
            raise ValueError("discovery industries must be unique")
        return self

    def snapshot_hash(self) -> str:
        return canonical_hash(self)


class CandidateIndustryResult(DiscoveryModel):
    industry_key: str
    industry_name: str
    classification: IndustryClassification
    score: Decimal | None
    rank: int | None
    metrics: dict
    reason_codes: tuple[str, ...]
    evidence_references: tuple[str, ...]
    quality_status: QualityStatus


class CandidateResult(DiscoveryModel):
    symbol: str
    name: str
    industry_key: str
    industry_name: str
    candidate_type: CandidateType
    score: Decimal
    rank: int
    status: CandidateStatus = "NEW"
    current_price: Decimal
    technical_metrics: dict
    reason_codes: tuple[str, ...]
    risk_flags: tuple[str, ...]
    evidence_references: tuple[str, ...]
    quality_status: QualityStatus
    snapshot_hash: str


class DiscoveryResult(DiscoveryModel):
    status: Literal["COMPLETED", "BLOCKED"]
    algorithm_id: str
    algorithm_version: str
    config_hash: str
    input_snapshot_hash: str
    market_state: MarketState
    quality_status: QualityStatus
    blocked_reasons: tuple[str, ...]
    total_constituents: int
    historical_data_ready: int
    historical_data_missing: int
    coverage_ratio: Decimal
    industries: tuple[CandidateIndustryResult, ...]
    candidates: tuple[CandidateResult, ...]


__all__ = [
    "CandidateIndustryResult",
    "CandidateResult",
    "CandidateStockInput",
    "CandidateStatus",
    "CandidateType",
    "DiscoveryConfig",
    "DiscoveryResult",
    "DiscoverySnapshot",
    "IndustryDiscoveryInput",
    "MarketDiscoveryInput",
    "PriceHistoryPoint",
]
