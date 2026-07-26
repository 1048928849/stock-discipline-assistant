from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.discovery.algorithm import discover_candidates
from app.discovery.contracts import (
    CandidateStockInput,
    DiscoveryConfig,
    DiscoverySnapshot,
    IndustryDiscoveryInput,
    MarketDiscoveryInput,
    PriceHistoryPoint,
)


NOW = datetime(2026, 7, 24, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _prices(*, accelerated: bool = False) -> tuple[PriceHistoryPoint, ...]:
    rows = []
    for index in range(55):
        close = Decimal("10") + Decimal(index) * (
            Decimal("0.12") if accelerated else Decimal("0.01")
        )
        rows.append(
            PriceHistoryPoint(
                trade_date=date(2026, 5, 1).fromordinal(date(2026, 5, 1).toordinal() + index),
                close=close,
                amount=Decimal("200000000"),
                turnover_rate=Decimal("3.2"),
            )
        )
    return tuple(rows)


def _industry(
    name: str,
    classification: str,
    *,
    symbol: str,
    limit_up: bool = False,
    accelerated: bool = False,
    quality: str = "VERIFIED",
) -> IndustryDiscoveryInput:
    return IndustryDiscoveryInput(
        industry_key=f"BK-{name}",
        industry_name=name,
        classification=classification,
        relative_strength_5d=Decimal("4"),
        relative_strength_10d=Decimal("8"),
        relative_strength_20d=Decimal("12"),
        amount_share=Decimal("0.08"),
        advance_ratio=Decimal("0.66"),
        limit_up_count=2,
        leader_strength=Decimal("8"),
        new_high_ratio=Decimal("0.2"),
        net_inflow_1d=Decimal("300000000"),
        net_inflow_5d=Decimal("900000000"),
        net_inflow_10d=Decimal("1500000000"),
        broken_limit_rate=Decimal("0.1"),
        quality_status=quality,
        evidence_references=(f"industry:{name}",),
        constituents=(
            CandidateStockInput(
                symbol=symbol,
                name=f"测试{symbol}",
                industry_key=f"BK-{name}",
                industry_name=name,
                prices=_prices(accelerated=accelerated),
                is_st=False,
                suspended=False,
                limit_up=limit_up,
                quality_status=quality,
                evidence_references=(f"daily:{symbol}",),
            ),
        ),
    )


def _snapshot(*industries, market_state="EXPANSION", market_quality="VERIFIED"):
    return DiscoverySnapshot(
        market=MarketDiscoveryInput(
            market="CN-A",
            trade_date=date(2026, 7, 24),
            state=market_state,
            quality_status=market_quality,
            evidence_references=("market:2026-07-24",),
        ),
        industries=tuple(industries),
        observed_at=NOW,
    )


def test_discovery_is_deterministic_for_identical_snapshot_and_config():
    snapshot = _snapshot(
        _industry("通信", "MAINLINE", symbol="300001"),
        _industry("电子", "SECONDARY", symbol="300002"),
    )
    first = discover_candidates(snapshot, DiscoveryConfig())
    second = discover_candidates(snapshot, DiscoveryConfig())
    assert first == second
    assert first.input_snapshot_hash == second.input_snapshot_hash
    assert [item.symbol for item in first.candidates] == [
        item.symbol for item in second.candidates
    ]


def test_market_quality_block_creates_no_candidates():
    result = discover_candidates(
        _snapshot(
            _industry("通信", "MAINLINE", symbol="300001"),
            market_quality="CONFLICTED",
        ),
        DiscoveryConfig(),
    )
    assert result.status == "BLOCKED"
    assert result.candidates == ()
    assert "MARKET_QUALITY_CONFLICTED" in result.blocked_reasons


@pytest.mark.parametrize("state", ["CONTRACTION", "PANIC"])
def test_high_risk_market_only_produces_research_candidates(state):
    result = discover_candidates(
        _snapshot(_industry("通信", "MAINLINE", symbol="300001"), market_state=state),
        DiscoveryConfig(),
    )
    assert result.candidates
    assert {item.candidate_type for item in result.candidates} == {"RESEARCH_ONLY"}


@pytest.mark.parametrize("state", ["CONTRACTION", "PANIC"])
def test_high_risk_market_keeps_limit_up_reference_research_only(state):
    result = discover_candidates(
        _snapshot(
            _industry("通信", "MAINLINE", symbol="300001", limit_up=True),
            market_state=state,
        ),
        DiscoveryConfig(),
    )
    assert result.candidates[0].candidate_type == "RESEARCH_ONLY"
    assert "LIMIT_UP_LEADER_REFERENCE" in result.candidates[0].reason_codes


def test_limit_up_stock_is_leader_reference_not_watch_candidate():
    result = discover_candidates(
        _snapshot(_industry("通信", "MAINLINE", symbol="300001", limit_up=True)),
        DiscoveryConfig(),
    )
    assert result.candidates[0].candidate_type == "LEADER_REFERENCE"
    assert "LIMIT_UP_LEADER_REFERENCE" in result.candidates[0].reason_codes


def test_accelerated_stock_does_not_rank_before_early_candidate():
    accelerated = _industry(
        "通信", "MAINLINE", symbol="300001", accelerated=True
    ).model_copy(
        update={
            "constituents": (
                _industry(
                    "通信", "MAINLINE", symbol="300001", accelerated=True
                ).constituents[0],
                _industry("通信", "MAINLINE", symbol="300002").constituents[0],
            )
        }
    )
    result = discover_candidates(_snapshot(accelerated), DiscoveryConfig())
    by_symbol = {item.symbol: item for item in result.candidates}
    assert by_symbol["300002"].rank < by_symbol["300001"].rank
    assert "OVEREXTENDED" in by_symbol["300001"].risk_flags


def test_industry_quality_block_does_not_fill_missing_metrics_with_zero():
    result = discover_candidates(
        _snapshot(
            _industry(
                "通信", "MAINLINE", symbol="300001", quality="STALE"
            )
        ),
        DiscoveryConfig(),
    )
    assert result.candidates == ()
    assert result.status == "BLOCKED"
    assert "NO_EXECUTABLE_INDUSTRY_DATA" in result.blocked_reasons
    assert result.industries[0].quality_status == "STALE"
    assert result.industries[0].score is None


def test_partial_industry_quality_gap_blocks_incomplete_universe():
    result = discover_candidates(
        _snapshot(
            _industry("通信", "MAINLINE", symbol="300001"),
            _industry(
                "电子", "SECONDARY", symbol="300002", quality="CONFLICTED"
            ),
        ),
        DiscoveryConfig(),
    )
    assert result.status == "BLOCKED"
    assert result.quality_status == "CONFLICTED"
    assert result.candidates == ()
    assert result.blocked_reasons == ("INCOMPLETE_INDUSTRY_UNIVERSE",)


def test_missing_daily_data_for_all_constituents_blocks_run():
    industry = _industry("通信", "MAINLINE", symbol="300001").model_copy(
        update={"constituents": ()}
    )
    result = discover_candidates(_snapshot(industry), DiscoveryConfig())
    assert result.status == "BLOCKED"
    assert result.candidates == ()
    assert result.blocked_reasons == ("NO_EXECUTABLE_STOCK_DATA",)


def test_untrusted_stock_data_blocks_instead_of_completing_empty():
    industry = _industry("通信", "MAINLINE", symbol="300001")
    stale_stock = industry.constituents[0].model_copy(
        update={"quality_status": "STALE"}
    )
    result = discover_candidates(
        _snapshot(industry.model_copy(update={"constituents": (stale_stock,)})),
        DiscoveryConfig(),
    )
    assert result.status == "BLOCKED"
    assert result.quality_status == "STALE"
    assert result.blocked_reasons == ("NO_EXECUTABLE_STOCK_DATA",)


def test_industry_score_uses_capital_flow_and_broken_limit_rate():
    strong = _industry("强行业", "MAINLINE", symbol="300001")
    weak = _industry("弱行业", "MAINLINE", symbol="300002").model_copy(
        update={
            "net_inflow_1d": Decimal("-500000000"),
            "net_inflow_5d": Decimal("-900000000"),
            "net_inflow_10d": Decimal("-1500000000"),
            "broken_limit_rate": Decimal("0.8"),
        }
    )
    result = discover_candidates(_snapshot(strong, weak), DiscoveryConfig())
    assert [item.industry_name for item in result.industries] == ["强行业", "弱行业"]
    assert result.industries[0].score > result.industries[1].score


def test_candidate_contract_contains_no_formal_trade_plan_fields():
    candidate = discover_candidates(
        _snapshot(_industry("通信", "MAINLINE", symbol="300001")),
        DiscoveryConfig(),
    ).candidates[0]
    values = candidate.model_dump()
    assert not {
        "entry_low",
        "entry_high",
        "hard_stop",
        "position",
        "maximum_loss",
        "freeze_allowed",
    } & values.keys()


def test_candidate_binds_market_industry_and_stock_evidence():
    candidate = discover_candidates(
        _snapshot(_industry("通信", "MAINLINE", symbol="300001")),
        DiscoveryConfig(),
    ).candidates[0]
    assert candidate.evidence_references == (
        "daily:300001",
        "industry:通信",
        "market:2026-07-24",
    )


def test_configuration_changes_hash_and_is_fully_explicit():
    first = DiscoveryConfig()
    second = first.model_copy(update={"max_candidates": first.max_candidates + 1})
    assert first.config_hash() != second.config_hash()
    assert "weight_capital_flow" in first.model_dump()
    assert "max_distance_ma20_pct" in first.model_dump()


def test_default_discovery_limit_is_thirty_candidates():
    assert DiscoveryConfig().max_candidates == 30
