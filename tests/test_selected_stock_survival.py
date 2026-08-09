from __future__ import annotations

from decimal import Decimal

from app.selected_stock.contracts import ContextStatus, StockRole, SurvivalRuleStatus
from app.selected_stock.survival import evaluate_survival_discipline


def _indicators(**changes):
    value = {
        "bullish_alignment": True,
        "bearish_alignment": False,
        "returns": {"5": Decimal("0.02")},
        "sma": {"20": Decimal("10")},
        "atr_pct": Decimal("0.02"),
        "momentum_accelerating": False,
        "volume_ratio_20": Decimal("1"),
        "down_on_volume": False,
        "support_zone": [Decimal("9"), Decimal("10")],
        "latest_candle": {
            "price_percentile_250": Decimal("0.5"),
            "body_pct": Decimal("0.03"),
            "upper_wick_pct": Decimal("0.01"),
            "close_position": Decimal("0.8"),
        },
    }
    value.update(changes)
    return value


def _evaluate(indicators=None, **changes):
    values = {
        "indicators": indicators or _indicators(),
        "current_price": Decimal("10"),
        "entry_high": Decimal("10"),
        "hard_stop": Decimal("9"),
        "proposed_position_pct": Decimal("10"),
        "has_position": False,
        "market_status": ContextStatus.AVAILABLE,
        "industry_status": ContextStatus.AVAILABLE,
        "stock_role": StockRole.LEADER,
    }
    values.update(changes)
    return evaluate_survival_discipline(**values)


def _rule(results, code):
    return next(item for item in results if item.rule_code == code)


def test_chase_risk_uses_price_ma_entry_atr_and_acceleration():
    results = _evaluate(
        _indicators(
            returns={"5": Decimal("0.15")},
            sma={"20": Decimal("8")},
            atr_pct=Decimal("0.02"),
            momentum_accelerating=True,
        ),
        current_price=Decimal("12"),
        entry_high=Decimal("10"),
    )
    rule = _rule(results, "CHASE_RISK")
    assert rule.status == SurvivalRuleStatus.BLOCK
    assert rule.evidence["trigger_count"] >= 3


def test_high_level_volume_stall_is_warn_or_block_from_candle_evidence():
    results = _evaluate(
        _indicators(
            volume_ratio_20=Decimal("2"),
            latest_candle={
                "price_percentile_250": Decimal("0.95"),
                "body_pct": Decimal("0.01"),
                "upper_wick_pct": Decimal("0.03"),
                "close_position": Decimal("0.2"),
            },
        )
    )
    assert _rule(results, "HIGH_VOLUME_STALL").status == SurvivalRuleStatus.BLOCK


def test_volume_decline_breaking_support_is_blocked():
    results = _evaluate(
        _indicators(
            down_on_volume=True,
            sma={"20": Decimal("11")},
            support_zone=[Decimal("10.5"), Decimal("11")],
        ),
        current_price=Decimal("10"),
    )
    assert _rule(results, "VOLUME_DECLINE_BREAKDOWN").status == SurvivalRuleStatus.BLOCK


def test_hard_stop_has_priority_and_has_no_override_sources():
    results = _evaluate(current_price=Decimal("8.99"), hard_stop=Decimal("9"))
    rule = _rule(results, "HARD_STOP_PRIORITY")
    assert rule.status == SurvivalRuleStatus.BLOCK
    assert rule.severity == "CRITICAL"
    assert rule.evidence["override_sources_allowed"] == []


def test_unknown_role_never_claims_leader_and_is_insufficient():
    results = _evaluate(
        industry_status=ContextStatus.INDUSTRY_CONTEXT_UNAVAILABLE,
        stock_role=StockRole.UNKNOWN,
    )
    rule = _rule(results, "INDUSTRY_ROLE_RISK")
    assert rule.status == SurvivalRuleStatus.INSUFFICIENT_DATA
    assert rule.evidence["stock_role"] == "UNKNOWN"


def test_survival_contract_has_exactly_six_versioned_rules():
    results = _evaluate()
    assert len(results) == 6
    assert {item.rule_version for item in results} == {"survival_discipline_v1"}
