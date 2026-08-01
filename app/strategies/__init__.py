from app.strategies.platform_breakout import PlatformBreakoutPullbackStrategy
from app.strategies.registry import StrategyNotFoundError, StrategyRegistry


def default_strategy_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register(PlatformBreakoutPullbackStrategy())
    return registry


__all__ = [
    "PlatformBreakoutPullbackStrategy",
    "StrategyNotFoundError",
    "StrategyRegistry",
    "default_strategy_registry",
]
