from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WatchlistStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    RESEARCHING = "RESEARCHING"
    WATCHING = "WATCHING"
    NEAR_ENTRY = "NEAR_ENTRY"
    ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
    INVALIDATED = "INVALIDATED"
    ARCHIVED = "ARCHIVED"


class MonitoringHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DATA_BLOCKED = "DATA_BLOCKED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    STALE = "STALE"
    CONFLICTED = "CONFLICTED"
    DISABLED = "DISABLED"


class WatchlistSourceType(str, Enum):
    MANUAL = "MANUAL"
    PRODUCT_ANALYSIS = "PRODUCT_ANALYSIS"
    STRATEGY_SIGNAL = "STRATEGY_SIGNAL"
    CANDIDATE_DISCOVERY = "CANDIDATE_DISCOVERY"


class MonitoringRuleType(str, Enum):
    PRICE_NEAR_ENTRY_ZONE = "PRICE_NEAR_ENTRY_ZONE"
    PRICE_ENTER_ENTRY_ZONE = "PRICE_ENTER_ENTRY_ZONE"
    PRICE_BREAK_HARD_STOP = "PRICE_BREAK_HARD_STOP"
    MARKET_REGIME_DOWNGRADE = "MARKET_REGIME_DOWNGRADE"
    INDUSTRY_STATUS_DOWNGRADE = "INDUSTRY_STATUS_DOWNGRADE"
    DATA_QUALITY_DEGRADED = "DATA_QUALITY_DEGRADED"
    PLAN_BECAME_STALE = "PLAN_BECAME_STALE"
    PLAN_INVALIDATION_TRIGGERED = "PLAN_INVALIDATION_TRIGGERED"


class EventSeverity(str, Enum):
    INFO = "INFO"
    ATTENTION = "ATTENTION"
    CRITICAL = "CRITICAL"


class ReanalysisStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class InvalidationRuleType(str, Enum):
    PRICE_AT_OR_BELOW_HARD_STOP = "PRICE_AT_OR_BELOW_HARD_STOP"
    MARKET_REGIME_AT_OR_WORSE_THAN = "MARKET_REGIME_AT_OR_WORSE_THAN"
    INDUSTRY_STATUS_AT_OR_WORSE_THAN = "INDUSTRY_STATUS_AT_OR_WORSE_THAN"


class InvalidationRuleBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1, max_length=100)
    evidence_reference: str = Field(min_length=1, max_length=200)
    created_at: datetime

    @model_validator(mode="after")
    def validate_created_at(self):
        if self.created_at.tzinfo is None:
            raise ValueError("invalidation rule created_at must be timezone-aware")
        return self


class PriceInvalidationRule(InvalidationRuleBase):
    rule_type: Literal[
        InvalidationRuleType.PRICE_AT_OR_BELOW_HARD_STOP
    ] = InvalidationRuleType.PRICE_AT_OR_BELOW_HARD_STOP
    threshold: Decimal = Field(gt=0)


class MarketRegimeInvalidationRule(InvalidationRuleBase):
    rule_type: Literal[
        InvalidationRuleType.MARKET_REGIME_AT_OR_WORSE_THAN
    ] = InvalidationRuleType.MARKET_REGIME_AT_OR_WORSE_THAN
    threshold: Literal["EXPANSION", "REPAIR", "DIVERGENCE", "CONTRACTION", "PANIC"]


class IndustryStatusInvalidationRule(InvalidationRuleBase):
    rule_type: Literal[
        InvalidationRuleType.INDUSTRY_STATUS_AT_OR_WORSE_THAN
    ] = InvalidationRuleType.INDUSTRY_STATUS_AT_OR_WORSE_THAN
    threshold: Literal["MAINLINE", "SECONDARY", "ROTATION", "FADING", "NONE"]


InvalidationRuleSpec = Annotated[
    PriceInvalidationRule
    | MarketRegimeInvalidationRule
    | IndustryStatusInvalidationRule,
    Field(discriminator="rule_type"),
]


class WatchlistCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: WatchlistSourceType
    thesis: str = Field(min_length=1, max_length=4000)
    analysis_run_id: int | None = Field(default=None, gt=0)
    symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    analysis_capital: Decimal | None = Field(default=None, gt=0)
    waiting_conditions: list[str] = Field(default_factory=list, max_length=50)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=50)
    invalidation_rule_specs: list[InvalidationRuleSpec] = Field(
        default_factory=list,
        max_length=50,
    )

    @model_validator(mode="after")
    def validate_source_reference(self):
        if self.source_type == WatchlistSourceType.PRODUCT_ANALYSIS:
            if self.analysis_run_id is None or self.symbol is not None:
                raise ValueError("product analysis source requires only analysis_run_id")
            if self.invalidation_rule_specs:
                raise ValueError(
                    "product analysis invalidation rules must be loaded by the server"
                )
        elif self.symbol is None:
            raise ValueError("manual and strategy signal sources require symbol")
        return self


class WatchlistPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thesis: str | None = Field(default=None, min_length=1, max_length=4000)
    analysis_capital: Decimal | None = Field(default=None, gt=0)
    waiting_conditions: list[str] | None = Field(default=None, max_length=50)
    invalidation_conditions: list[str] | None = Field(default=None, max_length=50)
    invalidation_rule_specs: list[InvalidationRuleSpec] | None = Field(
        default=None,
        max_length=50,
    )
    monitoring_enabled: bool | None = None


class WatchlistScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    now: datetime | None = None
    item_ids: list[int] | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_now(self):
        if self.now is not None and self.now.tzinfo is None:
            raise ValueError("scan now must be timezone-aware")
        return self


class WatchlistScanSummary(BaseModel):
    scanned: int = 0
    unchanged: int = 0
    transitioned: int = 0
    blocked: int = 0
    events_created: int = 0
    reanalysis_requested: int = 0
    failures: list[dict] = Field(default_factory=list, max_length=500)
    lease_acquired: bool = True


class PriceStateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    current_status: WatchlistStatus
    current_price: Decimal | None = Field(default=None, gt=0)
    entry_low: Decimal = Field(gt=0)
    entry_high: Decimal = Field(gt=0)
    hard_stop: Decimal = Field(gt=0)
    near_entry_distance_pct: Decimal = Field(ge=0, le=100)
    observed_at: datetime
    monitoring_health: MonitoringHealth = MonitoringHealth.HEALTHY
    invalidation_triggered: bool = False
    reassessment: bool = False

    @model_validator(mode="after")
    def validate_price_plan(self):
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low must not exceed entry_high")
        if self.hard_stop >= self.entry_low:
            raise ValueError("hard_stop must be below entry_low")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return self


class PriceStateOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_status: WatchlistStatus
    to_status: WatchlistStatus
    rule_type: MonitoringRuleType | None = None
    reason_codes: tuple[str, ...] = ()
    severity: Literal["INFO", "ATTENTION", "CRITICAL"] = "INFO"
    trigger_reanalysis: bool = False


__all__ = [
    "EventSeverity",
    "IndustryStatusInvalidationRule",
    "InvalidationRuleSpec",
    "InvalidationRuleType",
    "MarketRegimeInvalidationRule",
    "MonitoringHealth",
    "MonitoringRuleType",
    "PriceInvalidationRule",
    "PriceStateInput",
    "PriceStateOutcome",
    "ReanalysisStatus",
    "WatchlistCreateRequest",
    "WatchlistPatchRequest",
    "WatchlistScanRequest",
    "WatchlistScanSummary",
    "WatchlistSourceType",
    "WatchlistStatus",
]
