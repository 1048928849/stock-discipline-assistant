from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Any, Protocol, runtime_checkable

from app.strategies.contracts import StrategyManifest, StrategySignal
from app.strategies.hashing import validate_parameter_schema


@runtime_checkable
class StrategySnapshot(Protocol):
    @property
    def snapshot_hash(self) -> str: ...

    def has_capability(self, capability: str) -> bool: ...

    def evidence_refs_for(self, capability: str) -> tuple[str, ...]: ...


class TradingStrategy(ABC):
    strategy_id: str
    strategy_version: str

    @abstractmethod
    def manifest(self) -> StrategyManifest:
        raise NotImplementedError

    def required_capabilities(self) -> tuple[str, ...]:
        return self.manifest().required_capabilities

    def validate_parameters(self, parameters: dict[str, Any]) -> dict[str, Any]:
        manifest = self.manifest()
        merged = deepcopy(manifest.default_parameters)
        merged.update(deepcopy(parameters))
        return validate_parameter_schema(merged, manifest.parameter_schema)

    @abstractmethod
    def is_applicable(
        self, snapshot: StrategySnapshot, parameters: dict[str, Any]
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def evaluate(
        self, snapshot: StrategySnapshot, parameters: dict[str, Any]
    ) -> StrategySignal:
        raise NotImplementedError


__all__ = ["StrategySnapshot", "TradingStrategy"]
