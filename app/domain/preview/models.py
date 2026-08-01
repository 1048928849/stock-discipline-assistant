from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

from app.domain.preview.enums import PreviewSnapshotStatus


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class RuleVersionSnapshot:
    id: int
    version: str
    effective_from: date
    active: bool
    parameters: Mapping[str, Any]
    rules: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", freeze(self.parameters))
        object.__setattr__(self, "rules", freeze(self.rules))


@dataclass(frozen=True)
class PreviewSnapshot:
    snapshot_id: int | None
    symbol: str
    preview_hash: str
    strategy_snapshot: Mapping[str, Any]
    feature_snapshot: Mapping[str, Any]
    risk_snapshot: Mapping[str, Any]
    decision_snapshot: Mapping[str, Any]
    price_snapshot: Mapping[str, Any]
    rule_version_snapshot: Mapping[str, Any]
    account_snapshot: Mapping[str, Any]
    market_snapshot: Mapping[str, Any]
    preview_payload: Mapping[str, Any]
    created_at: datetime
    hash: str
    status: PreviewSnapshotStatus = field(default=PreviewSnapshotStatus.FROZEN)

    def __post_init__(self) -> None:
        for name in (
            "strategy_snapshot",
            "feature_snapshot",
            "risk_snapshot",
            "decision_snapshot",
            "price_snapshot",
            "rule_version_snapshot",
            "account_snapshot",
            "market_snapshot",
            "preview_payload",
        ):
            object.__setattr__(self, name, freeze(getattr(self, name)))
