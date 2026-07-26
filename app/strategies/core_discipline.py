from __future__ import annotations

from app.decision.contracts import TradeDecisionContext, TradeDecisionResult
from app.decision.engine import build_trade_decision
from app.strategies.base import StrategySnapshot, TradingStrategy
from app.strategies.contracts import StrategyManifest, StrategySignal
from app.strategies.hashing import implementation_hash, parameter_hash


class CoreDisciplineStrategy(TradingStrategy):
    strategy_id = "core.discipline"
    strategy_version = "1.0.0"

    def manifest(self) -> StrategyManifest:
        return StrategyManifest(
            strategy_id=self.strategy_id,
            version=self.strategy_version,
            name="Core Discipline",
            description="Existing deterministic discipline decision under global risk gates.",
            time_horizon="daily-swing",
            required_capabilities=(
                "announcement.catalog",
                "fundamental.profile",
                "market.amount.daily",
                "market.breadth.daily",
                "market.daily.qfq",
                "market.industry.daily",
                "market.intraday.60m",
                "market.turnover.daily",
            ),
            parameter_schema={
                "type": "object",
                "properties": {
                    "risk_multiplier": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    }
                },
                "required": ["risk_multiplier"],
                "additionalProperties": False,
            },
            default_parameters={"risk_multiplier": 1},
            applicable_market_regimes=(
                "CONTRACTION",
                "DIVERGENCE",
                "EXPANSION",
                "PANIC",
                "REPAIR",
            ),
            source_evidence=(
                "existing deterministic trade_plan_generator",
                "Product V1 global risk constraints",
            ),
            implementation_hash=implementation_hash(type(self)),
        )

    def is_applicable(self, snapshot: StrategySnapshot, parameters: dict) -> bool:
        self.validate_parameters(parameters)
        return all(
            snapshot.has_capability(capability)
            for capability in self.required_capabilities()
        )

    def evaluate(self, snapshot: StrategySnapshot, parameters: dict) -> StrategySignal:
        validated = self.validate_parameters(parameters)
        missing = tuple(
            capability
            for capability in self.required_capabilities()
            if not snapshot.has_capability(capability)
        )
        references = tuple(
            sorted(
                {
                    reference
                    for capability in self.required_capabilities()
                    for reference in snapshot.evidence_refs_for(capability)
                }
            )
        )
        applicable = not missing
        return StrategySignal(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            applicable=applicable,
            signal_type="CORE_CONTEXT_READY" if applicable else "BLOCKED",
            signal_strength=validated["risk_multiplier"] if applicable else 0,
            entry_assessment={
                "snapshot_hash": snapshot.snapshot_hash,
                "capabilities_ready": applicable,
            },
            invalidation="required capability or global risk constraint changes",
            suggested_risk_level="GLOBAL_ENGINE_REQUIRED",
            holding_horizon="daily-swing",
            evidence_refs=references,
            blocked_reasons=tuple(f"missing {capability}" for capability in missing),
            parameter_hash=parameter_hash(validated),
        )

    def evaluate_trade_decision(
        self, context: TradeDecisionContext
    ) -> TradeDecisionResult:
        return build_trade_decision(context)


__all__ = ["CoreDisciplineStrategy"]
