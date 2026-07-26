from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.strategies.base import StrategySnapshot, TradingStrategy
from app.strategies.contracts import StrategyManifest, StrategySignal
from app.strategies.hashing import implementation_hash, parameter_hash
from app.strategies.registry import StrategyRegistry


class Snapshot:
    snapshot_hash = "a" * 64

    def has_capability(self, capability: str) -> bool:
        return capability == "market.daily.qfq"

    def evidence_refs_for(self, capability: str) -> tuple[str, ...]:
        return ("evidence:daily",) if self.has_capability(capability) else ()


class FixtureStrategy(TradingStrategy):
    strategy_id = "fixture.core"
    strategy_version = "1.0.0"

    def manifest(self) -> StrategyManifest:
        return StrategyManifest(
            strategy_id=self.strategy_id,
            version=self.strategy_version,
            name="Fixture",
            description="Contract fixture",
            time_horizon="swing",
            required_capabilities=("market.daily.qfq",),
            parameter_schema={
                "type": "object",
                "properties": {
                    "threshold": {"type": "number", "minimum": 0, "maximum": 1}
                },
                "required": ["threshold"],
                "additionalProperties": False,
            },
            default_parameters={"threshold": 0.5},
            applicable_market_regimes=("EXPANSION", "REPAIR"),
            source_evidence=("deterministic fixture",),
            implementation_hash=implementation_hash(type(self)),
        )

    def is_applicable(self, snapshot: StrategySnapshot, parameters: dict) -> bool:
        return snapshot.has_capability("market.daily.qfq") and parameters["threshold"] > 0

    def evaluate(self, snapshot: StrategySnapshot, parameters: dict) -> StrategySignal:
        validated = self.validate_parameters(parameters)
        return StrategySignal(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            applicable=self.is_applicable(snapshot, validated),
            signal_type="WATCH",
            signal_strength="0.5",
            entry_assessment={"threshold": validated["threshold"]},
            invalidation="fixture invalidation",
            suggested_risk_level="LOW",
            holding_horizon="swing",
            evidence_refs=snapshot.evidence_refs_for("market.daily.qfq"),
            blocked_reasons=(),
            parameter_hash=parameter_hash(validated),
        )


def test_strategy_registry_registers_and_queries_enabled_strategy():
    registry = StrategyRegistry()
    strategy = FixtureStrategy()
    registry.register(strategy)
    assert registry.get("fixture.core", "1.0.0") is strategy
    assert registry.enabled_strategies() == (strategy,)


def test_duplicate_strategy_id_and_version_is_rejected():
    registry = StrategyRegistry()
    registry.register(FixtureStrategy())
    with pytest.raises(ValueError, match="duplicate strategy registration"):
        registry.register(FixtureStrategy())


def test_manifest_and_implementation_version_mismatch_is_rejected():
    class WrongVersion(FixtureStrategy):
        strategy_version = "2.0.0"

        def manifest(self):
            value = super().manifest().model_dump()
            value["version"] = "1.0.0"
            value["implementation_hash"] = implementation_hash(type(self))
            return StrategyManifest(**value)

    with pytest.raises(ValueError, match="manifest version"):
        StrategyRegistry().register(WrongVersion())


def test_default_parameters_must_match_schema():
    data = FixtureStrategy().manifest().model_dump()
    data["default_parameters"] = {"threshold": 2}
    with pytest.raises(ValueError, match="maximum"):
        StrategyManifest(**data)


def test_same_manifest_has_same_deterministic_hash():
    first = FixtureStrategy().manifest()
    second = FixtureStrategy().manifest()
    assert first.manifest_hash() == second.manifest_hash()


def test_parameter_changes_change_parameter_hash():
    assert parameter_hash({"threshold": 0.5}) != parameter_hash({"threshold": 0.6})


def test_signal_input_changes_change_signal_hash():
    strategy = FixtureStrategy()
    first = strategy.evaluate(Snapshot(), {"threshold": 0.5})
    second = strategy.evaluate(Snapshot(), {"threshold": 0.6})
    assert first.signal_hash != second.signal_hash


def test_disabled_strategy_is_not_returned_as_enabled():
    registry = StrategyRegistry()
    registry.register(FixtureStrategy())
    registry.disable("fixture.core", "1.0.0")
    assert registry.enabled_strategies() == ()
    registry.enable("fixture.core", "1.0.0")
    assert len(registry.enabled_strategies()) == 1


def test_required_capabilities_are_stably_sorted():
    registry = StrategyRegistry()
    registry.register(FixtureStrategy())
    assert registry.required_capabilities() == ("market.daily.qfq",)


def test_registry_modules_do_not_import_provider_session_or_persistence():
    strategy_root = Path(__file__).parents[1] / "app" / "strategies"
    prohibited = ("app.providers", "sqlalchemy", "app.services")
    for path in strategy_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        assert not any(name.startswith(prohibited) for name in imports), (path, imports)


def test_strategy_interface_accepts_snapshot_protocol_not_provider_or_session():
    for method in (TradingStrategy.is_applicable, TradingStrategy.evaluate):
        signature = str(inspect.signature(method))
        assert "StrategySnapshot" in signature
        assert "Provider" not in signature
        assert "Session" not in signature


@pytest.mark.parametrize(
    ("strategy_id", "version"),
    [("", "1.0.0"), ("Bad ID", "1.0.0"), ("valid.id", ""), ("valid.id", "v1")],
)
def test_invalid_strategy_identity_is_rejected(strategy_id, version):
    data = FixtureStrategy().manifest().model_dump()
    data["strategy_id"] = strategy_id
    data["version"] = version
    with pytest.raises(ValueError):
        StrategyManifest(**data)
