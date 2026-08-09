from datetime import datetime
from decimal import Decimal

import pytest

from app.trading_discipline.contracts import (
    DecisionContext,
    DetectionStatus,
    EvidenceTier,
    FinalAction,
    MarketStage,
    PriceBehaviorAssessment,
    StressStatus,
    TradeDecisionInput,
)
from app.trading_discipline.service import TradingDisciplineService


service = TradingDisciplineService()


def decision(**updates):
    payload = dict(
        symbol="fixture",
        decision_at=datetime(2026, 8, 9, 15),
        new_variable="daily state change",
        information_tier=EvidenceTier.FACT,
        information_is_primary_reason=False,
        stock_hierarchy_role="CORE",
        market_stage=MarketStage.STATE_CHANGE,
        why_researching_now="multi-group state change",
        state_change_status=DetectionStatus.CANDIDATE,
        second_confirmation_present=False,
        expected_behavior=["hold breakout", "higher low"],
        actual_behavior=["first close held"],
        expected_behavior_score=Decimal("1"),
        actual_behavior_score=Decimal("1"),
        invalidation=["close below breakout structure"],
        position_rationale="no position before confirmation",
        position_stress=StressStatus.NORMAL,
        evidence_seen=["MA cluster", "volume expansion", "close near high"],
        evidence_required=["subsequent-session confirmation"],
        next_reassessment_trigger="NEXT_DAILY_CLOSE",
    )
    payload.update(updates)
    return TradeDecisionInput(**payload)


def test_seven_gates_have_exact_order():
    result = service.seven_gate_decision(
        decision(second_confirmation_present=True, market_stage=MarketStage.SECOND_CONFIRMATION)
    )
    assert [gate.rule_code for gate in result.gates] == [
        "INFORMATION",
        "CHANGE",
        "HIERARCHY",
        "STAGE",
        "PRICE_BEHAVIOR",
        "RISK_INVALIDATION",
        "POSITION",
    ]


def test_state_change_fixture_promotes_observation_and_waiting_plan():
    result = service.seven_gate_decision(decision())
    assert result.final_action is FinalAction.WAIT_FOR_CONFIRMATION
    assert result.observation_plan.evidence_required == ["subsequent-session confirmation"]
    assert result.observation_plan.next_reassessment_trigger == "NEXT_DAILY_CLOSE"


def test_acceleration_stall_fixture_does_not_treat_large_order_as_bullish():
    result = service.seven_gate_decision(
        decision(
            market_stage=MarketStage.ACCELERATION,
            research_started_after_spike=True,
            new_variable="large print",
            information_tier=EvidenceTier.SENTIMENT,
            information_is_primary_reason=True,
            expected_behavior_score=Decimal("2"),
            actual_behavior_score=Decimal("1"),
            actual_behavior=["high-volume stall", "failed repair"],
        )
    )
    assert result.final_action is FinalAction.OBSERVE
    assert result.price_behavior is PriceBehaviorAssessment.WEAKER_THAN_EXPECTED
    assert {"RESEARCH_STARTED_AFTER_SPIKE", "LOW_AUTHORITY_PRIMARY_REASON"} <= set(
        result.veto_reason_codes
    )


def test_oversized_post_position_search_fixture_cannot_raise_risk():
    result = service.seven_gate_decision(
        decision(
            market_stage=MarketStage.SECOND_CONFIRMATION,
            second_confirmation_present=True,
            position_stress=StressStatus.CRITICAL,
            post_position_information_search=True,
            new_variable="new bullish broker opinion",
            information_tier=EvidenceTier.ANALYSIS,
            information_is_primary_reason=True,
        )
    )
    assert result.final_action is FinalAction.OBSERVE
    assert result.new_risk_blocked is True
    assert "POSITION_ABOVE_PLAN" in result.veto_reason_codes


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"research_started_after_spike": True}, "RESEARCH_STARTED_AFTER_SPIKE"),
        (
            {"information_tier": EvidenceTier.SENTIMENT, "information_is_primary_reason": True},
            "LOW_AUTHORITY_PRIMARY_REASON",
        ),
        ({"market_stage": MarketStage.UNKNOWN}, "MARKET_STAGE_UNRESOLVED"),
        ({"invalidation": []}, "MISSING_INVALIDATION"),
        ({"upside_first_process": True}, "UPSIDE_FIRST_DECISION_PROCESS"),
        (
            {
                "decision_context": DecisionContext(
                    recent_large_win_pct=Decimal("20"),
                    previous_risk_pct=Decimal("1"),
                    proposed_risk_pct=Decimal("2"),
                )
            },
            "POST_WIN_RISK_ESCALATION",
        ),
        (
            {
                "decision_context": DecisionContext(
                    recent_large_loss_pct=Decimal("-8"), sessions_since_loss_exit=1
                )
            },
            "LOSS_RECOVERY_TRADE_RISK",
        ),
        (
            {"decision_context": DecisionContext(conflicting_information_count=3)},
            "DECISION_CONTEXT_CONFLICTED",
        ),
    ],
)
def test_each_hard_veto_forces_observe(updates, reason):
    result = service.seven_gate_decision(decision(**updates))
    assert reason in result.veto_reason_codes
    assert result.final_action is FinalAction.OBSERVE


def test_trade_horizon_drift_cannot_erase_original_failure():
    result = service.seven_gate_decision(
        decision(
            original_trade_horizon="SHORT_TERM",
            proposed_trade_horizon="LONG_TERM",
            original_thesis_failed=True,
            invalidation_triggered=True,
        )
    )
    assert "TRADE_HORIZON_DRIFT" in result.veto_reason_codes
    assert result.final_action is FinalAction.ABANDON


@pytest.mark.parametrize(
    "expected,actual,status",
    [
        ("1", "2", PriceBehaviorAssessment.STRONGER_THAN_EXPECTED),
        ("1", "1", PriceBehaviorAssessment.AS_EXPECTED),
        ("2", "1", PriceBehaviorAssessment.WEAKER_THAN_EXPECTED),
        (None, None, PriceBehaviorAssessment.NOT_EVALUATED),
    ],
)
def test_expected_vs_actual_behavior(expected, actual, status):
    result = service.seven_gate_decision(
        decision(
            expected_behavior_score=Decimal(expected) if expected else None,
            actual_behavior_score=Decimal(actual) if actual else None,
        )
    )
    assert result.price_behavior is status


def test_decision_card_has_exact_conceptual_fields():
    card = service.seven_gate_decision(decision()).decision_card.model_dump()
    assert set(card) == {
        "new_variable",
        "information_tier",
        "stock_hierarchy_role",
        "market_stage",
        "why_researching_now",
        "expected_behavior_if_thesis_correct",
        "invalidation",
        "position_rationale",
        "information_decision_interference",
        "final_action",
    }


def test_only_one_active_playbook_contract():
    from app.trading_discipline.service import PLAYBOOK_CODE

    assert PLAYBOOK_CODE == "CORE_STATE_CHANGE_V1"
