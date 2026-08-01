import pytest

from app.strategies import PlatformBreakoutPullbackStrategy
from app.strategies.registry import StrategyNotFoundError, StrategyRegistry


def test_registry_register_get_and_list():
    registry = StrategyRegistry()
    strategy = PlatformBreakoutPullbackStrategy()

    registry.register(strategy)

    assert registry.get(strategy.strategy_id) is strategy
    assert registry.list() == (strategy,)


def test_registry_rejects_duplicate_strategy_id():
    registry = StrategyRegistry()
    registry.register(PlatformBreakoutPullbackStrategy())

    with pytest.raises(ValueError, match="策略已注册"):
        registry.register(PlatformBreakoutPullbackStrategy())


def test_registry_rejects_unknown_strategy():
    registry = StrategyRegistry()

    with pytest.raises(StrategyNotFoundError, match="未知策略"):
        registry.get("unknown")
