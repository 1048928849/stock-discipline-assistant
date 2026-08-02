from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.domain.features import Feature, FeatureSnapshot, FeatureValue
from app.domain.strategy import StrategyEvaluationResult, StrategyRuleStatus
from app.services.price_planner import plan_prices


def _strategy():
    return StrategyEvaluationResult(
        strategy_id="platform_breakout_pullback",
        strategy_version="1.0.0",
        overall_status=StrategyRuleStatus.PASS,
        rules=(),
    )


def _snapshot(**changes):
    pattern = {
        "valid_platform": True,
        "platform_upper": 10.5,
        "platform_lower": 9.5,
        "turn_trigger_price": 10.6,
        "atr14": 0.4,
    }
    pattern.update(changes)
    return FeatureSnapshot(
        symbol="300502",
        as_of="2026-08-01",
        features=(Feature("platform_structure", FeatureValue(pattern)),),
    )


def _frame(low=9.7):
    index = pd.to_datetime([date.today() - timedelta(days=i) for i in range(9, -1, -1)])
    return pd.DataFrame({"Low": [low] * 10}, index=index)


PARAMETERS = {"atr_buffer_multiple": 0.5, "minimum_reward_risk": 2.0}


def test_normal_price_plan():
    result = plan_prices(
        strategy_result=_strategy(),
        feature_snapshot=_snapshot(),
        market_data=_frame(),
        parameters=PARAMETERS,
    )

    assert result.entry_reference == 10.6
    assert result.entry_zone.low == 10.52
    assert result.entry_zone.high == 10.68
    assert result.stop_price == 9.5
    assert result.first_target == 12.8
    assert result.second_target == 13.9


def test_missing_market_data_has_no_stop():
    result = plan_prices(
        strategy_result=_strategy(),
        feature_snapshot=_snapshot(),
        market_data=None,
        parameters=PARAMETERS,
    )

    assert result.stop_price is None
    assert result.entry_zone.low is None


def test_invalid_platform_has_no_price_plan():
    result = plan_prices(
        strategy_result=_strategy(),
        feature_snapshot=_snapshot(valid_platform=False),
        market_data=_frame(),
        parameters=PARAMETERS,
    )

    assert result.entry_reference is None
    assert result.target_price is None


def test_stop_not_below_entry_has_no_target():
    result = plan_prices(
        strategy_result=_strategy(),
        feature_snapshot=_snapshot(turn_trigger_price=10),
        market_data=_frame(low=11),
        parameters=PARAMETERS,
    )

    assert result.stop_price == 10.8
    assert result.first_target is None
    assert result.reward_risk is None


def test_reward_risk_and_rounding_match_original_formula():
    result = plan_prices(
        strategy_result=_strategy(),
        feature_snapshot=_snapshot(turn_trigger_price=10.12345, atr14=0.33333),
        market_data=_frame(low=9.8),
        parameters=PARAMETERS,
    )

    expected = (result.first_target - result.entry_reference) / (
        result.entry_reference - result.stop_price
    )
    assert result.entry_reference == 10.1235
    assert result.entry_zone.low == 10.0568
    assert result.entry_zone.high == 10.1902
    assert round(result.reward_risk, 4) == round(expected, 4)
