from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.trading_discipline.contracts import (
    BuyImpactObservation,
    ConfirmationBiasInput,
    DetectionStatus,
    EvidenceTier,
    GateStatus,
    MarketStage,
    PositionStressInput,
    PreTradeContext,
    ProposedAction,
    StageEvidence,
)
from app.trading_discipline.service import TradingDisciplineService
from app.watchlist.contracts import WatchlistStatus


service = TradingDisciplineService()


def test_state_change_requires_independent_groups():
    result = service.assess_market_stage(
        StageEvidence(
            as_of=date(2026, 8, 9),
            prior_base=True,
            above_ma_cluster=True,
            breakout=True,
            volume_ratio=Decimal("1.8"),
            close_position=Decimal("0.9"),
            relative_strength_change=Decimal("0.03"),
        )
    )
    assert result.stage is MarketStage.STATE_CHANGE
    assert result.result is DetectionStatus.CANDIDATE
    assert len(result.evidence_groups) >= 3


def test_second_confirmation_uses_subsequent_session_only():
    result = service.assess_market_stage(
        StageEvidence(
            as_of=date(2026, 8, 10),
            prior_base=True,
            above_ma_cluster=True,
            breakout=True,
            volume_ratio=Decimal("1.6"),
            close_position=Decimal("0.8"),
            sessions_since_state_change=1,
            held_breakout=True,
            higher_low=True,
        )
    )
    assert result.stage is MarketStage.SECOND_CONFIRMATION
    assert "SUBSEQUENT_CONFIRMATION" in result.evidence_groups


@pytest.mark.parametrize(
    ("distance", "stage"), [("12", MarketStage.ACCELERATION), ("20", MarketStage.CLIMAX)]
)
def test_late_stage(distance, stage):
    result = service.assess_market_stage(
        StageEvidence(
            as_of=date(2026, 8, 15),
            prior_base=True,
            above_ma_cluster=True,
            breakout=True,
            volume_ratio=Decimal("1.6"),
            close_position=Decimal("0.8"),
            sessions_since_state_change=5,
            held_breakout=False,
            higher_low=False,
            distance_from_structure_pct=Decimal(distance),
        )
    )
    assert result.stage is stage


def test_insufficient_stage_evidence_and_watchlist_independence():
    result = service.assess_market_stage(StageEvidence(as_of=date(2026, 8, 9)))
    assert result.result is DetectionStatus.NOT_EVALUATED
    assert MarketStage.WATCHING.value == WatchlistStatus.WATCHING.value
    assert MarketStage is not WatchlistStatus


def compliant_context(**updates):
    now = datetime(2026, 8, 9, 9, 30)
    values = dict(
        account_id=1,
        symbol="300308",
        action=ProposedAction.BUY,
        decision_at=now,
        playbook_code="CORE_STATE_CHANGE_V1",
        playbook_selected_at=now - timedelta(days=1),
        entry_evidence_at=now - timedelta(hours=1),
        invalidation_defined_at=now - timedelta(days=1),
        hard_stop=Decimal("9.70"),
        intended_quantity=100,
        proposed_quantity=100,
        current_price=Decimal("10.45"),
        planned_entry_low=Decimal("10.42"),
        planned_entry_high=Decimal("10.54"),
        planned_max_quantity=600,
        market_stage=MarketStage.SECOND_CONFIRMATION,
        chase_risk=False,
        product_v1_status="READY",
        csv_v2_executable=False,
    )
    values.update(updates)
    return PreTradeContext(**values)


def test_compliant_trade_is_100_but_not_execution_authority():
    result = service.pretrade_check(compliant_context())
    assert result.status is GateStatus.PASS
    assert result.score == 100
    assert result.executable is False
    assert set(result.category_scores.values()) == {20}


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"playbook_code": None}, "OUT_OF_PLAYBOOK"),
        ({"invalidation_defined_at": None}, "MISSING_INVALIDATION"),
        ({"hard_stop": None}, "MISSING_STOP"),
        ({"chase_risk": True}, "LATE_CONFIRMATION_RISK"),
        ({"market_stage": MarketStage.CLIMAX}, "ENTRY_AFTER_CLIMAX"),
        ({"proposed_quantity": 700}, "POSITION_ABOVE_PLAN"),
        ({"product_v1_status": "NO_TRADE"}, "PRODUCT_V1_NO_TRADE"),
    ],
)
def test_pretrade_blocks_are_not_offset_by_score(updates, reason):
    result = service.pretrade_check(compliant_context(**updates))
    assert result.status is GateStatus.BLOCK
    assert reason in result.reason_codes


