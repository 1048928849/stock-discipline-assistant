from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.strategy_research.enums import EvidenceType, ValidationStatus


@dataclass(frozen=True)
class StrategyResearchRecord:
    id: int | None
    strategy_version_id: int
    hypothesis: str
    thesis: str
    causal_chain: tuple[str, ...]
    market_conditions: tuple[str, ...]
    applicable_scenarios: tuple[str, ...]
    failure_conditions: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True)
class EvidenceRecord:
    id: int | None
    strategy_version_id: int
    type: EvidenceType
    source: str
    content: str
    reference: str | None
    created_at: datetime


@dataclass(frozen=True)
class ValidationRecord:
    id: int | None
    strategy_version_id: int
    validation_type: str
    status: ValidationStatus
    sample_size: int
    result_summary: str
    created_at: datetime


@dataclass(frozen=True)
class StrategyResearchHistory:
    strategy_version_id: int
    research_record: StrategyResearchRecord | None
    evidence_records: tuple[EvidenceRecord, ...] = ()
    validation_records: tuple[ValidationRecord, ...] = ()
