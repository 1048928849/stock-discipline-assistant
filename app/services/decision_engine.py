from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.decision import (
    DecisionContext,
    DecisionResult,
    RiskEvaluationResult,
    TradeDecision,
)
from app.domain.strategy import (
    StrategyEvaluationResult,
    StrategyRuleStatus,
)

CRITICAL_UNKNOWN_GATES = (
    "market",
    "sector",
    "large_cycle",
    "platform",
    "stop",
    "reward_risk",
    "data",
)
HARD_FAIL_GATES = (
    "market",
    "sector",
    "large_cycle",
    "platform",
    "pullback",
    "stop",
    "reward_risk",
    "position",
)

DECISION_LABELS = {
    TradeDecision.BUY_PROHIBITED: "禁止买入",
    TradeDecision.WAIT: "等待观察",
    TradeDecision.TRIAL_ALLOWED: "允许试仓",
    TradeDecision.HOLD: "允许持有",
    TradeDecision.CONDITIONAL_ADD: "允许条件式加仓",
    TradeDecision.REDUCE: "建议减仓",
    TradeDecision.PLAN_INVALID_EXIT: "计划失效，需要退出",
}


def _legacy_plan_status(context: DecisionContext) -> str:
    statuses = context.risk_result.gate_statuses
    hard_fail = context.strategy_result.overall_status is StrategyRuleStatus.FAIL or any(
        statuses.get(code) == "不通过" for code in HARD_FAIL_GATES
    )
    critical_unknown = (
        context.strategy_result.overall_status is StrategyRuleStatus.UNKNOWN
        or any(statuses.get(code) == "无法判断" for code in CRITICAL_UNKNOWN_GATES)
    )
    triggers_waiting = context.strategy_result.overall_status is StrategyRuleStatus.WAIT or any(
        statuses.get(code) != "通过" for code in ("breakout", "pullback", "turn_stronger")
    )
    if hard_fail:
        return "NO_TRADE"
    if critical_unknown:
        return "INSUFFICIENT_DATA"
    if triggers_waiting:
        return "WAIT"
    return "READY"


def _number(value: Any) -> float | None:
    return float(value) if value is not None else None


def evaluate_decision(context: DecisionContext) -> DecisionResult:
    legacy_status = _legacy_plan_status(context)
    position = context.position_context
    has_position = bool(position.get("has_position"))
    current_price = _number(position.get("current_price"))
    cost_price = _number(position.get("cost_price"))
    stop_price = _number(position.get("stop_loss_price"))
    target_price = _number(position.get("target_price"))
    floating_profit = position.get("floating_profit")
    if "floating_profit" not in position:
        floating_profit = (
            current_price is not None and cost_price is not None and current_price > cost_price
            if has_position
            else None
        )
    hard_stop_triggered = bool(position.get("hard_stop_triggered"))
    if "hard_stop_triggered" not in position:
        hard_stop_triggered = bool(
            has_position
            and current_price is not None
            and stop_price is not None
            and current_price <= stop_price
        )
    first_reduction_triggered = bool(position.get("first_reduction_triggered"))
    if "first_reduction_triggered" not in position:
        first_reduction_triggered = bool(
            has_position
            and current_price is not None
            and target_price is not None
            and current_price >= target_price
        )
    confirmation_add_allowed = bool(position.get("confirmation_add_allowed"))
    if "confirmation_add_allowed" not in position:
        confirmation_add_allowed = bool(
            has_position
            and floating_profit
            and legacy_status == "READY"
            and current_price is not None
            and current_price > (stop_price or 0)
            and not hard_stop_triggered
        )
    if has_position and not confirmation_add_allowed and legacy_status == "READY":
        legacy_status = "WAIT"

    if has_position:
        if hard_stop_triggered or bool(position.get("platform_broken")):
            decision = TradeDecision.PLAN_INVALID_EXIT
        elif first_reduction_triggered:
            decision = TradeDecision.REDUCE
        elif confirmation_add_allowed:
            decision = TradeDecision.CONDITIONAL_ADD
        elif legacy_status == "NO_TRADE":
            decision = TradeDecision.REDUCE
        else:
            decision = TradeDecision.HOLD
    elif legacy_status == "READY" and context.risk_result.entry_capacity_allowed:
        decision = TradeDecision.TRIAL_ALLOWED
    elif legacy_status == "NO_TRADE":
        decision = TradeDecision.BUY_PROHIBITED
    else:
        decision = TradeDecision.WAIT

    next_actions = tuple(context.market_context.get("next_actions", ())) or (
        "等待下一次有效触发并重新分析。",
    )
    blocking = tuple(context.risk_result.blocking_factors)
    evidence = (
        f"兼容计划状态：{legacy_status}",
        f"持仓模式：{'持仓' if has_position else '空仓'}",
    )
    return DecisionResult(
        decision=decision,
        reason=DECISION_LABELS[decision],
        evidence=evidence,
        blocking_factors=blocking,
        next_actions=next_actions,
        legacy_plan_status=legacy_status,
        label=DECISION_LABELS[decision],
        position_evidence={
            "floating_profit": floating_profit,
            "hard_stop_triggered": hard_stop_triggered,
            "first_reduction_triggered": first_reduction_triggered,
            "confirmation_add_allowed": confirmation_add_allowed,
        },
    )


