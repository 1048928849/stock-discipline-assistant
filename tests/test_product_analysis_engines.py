import ast
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.analysis.concept_chain import analyze_concept_chain
from app.analysis.contracts import (
    BreadthMetrics,
    ConceptChainInput,
    ConceptMapping,
    IndustryAnalysisInput,
    IndustryObservation,
    IndustrySeries,
    MarketRegimeInput,
    PriceBar,
    TechnicalAnalysisInput,
    TurnoverPoint,
)
from app.analysis.industry import analyze_industry_mainlines
from app.analysis.market_regime import analyze_market_regime
from app.analysis.snapshot import ProductAnalysisSnapshot, SnapshotCapability
from app.analysis.technical import analyze_intraday_turnover
from app.data_hub.trading_calendar import SHANGHAI_TZ
from app.domain.quality import DataQualityStatus
from app.domain.quality_subject import SubjectRef
from app.strategies.base import StrategySnapshot


DAY = date(2026, 7, 24)


def _bars(*, count=30, start="10", step="0.1", last_close=None, last_volume="100"):
    start_value = Decimal(start)
    increment = Decimal(step)
    rows = []
    for index in range(count):
        close = start_value + increment * index
        if index == count - 1 and last_close is not None:
            close = Decimal(last_close)
        rows.append(
            PriceBar(
                timestamp=datetime(2026, 6, 1, tzinfo=SHANGHAI_TZ)
                + timedelta(days=index),
                open=close - Decimal("0.05"),
                high=close + Decimal("0.10"),
                low=close - Decimal("0.10"),
                close=close,
                volume=Decimal(last_volume if index == count - 1 else "100"),
            )
        )
    return tuple(rows)


def _turnover(current="2", history="1"):
    return tuple(
        TurnoverPoint(
            trade_date=DAY - timedelta(days=20 - index),
            turnover_rate=Decimal(current if index == 20 else history),
        )
        for index in range(21)
    )


def test_breakout_and_high_turnover_are_deterministic():
    bars = _bars(last_close="14.5", last_volume="220")
    result = analyze_intraday_turnover(
        TechnicalAnalysisInput(
            daily_bars=bars,
            intraday_bars=bars,
            turnover=_turnover(current="2.5"),
        )
    )
    assert result.breakout is True
    assert result.volume_breakout is True
    assert result.high_position_abnormal_turnover is True


def test_pullback_and_fake_breakout_are_distinguished():
    pullback = analyze_intraday_turnover(
        TechnicalAnalysisInput(
            daily_bars=_bars(last_close="12.65", last_volume="60"),
            intraday_bars=_bars(last_close="12.65", last_volume="60"),
            turnover=_turnover(current="0.7"),
        )
    )
    fake = analyze_intraday_turnover(
        TechnicalAnalysisInput(
            daily_bars=_bars(last_close="12.0", last_volume="220"),
            intraday_bars=_bars(last_close="12.0", last_volume="220"),
            turnover=_turnover(current="2"),
        )
    )
    assert pullback.pullback is True
    assert pullback.low_volume_pullback is True
    assert fake.false_breakout is True


def _breadth(advancing, declining, *, limit_up=20, limit_down=5, median="0"):
    return BreadthMetrics(
        trade_date=DAY,
        advancing=advancing,
        declining=declining,
        unchanged=100,
        limit_up=limit_up,
        limit_down=limit_down,
        new_highs=100,
        new_lows=20,
        median_change_pct=Decimal(median),
        above_ma20_ratio=Decimal("0.6"),
        above_ma50_ratio=Decimal("0.55"),
    )


def test_market_expansion_and_panic_use_breadth_and_amount():
    expansion = analyze_market_regime(
        MarketRegimeInput(
            current=_breadth(3500, 1000, limit_up=80, median="1"),
            previous_state="REPAIR",
            amount_history=tuple(Decimal("100") for _ in range(20)) + (Decimal("120"),),
            index_changes={"CSI300": Decimal("1")},
        )
    )
    panic = analyze_market_regime(
        MarketRegimeInput(
            current=_breadth(500, 4000, limit_down=80, median="-2"),
            previous_state="DIVERGENCE",
            amount_history=tuple(Decimal("100") for _ in range(20)) + (Decimal("130"),),
            index_changes={"CSI300": Decimal("-2")},
        )
    )
    assert expansion.state == "EXPANSION" and expansion.offensive_allowed
    assert panic.state == "PANIC" and panic.market_position_cap == Decimal("0")


def test_index_up_but_most_stocks_down_is_divergence():
    result = analyze_market_regime(
        MarketRegimeInput(
            current=_breadth(1500, 3000, limit_up=45, median="-0.5"),
            previous_state="EXPANSION",
            amount_history=tuple(Decimal("100") for _ in range(21)),
            index_changes={"CSI300": Decimal("1.2")},
        )
    )
    assert result.state == "DIVERGENCE"
    assert result.offensive_allowed is False


