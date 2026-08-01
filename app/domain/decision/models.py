from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from app.domain.decision.enums import TradeDecision
from app.domain.strategy import StrategyEvaluationResult


def _readonly(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class RiskEvaluationResult:
    gate_statuses: Mapping[str, str] = field(default_factory=dict)
    entry_capacity_allowed: bool = False
    blocking_factors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "gate_statuses", _readonly(self.gate_statuses))


@dataclass(frozen=True)
class DecisionContext:
    strategy_result: StrategyEvaluationResult
    risk_result: RiskEvaluationResult
    market_context: Mapping[str, Any] = field(default_factory=dict)
    position_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_context", _readonly(self.market_context))
        object.__setattr__(self, "position_context", _readonly(self.position_context))


@dataclass(frozen=True)
class DecisionResult:
    decision: TradeDecision
    reason: str
    evidence: tuple[str, ...]
    blocking_factors: tuple[str, ...]
    next_actions: tuple[str, ...]
    legacy_plan_status: str
    label: str
    position_evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "position_evidence", _readonly(self.position_evidence))
