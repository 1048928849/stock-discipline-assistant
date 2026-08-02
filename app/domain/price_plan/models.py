from dataclasses import dataclass

from app.domain.event_anchor import EventAnchorResult
from app.domain.price_plan.enums import PricePlanStatus


@dataclass(frozen=True)
class PriceZone:
    low: float | None
    high: float | None


@dataclass(frozen=True)
class CapitalAnchor:
    anchor_id: str
    label: str
    price: float
    weight: float
    source: str
    window: int | None = None


@dataclass(frozen=True)
class StructuralZone:
    role: str
    low: float
    high: float
    center: float
    confidence: float
    anchor_ids: tuple[str, ...]


@dataclass(frozen=True)
class MarketStructurePlan:
    as_of_price: float
    anchors: tuple[CapitalAnchor, ...]
    support_zones: tuple[StructuralZone, ...]
    resistance_zones: tuple[StructuralZone, ...]
    extreme_stop: float | None
    extreme_stop_basis: str | None
    warnings: tuple[str, ...] = ()
    event_anchors: EventAnchorResult | None = None


@dataclass(frozen=True)
class PricePlan:
    status: PricePlanStatus
    entry_zone: PriceZone
    entry_reference: float | None
    stop_price: float | None
    target_price: float | None
    first_target: float | None
    second_target: float | None
    reward_risk: float | None
    invalidation_price: float | None
    structure_invalidation: str
    market_structure: MarketStructurePlan | None = None
