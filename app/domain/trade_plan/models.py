from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from app.domain.trade_plan.enums import TradePlanLifecycle


def _readonly(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class TradePlanDraft:
    symbol: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    lifecycle: TradePlanLifecycle = TradePlanLifecycle.DRAFT

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", _readonly(self.inputs))


@dataclass(frozen=True)
class TradePlanPreview:
    symbol: str
    payload: Mapping[str, Any]
    lifecycle: TradePlanLifecycle = TradePlanLifecycle.PREVIEW

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _readonly(self.payload))


@dataclass(frozen=True)
class TradePlanSnapshot:
    symbol: str
    preview_hash: str
    payload: Mapping[str, Any]
    lifecycle: TradePlanLifecycle = TradePlanLifecycle.CONFIRMED

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _readonly(self.payload))


@dataclass(frozen=True)
class TradePlanVersion:
    version: int
    parent_plan_id: int | None = None
