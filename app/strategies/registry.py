from __future__ import annotations

from app.strategies.base import TradingStrategy
from app.strategies.hashing import implementation_hash


StrategyKey = tuple[str, str]


class StrategyRegistry:
    def __init__(self):
        self._strategies: dict[StrategyKey, TradingStrategy] = {}
        self._enabled: set[StrategyKey] = set()

    def register(self, strategy: TradingStrategy, *, enabled: bool = True) -> None:
        if not isinstance(strategy, TradingStrategy):
            raise TypeError("strategy must implement TradingStrategy")
        manifest = strategy.manifest()
        if manifest.strategy_id != strategy.strategy_id:
            raise ValueError("manifest strategy_id does not match implementation")
        if manifest.version != strategy.strategy_version:
            raise ValueError("manifest version does not match implementation")
        capabilities = tuple(sorted(set(strategy.required_capabilities())))
        if capabilities != manifest.required_capabilities:
            raise ValueError("manifest capabilities do not match implementation")
        computed_hash = implementation_hash(type(strategy))
        if manifest.implementation_hash != computed_hash:
            raise ValueError("manifest implementation_hash does not match implementation")
        key = (manifest.strategy_id, manifest.version)
        if key in self._strategies:
            raise ValueError("duplicate strategy registration")
        self._strategies[key] = strategy
        if enabled:
            self._enabled.add(key)

    def get(self, strategy_id: str, version: str) -> TradingStrategy:
        try:
            return self._strategies[(strategy_id, version)]
        except KeyError as exc:
            raise KeyError(f"strategy is not registered: {strategy_id}@{version}") from exc

    def enable(self, strategy_id: str, version: str) -> None:
        key = (strategy_id, version)
        if key not in self._strategies:
            raise KeyError(f"strategy is not registered: {strategy_id}@{version}")
        self._enabled.add(key)

    def disable(self, strategy_id: str, version: str) -> None:
        key = (strategy_id, version)
        if key not in self._strategies:
            raise KeyError(f"strategy is not registered: {strategy_id}@{version}")
        self._enabled.discard(key)

    def enabled_strategies(self) -> tuple[TradingStrategy, ...]:
        return tuple(self._strategies[key] for key in sorted(self._enabled))

    def required_capabilities(self, *, enabled_only: bool = True) -> tuple[str, ...]:
        strategies = (
            self.enabled_strategies()
            if enabled_only
            else tuple(self._strategies[key] for key in sorted(self._strategies))
        )
        return tuple(
            sorted(
                {
                    capability
                    for strategy in strategies
                    for capability in strategy.required_capabilities()
                }
            )
        )


__all__ = ["StrategyRegistry"]
