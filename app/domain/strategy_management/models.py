from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from app.domain.strategy_management.enums import StrategyLifecycle
from app.domain.strategy_research import EvidenceRecord, StrategyResearchRecord, ValidationRecord


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class StrategyDefinition:
    id: str
    name: str
    description: str
    category: str
    created_at: datetime
    owner: str
    status: StrategyLifecycle


@dataclass(frozen=True)
class StrategyVersion:
    id: int | None
    strategy_id: str
    version: str
    status: StrategyLifecycle
    rule_snapshot: Mapping[str, Any]
    parameter_snapshot: Mapping[str, Any]
    created_at: datetime
    activated_at: datetime | None = None
    retired_at: datetime | None = None
    research_record: StrategyResearchRecord | None = None
    evidence_records: tuple[EvidenceRecord, ...] = ()
    validation_records: tuple[ValidationRecord, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_snapshot", freeze(self.rule_snapshot))
        object.__setattr__(self, "parameter_snapshot", freeze(self.parameter_snapshot))


@dataclass(frozen=True)
class StrategyLifecycleEvent:
    id: int | None
    strategy_id: str
    strategy_version_id: int | None
    from_status: StrategyLifecycle | None
    to_status: StrategyLifecycle
    event_type: str
    reason: str | None
    occurred_at: datetime
