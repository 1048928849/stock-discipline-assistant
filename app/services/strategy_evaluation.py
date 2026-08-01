from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.features import FeatureSnapshot
from app.domain.strategy import StrategyContext, StrategyEvaluationResult, StrategyRuleStatus
from app.strategies import default_strategy_registry

STRATEGY_ID = "platform_breakout_pullback"

_GATE_RULES = (
    ("large_cycle", "large_cycle_direction"),
    ("platform", "platform_structure"),
    ("breakout", "breakout_volume_confirmation"),
    ("pullback", "pullback_structure"),
    ("turn_stronger", "turn_stronger_confirmation"),
)


def evaluate_platform_breakout(
    *,
    symbol: str,
    feature_snapshot: FeatureSnapshot,
    parameters: Mapping[str, Any],
    position_mode: str,
    position_context: Mapping[str, Any],
    market_context: Mapping[str, Any],
    sector_context: Mapping[str, Any],
) -> StrategyEvaluationResult:
    context = StrategyContext(
        symbol=symbol,
        feature_snapshot=feature_snapshot,
        parameters=parameters,
        position_mode=position_mode,
        position_context=position_context,
        market_context=market_context,
        sector_context=sector_context,
    )
    return default_strategy_registry().get(STRATEGY_ID).evaluate(context)


def strategy_gates(result: StrategyEvaluationResult) -> list[dict[str, Any]]:
    rules = {rule.rule_id: rule for rule in result.rules}
    gates = []
    for code, rule_id in _GATE_RULES:
        rule = rules[rule_id]
        evidence = rule.evidence[0]
        if rule.status is StrategyRuleStatus.PASS:
            legacy_status = "通过"
        elif rule.status is StrategyRuleStatus.FAIL:
            legacy_status = "不通过"
        elif rule.status is StrategyRuleStatus.WAIT:
            legacy_status = "警告"
        else:
            legacy_status = "无法判断"
        gates.append(
            {
                "code": code,
                "name": rule.name,
                "status": legacy_status,
                "evidence": rule.reason,
                "missing_conditions": list(rule.missing_data),
                "source": evidence.source_id,
                "data_time": evidence.data_time,
            }
        )
    return gates