def test_losing_position_add_is_blocked():
    result = service.pretrade_check(
        compliant_context(
            action=ProposedAction.ADD,
            cost_price=Decimal("11"),
            current_price=Decimal("10.45"),
            current_quantity=100,
        )
    )
    assert "LOSS_AVERAGING" in result.reason_codes
    assert result.status is GateStatus.BLOCK


@pytest.mark.parametrize(
    ("field", "category"),
    [
        ("playbook_code", "PREDEFINED_PLAYBOOK"),
        ("entry_evidence_at", "PREEXISTING_ENTRY_CONDITION"),
        ("invalidation_defined_at", "PREDEFINED_INVALIDATION"),
        ("intended_quantity", "PREDEFINED_POSITION"),
    ],
)
def test_each_missing_score_dimension_subtracts_exactly_20(field, category):
    result = service.pretrade_check(compliant_context(**{field: None}))
    assert result.category_scores[category] == 0
    assert result.score == 80


def test_rule_mutation_and_low_authority_evidence_remove_stability_score():
    result = service.pretrade_check(
        compliant_context(
            thesis_changed_after_entry=True, new_primary_evidence_tier=EvidenceTier.SENTIMENT
        )
    )
    assert result.category_scores["RULE_STABILITY"] == 0
    assert result.score == 80
    assert result.status is GateStatus.BLOCK


def test_score_is_deterministic_and_has_no_future_outcome_input():
    first = service.pretrade_check(compliant_context())
    second = service.pretrade_check(compliant_context())
    assert first == second
    assert "pnl" not in compliant_context().model_dump()


@pytest.mark.parametrize(
    ("quantity", "expected"), [(10, "NORMAL"), (160, "ELEVATED"), (250, "CRITICAL")]
)
def test_position_stress_objective_levels(quantity, expected):
    result = service.position_stress(
        PositionStressInput(
            total_assets=Decimal("10000"),
            available_cash=Decimal("5000"),
            current_quantity=0,
            proposed_quantity=quantity,
            current_price=Decimal("10"),
            hard_stop=Decimal("9"),
            planned_max_position_pct=Decimal("20"),
        )
    )
    assert result.status.value == expected
    assert result.risk_reduction_allowed is True
    assert result.add_blocked is (expected == "CRITICAL")


def test_large_print_without_intraday_quality_has_no_intent_inference():
    observation = BuyImpactObservation(
        observed_at=datetime(2026, 8, 9, 10),
        side="BUY",
        notional=Decimal("10000000"),
        quantity=Decimal("100000"),
        start_price=Decimal("10"),
        data_quality="DAILY_ONLY",
    )
    assert observation.efficiency().value == "INSUFFICIENT_DATA"


@pytest.mark.parametrize("tier", list(EvidenceTier))
def test_evidence_never_has_formal_execution_authority(tier):
    authority = service.evidence_authority(tier, verified=tier is EvidenceTier.FACT)
    assert authority.formal_execution_authority is False
    assert authority.may_modify_formal_plan is False
    assert authority.may_update_fact_set is (tier is EvidenceTier.FACT)


def test_post_position_low_authority_reason_cannot_authorize_loss_add():
    result = service.confirmation_bias_guard(
        ConfirmationBiasInput(
            holding_exists=True,
            current_price=Decimal("9"),
            cost_price=Decimal("10"),
            proposed_action=ProposedAction.ADD,
            evidence_tier=EvidenceTier.ANALYSIS,
            evidence_added_after_entry=True,
        )
    )
    assert result.status is GateStatus.BLOCK
    assert "LOSS_POSITION_NEW_BULLISH_REASON" in result.reason_codes
    assert result.risk_reduction_allowed is True


def test_new_fact_requires_review_and_cannot_loosen_stop():
    result = service.confirmation_bias_guard(
        ConfirmationBiasInput(
            holding_exists=True,
            current_price=Decimal("11"),
            cost_price=Decimal("10"),
            proposed_action=ProposedAction.ADD,
            evidence_tier=EvidenceTier.FACT,
            evidence_added_after_entry=True,
            frozen_hard_stop=Decimal("9.7"),
            proposed_hard_stop=Decimal("9.0"),
        )
    )
    assert result.status is GateStatus.BLOCK
    assert "STOP_OVERRIDE_ATTEMPT" in result.reason_codes
    assert result.reanalysis_required is True


def test_profit_and_loss_never_rewrite_discipline_classification():
    assert (
        service.classify_execution(discipline_score=40, pnl_pct=Decimal("20"))
        == "PROFITABLE_UNDISCIPLINED"
    )
    assert (
        service.classify_execution(discipline_score=100, pnl_pct=Decimal("-5"))
        == "LOSING_COMPLIANT"
    )


def test_daily_data_cannot_activate_divergence_contract():
    assert service.assess_divergence(data_quality="DAILY_ONLY").value == "INSUFFICIENT_DATA"
