from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

from app.domain.features import FeatureSnapshot
from app.domain.strategy.enums import StrategyRuleCategory, StrategyRuleStatus


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class StrategyContext:
    symbol: str
    feature_snapshot: FeatureSnapshot
    parameters: Mapping[str, Any] = field(default_factory=dict)
    position_mode: str = "空仓"
    position_context: Mapping[str, Any] = field(default_factory=dict)
    market_context: Mapping[str, Any] = field(default_factory=dict)
    sector_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _frozen_mapping(self.parameters))
        object.__setattr__(self, "position_context", _frozen_mapping(self.position_context))
        object.__setattr__(self, "market_context", _frozen_mapping(self.market_context))
        object.__setattr__(self, "sector_context", _frozen_mapping(self.sector_context))


@dataclass(frozen=True)
class StrategyRuleEvidence:
    source_id: str
    description: str
    value: Any | None = None
    data_time: datetime | date | str | None = None


@dataclass(frozen=True)
class StrategyRuleResult:
    rule_id: str
    name: str
    category: StrategyRuleCategory
    status: StrategyRuleStatus
    reason: str
    evidence: tuple[StrategyRuleEvidence, ...] = ()
    missing_data: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyEvaluationResult:
    strategy_id: str
    strategy_version: str
    overall_status: StrategyRuleStatus
    rules: tuple[StrategyRuleResult, ...]
    reasons: tuple[str, ...] = ()
    next_observations: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
