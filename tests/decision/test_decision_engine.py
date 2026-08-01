from __future__ import annotations

import pytest

from app.domain.decision import DecisionContext, RiskEvaluationResult, TradeDecision
from app.domain.strategy import StrategyEvaluationResult, StrategyRuleStatus
from app.services.decision_engine import evaluate_decision


def _strategy(status: StrategyRuleStatus) -> StrategyEvaluationResult:
    return StrategyEvaluationResult(
        strategy_id="platform_breakout_pullback",
        strategy_version="1.0.0",
        overall_status=status,
        rules=(),
    )


def _context(
    strategy_status: StrategyRuleStatus,
    *,
    risk_passed: bool = False,
    position: dict | None = None,
) -> DecisionContext:
    gates = (
        {"breakout": "通过", "pullback": "通过", "turn_stronger": "通过"}
        if risk_passed
        else {}
    )
    return DecisionContext(
        strategy_result=_strategy(strategy_status),
        risk_result=RiskEvaluationResult(
            gate_statuses=gates,
            entry_capacity_allowed=risk_passed,
        ),
        market_context={"next_actions": ("保持原计划",)},
        position_context=position or {"has_position": False},
    )


@pytest.mark.parametrize(
    ("strategy_status", "risk_passed", "expected"),
    [
        (StrategyRuleStatus.FAIL, False, TradeDecision.BUY_PROHIBITED),
        (StrategyRuleStatus.WAIT, False, TradeDecision.WAIT),
        (StrategyRuleStatus.PASS, True, TradeDecision.TRIAL_ALLOWED),
    ],
)
def test_empty_position_decisions(strategy_status, risk_passed, expected):
    result = evaluate_decision(_context(strategy_status, risk_passed=risk_passed))

    assert result.decision is expected


def test_normal_holding_is_hold():
    result = evaluate_decision(
        _context(
            StrategyRuleStatus.WAIT,
            position={"has_position": True, "current_price": 10, "cost_price": 11},
        )
    )

    assert result.decision is TradeDecision.HOLD


def test_stop_trigger_is_plan_invalid_exit():
    result = evaluate_decision(
        _context(
            StrategyRuleStatus.PASS,
            risk_passed=True,
            position={
                "has_position": True,
                "current_price": 9,
                "cost_price": 8,
                "stop_loss_price": 9.5,
            },
        )
    )

    assert result.decision is TradeDecision.PLAN_INVALID_EXIT


def test_target_trigger_is_reduce():
    result = evaluate_decision(
        _context(
            StrategyRuleStatus.PASS,
            risk_passed=True,
            position={
                "has_position": True,
                "current_price": 12,
                "cost_price": 9,
                "stop_loss_price": 8,
                "target_price": 11,
            },
        )
    )

    assert result.decision is TradeDecision.REDUCE


def test_profitable_ready_holding_allows_conditional_add():
    result = evaluate_decision(
        _context(
            StrategyRuleStatus.PASS,
            risk_passed=True,
            position={
                "has_position": True,
                "current_price": 10,
                "cost_price": 9,
                "stop_loss_price": 8,
            },
        )
    )

    assert result.decision is TradeDecision.CONDITIONAL_ADD


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "hard_stop_triggered": True,
                "first_reduction_triggered": True,
                "confirmation_add_allowed": True,
            },
            TradeDecision.PLAN_INVALID_EXIT,
        ),
        (
            {
                "hard_stop_triggered": False,
                "first_reduction_triggered": True,
                "confirmation_add_allowed": True,
            },
            TradeDecision.REDUCE,
        ),
        (
            {
                "hard_stop_triggered": False,
                "first_reduction_triggered": False,
                "confirmation_add_allowed": True,
            },
            TradeDecision.CONDITIONAL_ADD,
        ),
        (
            {
                "hard_stop_triggered": False,
                "first_reduction_triggered": False,
                "confirmation_add_allowed": False,
            },
            TradeDecision.HOLD,
        ),
    ],
)
def test_holding_priority_stop_reduce_add_hold(overrides, expected):
    position = {"has_position": True, "floating_profit": True, **overrides}

    result = evaluate_decision(
        _context(StrategyRuleStatus.PASS, risk_passed=True, position=position)
    )

    assert result.decision is expected


def test_decision_engine_does_not_write_database(session):
    before = (set(session.new), set(session.dirty), set(session.deleted))

    evaluate_decision(_context(StrategyRuleStatus.PASS, risk_passed=True))

    assert (set(session.new), set(session.dirty), set(session.deleted)) == before
