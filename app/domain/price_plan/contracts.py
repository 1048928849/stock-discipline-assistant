from typing import Any, Protocol

from app.domain.price_plan.models import PricePlan


class PricePlanner(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> PricePlan: ...