def legacy_decision_dict(result: DecisionResult) -> dict[str, str]:
    return {
        "status": result.decision.value,
        "label": result.label,
        "next_action": result.next_actions[0],
        "rule_authority": "最终状态、仓位、止损和加仓均由确定性规则引擎决定，AI无权修改。",
    }


def compatibility_context(
    *,
    strategy_result: StrategyEvaluationResult,
    gate_statuses: Mapping[str, str],
    entry_capacity_allowed: bool,
    position_context: Mapping[str, Any],
    next_actions: Sequence[str] = (),
    blocking_factors: Sequence[str] = (),
) -> DecisionContext:
    return DecisionContext(
        strategy_result=strategy_result,
        risk_result=RiskEvaluationResult(
            gate_statuses=gate_statuses,
            entry_capacity_allowed=entry_capacity_allowed,
            blocking_factors=tuple(blocking_factors),
        ),
        market_context={"next_actions": tuple(next_actions)},
        position_context=position_context,
    )


def preview_decision_context(preview: Mapping[str, Any], position_mode: str) -> DecisionContext:
    legacy_status = str(preview["status"])
    strategy_status = {
        "NO_TRADE": StrategyRuleStatus.FAIL,
        "INSUFFICIENT_DATA": StrategyRuleStatus.UNKNOWN,
        "WAIT": StrategyRuleStatus.WAIT,
        "READY": StrategyRuleStatus.PASS,
    }[legacy_status]
    strategy_result = StrategyEvaluationResult(
        strategy_id="platform_breakout_pullback",
        strategy_version=str(preview["rule"]["version"]),
        overall_status=strategy_status,
        rules=(),
    )
    holding = preview["existing_position"]
    return compatibility_context(
        strategy_result=strategy_result,
        gate_statuses={item["code"]: item["status"] for item in preview["gates"]},
        entry_capacity_allowed=bool(preview["current_buy_allowed"]),
        position_context={
            "has_position": position_mode == "持仓",
            "floating_profit": holding["floating_profit"],
            "hard_stop_triggered": holding["hard_stop_triggered"],
            "first_reduction_triggered": holding["first_reduction_triggered"],
            "confirmation_add_allowed": holding["confirmation_add_allowed"],
            # 保持旧入口在 pattern=None 的持仓场景中的既有访问行为；阶段 E 不修复。
            "platform_broken": preview.get("pattern", {}).get("platform_broken")
            if position_mode == "持仓"
            else False,
        },
        next_actions=preview["next_observations"],
        blocking_factors=preview["status_reason"],
    )
