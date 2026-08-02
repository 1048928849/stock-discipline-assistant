from __future__ import annotations

from typing import Protocol

from app.domain.strategy_management import StrategyDefinition, StrategyVersion


class StrategyRepository(Protocol):
    def get_strategy(self, strategy_id: str) -> StrategyDefinition | None: ...

    def get_version(self, strategy_id: str, version: str) -> StrategyVersion | None: ...

    def list_versions(self, strategy_id: str) -> list[StrategyVersion]: ...
