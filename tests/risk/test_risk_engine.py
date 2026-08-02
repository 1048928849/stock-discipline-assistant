from __future__ import annotations

import pytest

from app.domain.risk import RiskContext, RiskStatus
from app.services.risk_engine import evaluate_risk, floor_lot


def _context(
    *,
    equity=100_000,
    cash=100_000,
    risk_pct=10,
    max_position_pct=100,
    max_total_position_pct=100,
    max_industry_position_pct=100,
    existing_value=0,
    total_value=0,
    sector_value=0,
    entry=10,
    stop=9,
):
    return RiskContext(
        account_context={
            "equity": equity,
            "available_cash": cash,
            "risk_pct": risk_pct,
            "max_position_pct": max_position_pct,
            "max_total_position_pct": max_total_position_pct,
            "max_industry_position_pct": max_industry_position_pct,
            "trial_position_ratio": 0.3333,
        },
        position_context={
            "existing_symbol_value": existing_value,
            "total_position_value": total_value,
        },
        entry_context={"entry_price": entry},
        stop_context={
            "stop_price": stop,
            "maximum_stop_distance_pct": 20,
            "reward_risk": 2,
            "minimum_reward_risk": 2,
        },
        market_context={"risk": "低"},
        sector_context={"sector_position_value": sector_value},
    )


def _limits(result):
    return {item.name: item.limit for item in result.constraints}


def test_normal_risk_calculation():
    result = evaluate_risk(_context())

    assert result.status is RiskStatus.PASS
    assert result.allowed_quantity == 10_000
    assert result.calculation_details["risk_budget"] == 10_000
    assert result.calculation_details["per_share_risk"] == 1


def test_risk_budget_is_binding_constraint():
    result = evaluate_risk(_context(risk_pct=1))

    assert result.allowed_quantity == 1_000
    assert result.binding_constraint == "risk_budget"


def test_cash_limit():
    result = evaluate_risk(_context(cash=2_500))

    assert result.allowed_quantity == 200
    assert _limits(result)["cash"] == 200
    assert result.binding_constraint == "cash"


def test_single_position_limit():
    result = evaluate_risk(_context(max_position_pct=5))

    assert result.allowed_quantity == 500
    assert _limits(result)["single_position"] == 500


def test_sector_exposure_limit():
    result = evaluate_risk(_context(max_industry_position_pct=4))

    assert result.allowed_quantity == 400
    assert _limits(result)["sector_exposure"] == 400


def test_total_position_limit():
    result = evaluate_risk(_context(max_total_position_pct=3))

    assert result.allowed_quantity == 300
    assert _limits(result)["total_position"] == 300


@pytest.mark.parametrize(("raw", "expected"), [(1_055.9, 1_000), (99.9, 0), (-1, 0)])
def test_a_share_lot_rounding(raw, expected):
    assert floor_lot(raw) == expected


def test_missing_stop_data():
    result = evaluate_risk(_context(stop=None))

    assert result.status is RiskStatus.INSUFFICIENT_DATA
    assert result.allowed_quantity == 0
    assert "硬止损" in result.blocking_reasons


def test_missing_entry_data():
    result = evaluate_risk(_context(entry=None))

    assert result.status is RiskStatus.INSUFFICIENT_DATA
    assert result.allowed_quantity == 0
    assert "有效买入价" in result.blocking_reasons


def test_existing_holding_reduces_single_position_capacity():
    result = evaluate_risk(_context(max_position_pct=20, existing_value=15_000))

    assert result.allowed_quantity == 500
    assert _limits(result)["single_position"] == 500


def test_risk_engine_does_not_write_database(session):
    before = (set(session.new), set(session.dirty), set(session.deleted))

    evaluate_risk(_context())

    assert (set(session.new), set(session.dirty), set(session.deleted)) == before


def _with_account_risk(context: RiskContext, **changes) -> RiskContext:
    return RiskContext(
        account_context={**context.account_context, **changes},
        position_context=context.position_context,
        entry_context=context.entry_context,
        stop_context=context.stop_context,
        market_context=context.market_context,
        sector_context=context.sector_context,
    )


def test_account_drawdown_circuit_breaker_blocks_new_risk():
    result = evaluate_risk(
        _with_account_risk(_context(), current_drawdown_pct=8, max_account_drawdown_pct=8)
    )

    assert result.status is RiskStatus.BLOCKED
    assert result.allowed_quantity == 0
    assert _limits(result)["account_drawdown_circuit_breaker"] == 0
    assert "账户回撤达到熔断阈值" in result.blocking_reasons[0]


def test_portfolio_open_risk_reduces_capacity():
    context = _with_account_risk(_context(risk_pct=10), max_portfolio_risk_pct=3)
    context = RiskContext(
        account_context=context.account_context,
        position_context={**context.position_context, "open_risk_amount": 2_500},
        entry_context=context.entry_context,
        stop_context=context.stop_context,
        market_context=context.market_context,
        sector_context=context.sector_context,
    )

    result = evaluate_risk(context)

    assert result.allowed_quantity == 500
    assert result.binding_constraint == "portfolio_open_risk"


def test_loss_streak_reduces_effective_risk_budget():
    result = evaluate_risk(_with_account_risk(_context(risk_pct=2), consecutive_losses=3))

    assert result.allowed_quantity == 1_000
    assert result.calculation_details["effective_risk_budget"] == 1_000


def test_gap_stress_uses_worse_exit_when_price_jumps_over_stop():
    result = evaluate_risk(_context(risk_pct=1, entry=10, stop=9.8))

    assert result.calculation_details["stress_losses"]["gap_down_3_pct"] == 1500
    assert result.calculation_details["stress_losses"]["gap_down_10_pct"] == 5000


def test_missing_existing_position_stop_blocks_new_portfolio_risk():
    context = _context()
    context = RiskContext(
        account_context=context.account_context,
        position_context={
            **context.position_context,
            "portfolio_risk_complete": False,
            "holdings_without_stop": 1,
        },
        entry_context=context.entry_context,
        stop_context=context.stop_context,
        market_context=context.market_context,
        sector_context=context.sector_context,
    )

    result = evaluate_risk(context)

    assert result.status is RiskStatus.BLOCKED
    assert _limits(result)["portfolio_stop_completeness"] == 0
    assert "缺少硬止损" in result.blocking_reasons[0]
