from __future__ import annotations

from copy import deepcopy

import pytest

from app.domain.features import Feature, FeatureQuality, FeatureSnapshot, FeatureValue
from app.domain.strategy import StrategyContext, StrategyRuleStatus
from app.strategies.platform_breakout import PlatformBreakoutPullbackStrategy


def _pattern(**overrides):
    value = {
        "platform_days": 20,
        "platform_upper": 10.0,
        "platform_lower": 9.0,
        "platform_range_pct": 11.11,
        "valid_platform": True,
        "breakout": {
            "date": "2026-07-20",
            "price": 10.2,
            "volume_ratio": 1.8,
            "volume_confirmed": True,
        },
        "pullback_seen": True,
        "pullback_range": [9.9, 10.3],
        "pullback_volume_ratio": 0.7,
        "pullback_shrinking": True,
        "platform_broken": False,
        "turn_trigger_price": 10.3,
        "turned_stronger": True,
    }
    value.update(overrides)
    return value


def _timeframes(**overrides):
    value = {
        "daily_state": {"state": "向上", "evidence": "日线事实"},
        "weekly_state": {"state": "向上", "evidence": "周线事实"},
        "monthly_state": {"state": "向上", "evidence": "月线事实"},
        "large_state": "向上",
    }
    value.update(overrides)
    return value


def _context(pattern=None, timeframes=None, quality=FeatureQuality.GOOD, position=None):
    snapshot = FeatureSnapshot(
        symbol="300502",
        as_of="2026-07-31",
        features=(
            Feature(
                "platform_structure",
                FeatureValue(
                    value=_pattern() if pattern is None else pattern,
                    source_ids=("fixture",),
                    data_time="2026-07-31T15:00:00",
                    quality=quality,
                ),
            ),
            Feature(
                "multi_timeframe",
                FeatureValue(
                    value=_timeframes() if timeframes is None else timeframes,
                    source_ids=("fixture",),
                    data_time="2026-07-31T15:00:00",
                    quality=quality,
                ),
            ),
        ),
    )
    return StrategyContext(
        symbol="300502",
        feature_snapshot=snapshot,
        parameters={"platform_min_days": 20},
        position_mode="持仓" if position else "空仓",
        position_context=position or {},
        market_context={},
        sector_context={},
    )


def _rules(result):
    return {rule.rule_id: rule for rule in result.rules}


@pytest.mark.parametrize(
    ("pattern", "rule_id", "expected"),
    [
        (_pattern(valid_platform=False), "platform_structure", StrategyRuleStatus.FAIL),
        (_pattern(breakout=None), "breakout_volume_confirmation", StrategyRuleStatus.UNKNOWN),
        (
            _pattern(
                breakout={
                    "date": "2026-07-20",
                    "price": 10.2,
                    "volume_ratio": 1.1,
                    "volume_confirmed": False,
                }
            ),
            "breakout_volume_confirmation",
            StrategyRuleStatus.WAIT,
        ),
        (_pattern(), "breakout_volume_confirmation", StrategyRuleStatus.PASS),
        (
            _pattern(pullback_shrinking=False),
            "pullback_structure",
            StrategyRuleStatus.WAIT,
        ),
        (_pattern(), "pullback_structure", StrategyRuleStatus.PASS),
        (
            _pattern(platform_broken=True),
            "pullback_structure",
            StrategyRuleStatus.FAIL,
        ),
        (
            _pattern(turned_stronger=False),
            "turn_stronger_confirmation",
            StrategyRuleStatus.UNKNOWN,
        ),
        (_pattern(), "turn_stronger_confirmation", StrategyRuleStatus.PASS),
    ],
)
def test_platform_breakout_rule_outcomes(pattern, rule_id, expected):
    result = PlatformBreakoutPullbackStrategy().evaluate(_context(pattern=pattern))

    assert _rules(result)[rule_id].status is expected


def test_missing_features_produce_unknown():
    missing = {"missing_reason": "market_data_missing"}
    context = _context(pattern=missing, timeframes=missing, quality=FeatureQuality.MISSING)

    result = PlatformBreakoutPullbackStrategy().evaluate(context)

    assert result.overall_status is StrategyRuleStatus.UNKNOWN
    assert all(rule.status is StrategyRuleStatus.UNKNOWN for rule in result.rules[1:])


def test_position_context_does_not_add_position_calculation_to_result():
    context = _context(position={"quantity": 1000, "cost_price": 9.8})

    result = PlatformBreakoutPullbackStrategy().evaluate(context)

    assert result.overall_status is StrategyRuleStatus.PASS
    assert not hasattr(result, "quantity")
    assert not hasattr(result, "position_size")


def test_evaluation_does_not_mutate_feature_snapshot():
    context = _context()
    before = deepcopy(context.feature_snapshot.to_dict())

    PlatformBreakoutPullbackStrategy().evaluate(context)

    assert context.feature_snapshot.to_dict() == before


def test_evaluation_does_not_write_database(session):
    context = _context()
    before = (set(session.new), set(session.dirty), set(session.deleted))

    PlatformBreakoutPullbackStrategy().evaluate(context)

    assert (set(session.new), set(session.dirty), set(session.deleted)) == before
