from typing import Protocol

from app.domain.strategy.models import StrategyContext, StrategyEvaluationResult


class Strategy(Protocol):
    strategy_id: str
    strategy_version: str

    def evaluate(self, context: StrategyContext) -> StrategyEvaluationResult: ...
