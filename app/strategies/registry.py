from app.domain.strategy import Strategy


class StrategyNotFoundError(LookupError):
    pass


class StrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        if strategy.strategy_id in self._strategies:
            raise ValueError(f"策略已注册：{strategy.strategy_id}")
        self._strategies[strategy.strategy_id] = strategy

    def get(self, strategy_id: str) -> Strategy:
        try:
            return self._strategies[strategy_id]
        except KeyError as exc:
            raise StrategyNotFoundError(f"未知策略：{strategy_id}") from exc

    def list(self) -> tuple[Strategy, ...]:
        return tuple(self._strategies.values())
