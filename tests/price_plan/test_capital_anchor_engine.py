from __future__ import annotations

import pandas as pd

from app.services.capital_anchor_engine import calculate_market_structure


def _frame(rows: int = 140) -> pd.DataFrame:
    prices = [9 + index * 0.01 for index in range(rows)]
    return pd.DataFrame(
        {
            "Open": prices,
            "High": [value + 0.2 for value in prices],
            "Low": [value - 0.2 for value in prices],
            "Close": prices,
            "Volume": [1000 + index * 10 for index in range(rows)],
        }
    )


def test_builds_dynamic_cost_anchors_and_ordered_zones():
    result = calculate_market_structure(_frame(), atr=0.3, platform_lower=9.8, platform_upper=10.2)

    assert result is not None
    assert {item.anchor_id for item in result.anchors} >= {
        "vwap_20",
        "vwap_60",
        "vwap_120",
        "platform_lower",
        "platform_upper",
    }
    assert tuple(item.center for item in result.support_zones) == tuple(
        sorted((item.center for item in result.support_zones), reverse=True)
    )
    assert result.extreme_stop is not None
    assert result.extreme_stop < min(item.low for item in result.support_zones)


def test_volume_changes_rolling_cost_anchor():
    baseline = _frame()
    concentrated = _frame()
    concentrated.loc[concentrated.index[-10:], "Volume"] *= 100

    first = calculate_market_structure(baseline, atr=0.3)
    second = calculate_market_structure(concentrated, atr=0.3)

    assert first is not None and second is not None
    first_vwap = next(item.price for item in first.anchors if item.anchor_id == "vwap_20")
    second_vwap = next(item.price for item in second.anchors if item.anchor_id == "vwap_20")
    assert second_vwap > first_vwap


def test_missing_volume_does_not_invent_structure():
    frame = _frame().drop(columns=["Volume"])

    assert calculate_market_structure(frame, atr=0.3) is None


def test_price_plan_exposes_structure_without_replacing_frozen_stop():
    from app.domain.features import Feature, FeatureSnapshot, FeatureValue
    from app.domain.strategy import StrategyEvaluationResult, StrategyRuleStatus
    from app.services.price_planner import plan_prices

    pattern = {
        "valid_platform": True,
        "platform_upper": 10.5,
        "platform_lower": 9.5,
        "turn_trigger_price": 10.6,
        "atr14": 0.4,
    }
    snapshot = FeatureSnapshot(
        "300502", "2026-08-01", (Feature("platform_structure", FeatureValue(pattern)),)
    )
    strategy = StrategyEvaluationResult(
        "platform_breakout_pullback", "1.0.0", StrategyRuleStatus.PASS, ()
    )

    result = plan_prices(
        strategy_result=strategy,
        feature_snapshot=snapshot,
        market_data=_frame(),
        parameters={"atr_buffer_multiple": 0.5, "minimum_reward_risk": 2.0},
    )

    # 原公式仍为 max(平台下沿, 近10日低点) - 0.5 ATR。
    assert result.stop_price == 9.9
    assert result.market_structure is not None
    assert result.market_structure.extreme_stop != result.stop_price
