from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from app.domain.risk.enums import RiskStatus


def _readonly(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class RiskContext:
    account_context: Mapping[str, Any] = field(default_factory=dict)
    position_context: Mapping[str, Any] = field(default_factory=dict)
    entry_context: Mapping[str, Any] = field(default_factory=dict)
    stop_context: Mapping[str, Any] = field(default_factory=dict)
    market_context: Mapping[str, Any] = field(default_factory=dict)
    sector_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "account_context",
            "position_context",
            "entry_context",
            "stop_context",
            "market_context",
            "sector_context",
        ):
            object.__setattr__(self, name, _readonly(getattr(self, name)))


@dataclass(frozen=True)
class RiskConstraint:
    name: str
    limit: int


@dataclass(frozen=True)
class RiskEvaluationResult:
    status: RiskStatus
    allowed_quantity: int
    risk_amount: float | None
    risk_ratio: float | None
    constraints: tuple[RiskConstraint, ...]
    blocking_reasons: tuple[str, ...]
    calculation_details: Mapping[str, Any]
    binding_constraint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "calculation_details", _readonly(self.calculation_details))
