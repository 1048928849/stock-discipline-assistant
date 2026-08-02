from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class EventType(StrEnum):
    EXTREME_SELL_OFF = "EXTREME_SELL_OFF"
    EXTREME_RALLY = "EXTREME_RALLY"
    ABNORMAL_TURNOVER = "ABNORMAL_TURNOVER"


class AnchorRole(StrEnum):
    SUPPORT_FROM_ABOVE = "SUPPORT_FROM_ABOVE"
    RESISTANCE_FROM_BELOW = "RESISTANCE_FROM_BELOW"
    SUPPORT_AFTER_BREAKOUT = "SUPPORT_AFTER_BREAKOUT"
    RESISTANCE_AFTER_BREAKDOWN = "RESISTANCE_AFTER_BREAKDOWN"
    CONTESTED = "CONTESTED"


@dataclass(frozen=True)
class EventSession:
    event_date: date
    event_type: EventType
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class InstitutionalEvidence:
    evidence_id: str
    event_date: date
    evidence_type: str
    price: float | None
    source_id: str
    institutional: bool = False


@dataclass(frozen=True)
class EventPriceAnchor:
    anchor_id: str
    event_date: date
    anchor_type: str
    price: float
    reliability: float
    source_ids: tuple[str, ...]
    observed_at: datetime | None = None


@dataclass(frozen=True)
class EventAnchorCluster:
    center: float
    low: float
    high: float
    confidence: float
    role: AnchorRole
    anchor_ids: tuple[str, ...]


@dataclass(frozen=True)
class EventAnchorResult:
    events: tuple[EventSession, ...]
    anchors: tuple[EventPriceAnchor, ...]
    clusters: tuple[EventAnchorCluster, ...]
    missing_data: tuple[str, ...]