def test_shrinking_repair_is_not_expansion():
    result = analyze_market_regime(
        MarketRegimeInput(
            current=_breadth(2800, 1700, limit_up=30, median="0.4"),
            previous_state="PANIC",
            amount_history=tuple(Decimal("100") for _ in range(20)) + (Decimal("75"),),
            index_changes={"CSI300": Decimal("0.4")},
        )
    )
    assert result.state == "REPAIR"
    assert result.market_position_cap < Decimal("0.5")


def _industry(name, changes, *, width="0.7", leader="8", share="0.05"):
    return IndustrySeries(
        name=name,
        observations=tuple(
            IndustryObservation(
                trade_date=DAY - timedelta(days=len(changes) - index - 1),
                change_pct=Decimal(str(change)),
                amount=Decimal("100"),
                amount_share=Decimal(share),
                advance_ratio=Decimal(width),
                limit_up_count=3,
                leader_strength=Decimal(leader),
                new_high_ratio=Decimal("0.2"),
            )
            for index, change in enumerate(changes)
        ),
    )


def test_industry_pulse_and_continuous_strength_are_distinguished():
    result = analyze_industry_mainlines(
        IndustryAnalysisInput(
            industries=(
                _industry("pulse", [0] * 19 + [5]),
                _industry("persistent", [0.5] * 20),
            ),
            benchmark_changes=tuple(Decimal("0") for _ in range(20)),
        )
    )
    values = {item.name: item for item in result.industries}
    assert values["pulse"].classification == "ROTATION"
    assert values["persistent"].classification == "MAINLINE"


def test_strong_leader_with_weak_width_is_divergence():
    result = analyze_industry_mainlines(
        IndustryAnalysisInput(
            industries=(_industry("narrow", [1] * 20, width="0.25", leader="10"),),
            benchmark_changes=tuple(Decimal("0") for _ in range(20)),
        )
    )
    assert result.industries[0].classification == "DIVERGENCE"
    assert result.industries[0].crowding_risk is True


def test_fading_industry_is_reported():
    result = analyze_industry_mainlines(
        IndustryAnalysisInput(
            industries=(_industry("fading", [1] * 15 + [-2, -2, -2, -2, -2]),),
            benchmark_changes=tuple(Decimal("0") for _ in range(20)),
        )
    )
    assert result.industries[0].classification == "FADING"
    assert "fading" in result.fading_industries


def test_core_business_and_marginal_benefit_mapping():
    result = analyze_concept_chain(
        ConceptChainInput(
            symbol="300502",
            concepts=(
                ConceptMapping(
                    concept="AI",
                    relevance="CORE_BUSINESS",
                    evidence_refs=("e1",),
                ),
                ConceptMapping(
                    concept="robotics",
                    relevance="MARGINAL_BENEFIT",
                    evidence_refs=("e2",),
                ),
            ),
            chain_positions=(),
            current_mainlines=("AI",),
        )
    )
    assert result.core_concept == "AI"
    assert result.is_current_mainline is True
    assert result.is_mainline_core_company is True


def test_concept_conflict_and_insufficient_evidence_are_not_promoted():
    conflict = analyze_concept_chain(
        ConceptChainInput(
            symbol="300502",
            concepts=(
                ConceptMapping(concept="AI", relevance="CORE_BUSINESS", evidence_refs=("e1",)),
                ConceptMapping(concept="AI", relevance="REJECTED", evidence_refs=("e2",)),
            ),
            chain_positions=(),
            current_mainlines=("AI",),
        )
    )
    empty = analyze_concept_chain(
        ConceptChainInput(
            symbol="300502", concepts=(), chain_positions=(), current_mainlines=()
        )
    )
    assert conflict.relevance == "INSUFFICIENT_EVIDENCE"
    assert conflict.conflicts
    assert empty.relevance == "INSUFFICIENT_EVIDENCE"


def test_product_snapshot_is_deterministic_and_satisfies_strategy_protocol():
    capability = SnapshotCapability(
        capability="market.daily.qfq",
        subject=SubjectRef(
            subject_type="stock", subject_id="300502", semantic_key="qfq/CNY/share"
        ),
        required=True,
        quality_status=DataQualityStatus.SINGLE_SOURCE,
        executable=True,
        quality_record_id=1,
        observed_at=datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ),
        fetched_at=datetime(2026, 7, 24, 15, 1, tzinfo=SHANGHAI_TZ),
        normalized_digest="b" * 64,
        rows=({"close": "10.5"},),
        evidence_refs=("evidence:daily",),
    )
    first = ProductAnalysisSnapshot(
        analysis_started_at=datetime(2026, 7, 24, 15, 30, tzinfo=SHANGHAI_TZ),
        symbol="300502",
        capabilities=(capability,),
    )
    second = ProductAnalysisSnapshot(**first.model_dump(exclude={"snapshot_hash"}))
    assert isinstance(first, StrategySnapshot)
    assert first.snapshot_hash == second.snapshot_hash
    assert first.has_capability("market.daily.qfq")
    assert first.evidence_refs_for("market.daily.qfq") == ("evidence:daily",)


def test_analysis_modules_have_no_provider_database_or_integration_dependencies():
    root = Path(__file__).parents[1] / "app" / "analysis"
    prohibited = (
        "app.providers",
        "app.services",
        "app.domain.package_builder",
        "app.strategies",
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
