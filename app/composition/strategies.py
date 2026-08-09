from __future__ import annotations

from app.selected_stock.strategy import CycleStructureValidationStrategyV2
from app.strategies import CoreDisciplineStrategy, StrategyRegistry


_APPLICATION_REGISTRY: StrategyRegistry | None = None


def build_strategy_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register(CoreDisciplineStrategy())
    # CSV_V2 is resolved by the selected-stock advisory service only. Keeping it
    # disabled prevents Product V1 package and freeze bindings from changing.
    registry.register(CycleStructureValidationStrategyV2(), enabled=False)
    return registry


def get_strategy_registry() -> StrategyRegistry:
    global _APPLICATION_REGISTRY
    if _APPLICATION_REGISTRY is None:
        _APPLICATION_REGISTRY = build_strategy_registry()
    return _APPLICATION_REGISTRY


__all__ = ["build_strategy_registry", "get_strategy_registry"]
