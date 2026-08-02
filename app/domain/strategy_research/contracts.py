from typing import Protocol

from app.domain.strategy_research import StrategyResearchHistory


class StrategyResearchRepository(Protocol):
    def get_history(self, strategy_version_id: int) -> StrategyResearchHistory: ...
