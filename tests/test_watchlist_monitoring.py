from datetime import datetime
from decimal import Decimal

import pytest

from app.data_hub.trading_calendar import SHANGHAI_TZ
from app.watchlist.contracts import (
    InvalidationRuleType,
    MonitoringHealth,
    WatchlistCreateRequest,
    WatchlistSourceType,
    WatchlistStatus,
)
from app.watchlist.state_machine import (
    INDUSTRY_RISK_ORDER,
    MARKET_RISK_ORDER,
    PriceStateInput,
    determine_price_transition,
    matching_invalidation_rule,
    risk_increased,
    validate_transition,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=SHANGHAI_TZ)


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("10.82", WatchlistStatus.WATCHING),
        ("10.70", WatchlistStatus.NEAR_ENTRY),
        ("10.50", WatchlistStatus.ENTRY_TRIGGERED),
    ],
)
def test_price_state_uses_decimal_near_entry_boundaries(price, expected):
    outcome = determine_price_transition(
        PriceStateInput(
            current_status=WatchlistStatus.WATCHING,
            current_price=Decimal(price),
            entry_low=Decimal("10.40"),
            entry_high=Decimal("10.60"),
            hard_stop=Decimal("9.70"),
            near_entry_distance_pct=Decimal("2"),
            observed_at=NOW,
        )
    )
    assert outcome.to_status == expected


def test_price_below_entry_is_not_interpreted_as_cheaper_entry():
    outcome = determine_price_transition(
        PriceStateInput(
            current_status=WatchlistStatus.WATCHING,
            current_price=Decimal("10.00"),
            entry_low=Decimal("10.40"),
            entry_high=Decimal("10.60"),
            hard_stop=Decimal("9.70"),
            near_entry_distance_pct=Decimal("2"),
            observed_at=NOW,
        )
    )
    assert outcome.to_status == WatchlistStatus.WATCHING
    assert "BELOW_ENTRY_REQUIRES_REASSESSMENT" in outcome.reason_codes
    assert outcome.trigger_reanalysis is True


def test_explicit_market_and_industry_risk_orders_are_deterministic():
    assert risk_increased("EXPANSION", "CONTRACTION", MARKET_RISK_ORDER)
    assert not risk_increased("PANIC", "REPAIR", MARKET_RISK_ORDER)
    assert risk_increased("MAINLINE", "FADING", INDUSTRY_RISK_ORDER)
    assert not risk_increased("NONE", "ROTATION", INDUSTRY_RISK_ORDER)


def test_structured_invalidation_rules_do_not_parse_display_text():
    rule = {
        "rule_type": InvalidationRuleType.MARKET_REGIME_AT_OR_WORSE_THAN.value,
        "threshold": "CONTRACTION",
        "source": "USER",
        "evidence_reference": "evidence:market",
        "created_at": NOW.isoformat(),
    }
    matched = matching_invalidation_rule(
        [rule],
        current_price=Decimal("10.50"),
        market_state="PANIC",
        industry_state="MAINLINE",
    )
    assert matched is not None
    assert matched.rule_type == InvalidationRuleType.MARKET_REGIME_AT_OR_WORSE_THAN
    with pytest.raises(ValueError):
        WatchlistCreateRequest.model_validate(
            {
                "source_type": "MANUAL",
                "symbol": "300502",
                "thesis": "test",
                "invalidation_conditions": ["跌破趋势就失效"],
                "invalidation_rule_specs": [
                    {
                        "rule_type": "NATURAL_LANGUAGE_EXPRESSION",
                        "threshold": "PANIC",
                        "source": "LLM",
                        "evidence_reference": "text:1",
                        "created_at": NOW.isoformat(),
                    }
                ],
            }
        )


def test_hard_stop_permanently_invalidates_watchlist_item():
    outcome = determine_price_transition(
        PriceStateInput(
            current_status=WatchlistStatus.NEAR_ENTRY,
            current_price=Decimal("9.69"),
            entry_low=Decimal("10.40"),
            entry_high=Decimal("10.60"),
            hard_stop=Decimal("9.70"),
            near_entry_distance_pct=Decimal("2"),
            observed_at=NOW,
        )
    )
    assert outcome.to_status == WatchlistStatus.INVALIDATED
    assert outcome.severity == "CRITICAL"


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (WatchlistStatus.INVALIDATED, WatchlistStatus.WATCHING),
        (WatchlistStatus.ARCHIVED, WatchlistStatus.WATCHING),
        (WatchlistStatus.WATCHING, WatchlistStatus.ARCHIVED),
    ],
)
def test_illegal_automatic_transitions_are_rejected(from_status, to_status):
    with pytest.raises(ValueError, match="illegal_watchlist_transition"):
        validate_transition(from_status, to_status, automatic=True)


def test_quality_failure_is_health_only_and_does_not_invalidate():
    outcome = determine_price_transition(
        PriceStateInput(
            current_status=WatchlistStatus.NEAR_ENTRY,
            current_price=None,
            entry_low=Decimal("10.40"),
            entry_high=Decimal("10.60"),
            hard_stop=Decimal("9.70"),
            near_entry_distance_pct=Decimal("2"),
            observed_at=NOW,
            monitoring_health=MonitoringHealth.CONFLICTED,
        )
    )
    assert outcome.to_status == WatchlistStatus.NEAR_ENTRY
    assert outcome.trigger_reanalysis is False


def test_product_watchlist_request_accepts_only_server_analysis_reference():
    request = WatchlistCreateRequest(
        source_type=WatchlistSourceType.PRODUCT_ANALYSIS,
        analysis_run_id=42,
        thesis="等待价格和市场共同确认",
    )
    assert request.analysis_run_id == 42
    with pytest.raises(ValueError):
        WatchlistCreateRequest.model_validate(
            {
                "source_type": "PRODUCT_ANALYSIS",
                "analysis_run_id": 42,
                "thesis": "untrusted",
                "entry_low": "1",
                "hard_stop": "0",
                "strategy_version": "forged",
            }
        )
