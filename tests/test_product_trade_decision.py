import ast
from decimal import Decimal
from pathlib import Path

from app.analysis.contracts import (
    ConceptChainContext,
    IndustryAssessment,
    MarketRegime,
    TechnicalContext,
)
from app.decision.contracts import BaseRulePlan, TradeDecisionContext
from app.decision.engine import build_trade_decision
from app.domain.quality import DataQualityStatus
from app.strategies.core_discipline import CoreDisciplineStrategy
from app.strategies.registry import StrategyRegistry


def _technical(**overrides):
    data = {
        "intraday_trend": "UP",
        "daily_trend": "UP",
        "ma5": "10.8",
        "ma10": "10.6",
        "ma20": "10.3",
        "effective_high": "10.5",
        "effective_low": "9.8",
        "breakout": True,
        "pullback": False,
        "false_breakout": False,
        "volume_breakout": True,
        "low_volume_pullback": False,
        "spike_fade": False,
        "daily_intraday_aligned": True,
        "current_turnover": "2",
        "average_turnover_5d": "1.2",
        "average_turnover_20d": "1",
        "relative_turnover_20d": "2",
        "high_position_abnormal_turnover": False,
        "low_position_moderate_volume": False,
        "volume_without_price_gain": False,
        "low_volume_rise": False,
        "data_completeness": "1",
    }
    data.update(overrides)
    return TechnicalContext(**data)


def _market(state="EXPANSION"):
    return MarketRegime(
        state=state,
        previous_state="REPAIR",
        transition=f"REPAIR->{state}",
        data_completeness=Decimal("1"),
        supporting_indicators=("fixture",),
        conflicting_indicators=(),
        confidence="HIGH",
        offensive_allowed=state == "EXPANSION",
        market_position_cap={
            "EXPANSION": Decimal("1"),
            "REPAIR": Decimal("0.3"),
            "DIVERGENCE": Decimal("0.5"),
            "CONTRACTION": Decimal("0.2"),
            "PANIC": Decimal("0"),
        }[state],
        advance_ratio=Decimal("0.7"),
        amount_ratio_5d=Decimal("1.1"),
        amount_ratio_20d=Decimal("1.1"),
    )


def _industry(classification="MAINLINE"):
    return IndustryAssessment(
        name="electronics",
        classification=classification,
        phase="PERSISTENT" if classification == "MAINLINE" else "FADING",
        daily_change=Decimal("1"),
        relative_strength_5d=Decimal("3"),
        relative_strength_10d=Decimal("5"),
        relative_strength_20d=Decimal("8"),
        amount=Decimal("100"),
        amount_share=Decimal("0.05"),
        advance_ratio=Decimal("0.7"),
        limit_up_count=3,
        leader_strength=Decimal("8"),
        persistence_days=8,
        max_drawdown=Decimal("0.03"),
        new_high_ratio=Decimal("0.2"),
        crowding_risk=False,
        supporting_indicators=("fixture",),
        conflicting_indicators=(),
    )


def _concept():
    return ConceptChainContext(
        core_concept="AI",
        relevance="CORE_BUSINESS",
        chain=None,
        chain_node=None,
        primary_products=(),
        revenue_relevance="unknown",
        core_level=None,
        substitutability=None,
        competitive_position=None,
        evidence_refs=("e1",),
        conflicts=(),
        is_current_mainline=True,
        is_mainline_core_company=True,
    )


def _base():
    return BaseRulePlan(
        rule_status="READY",
        buy_zone_low=Decimal("10.4209"),
        buy_zone_high=Decimal("10.5391"),
        hard_stop=Decimal("9.7023"),
        final_quantity=600,
        trial_quantity=100,
        per_share_risk=Decimal("0.7777"),
        maximum_loss=Decimal("77.77"),
        base_position_pct=Decimal("2.1"),
        max_position_pct=Decimal("30"),
        trigger_condition="price confirms the deterministic buy zone",
        logical_invalidation="daily structure invalidated",
    )


def _context(**overrides):
    data = {
        "base_plan": _base(),
        "technical": _technical(),
        "market": _market(),
        "industry": _industry(),
        "concept_chain": _concept(),
        "data_quality": DataQualityStatus.SINGLE_SOURCE,
        "announcement_risk": "LOW",
    }
    data.update(overrides)
    return TradeDecisionContext(**data)


def test_golden_rule_numbers_remain_unchanged():
    result = build_trade_decision(_context())
    assert result.rule_status == "READY"
    assert result.executable_status == "READY"
    assert result.buy_zone == (Decimal("10.4209"), Decimal("10.5391"))
    assert result.risk_plan.hard_stop == Decimal("9.7023")
    assert result.position_constraints.target_quantity == 600
    assert result.position_constraints.trial_quantity == 100
    assert result.risk_plan.per_share_risk == Decimal("0.7777")
    assert result.risk_plan.maximum_plan_loss == Decimal("77.77")


def test_market_contraction_reduces_position():
    result = build_trade_decision(_context(market=_market("CONTRACTION")))
    assert result.position_constraints.target_quantity < 600
    assert result.position_constraints.effective_constraint == "market_regime"


def test_fading_industry_blocks_breakout_chasing():
    result = build_trade_decision(_context(industry=_industry("FADING")))
    assert result.executable_status == "WAIT"
    assert "industry is fading" in result.blocked_reasons


def test_conflicted_data_blocks_without_changing_rule_status():
    result = build_trade_decision(_context(data_quality=DataQualityStatus.CONFLICTED))
    assert result.rule_status == "READY"
    assert result.executable_status == "WAIT"
    assert result.position_constraints.target_quantity == 0


def test_high_announcement_risk_blocks_trade():
    result = build_trade_decision(_context(announcement_risk="HIGH"))
    assert result.executable_status == "WAIT"
    assert "high announcement risk" in result.blocked_reasons


def test_no_valid_entry_is_no_trade():
    technical = _technical(
        intraday_trend="RANGE",
        daily_trend="RANGE",
        breakout=False,
        volume_breakout=False,
        daily_intraday_aligned=False,
    )
    result = build_trade_decision(_context(technical=technical))
    assert result.buy_point_assessment.buy_point_type == "NO_VALID_ENTRY"
    assert result.buy_allowed is False


def test_risk_and_exit_plan_contains_all_formal_conditions():
    result = build_trade_decision(_context())
    assert result.risk_plan.add_condition
    assert result.risk_plan.no_add_condition
    assert result.exit_plan.first_take_profit
    assert result.exit_plan.second_take_profit
    assert result.exit_plan.trailing_stop
    assert result.exit_plan.reduce_condition
    assert result.exit_plan.exit_condition
    assert result.exit_plan.no_trade_condition
    assert result.next_session_observations


def test_core_strategy_registers_and_delegates_to_global_decision_engine():
    strategy = CoreDisciplineStrategy()
    registry = StrategyRegistry()
    registry.register(strategy)
    result = strategy.evaluate_trade_decision(_context(announcement_risk="HIGH"))
    assert result.executable_status == "WAIT"
    assert registry.get(strategy.strategy_id, strategy.strategy_version) is strategy


def test_decision_engine_has_no_provider_database_or_integration_dependency():
    root = Path(__file__).parents[1] / "app" / "decision"
    prohibited = (
        "app.providers",
        "app.services",
        "app.domain.package_builder",
        "sqlalchemy",
    )
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        assert not any(name.startswith(prohibited) for name in imports), (path, imports)
