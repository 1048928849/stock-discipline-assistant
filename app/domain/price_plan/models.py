from dataclasses import dataclass

from app.domain.price_plan.enums import PricePlanStatus


@dataclass(frozen=True)
class PriceZone:
    low: float | None
    high: float | None


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
