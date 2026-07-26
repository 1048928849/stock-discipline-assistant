from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.discovery.contracts import (
    CandidateStockInput,
    DiscoverySnapshot,
    IndustryDiscoveryInput,
    MarketDiscoveryInput,
    PriceHistoryPoint,
)
from app.discovery.service import CandidateDiscoveryService, promote_candidate
from app.data_hub.trading_calendar import to_market_storage_naive
from app.models import (
    CandidateDiscoveryRun,
    CandidateIndustryAssessment,
    DiscoveryCandidate,
    MarketRegimeSnapshot,
    ReanalysisRequest,
    TradePlan,
    WatchlistItem,
    WatchlistRevision,
)


NOW = datetime(2026, 7, 24, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _snapshot(*, market_state="EXPANSION", market_quality="VERIFIED"):
    prices = tuple(
        PriceHistoryPoint(
            trade_date=date(2026, 5, 1).fromordinal(date(2026, 5, 1).toordinal() + index),
            close=Decimal("10") + Decimal(index) * Decimal("0.01"),
            amount=Decimal("200000000"),
            turnover_rate=Decimal("3"),
        )
        for index in range(55)
    )
    stock = CandidateStockInput(
        symbol="300001",
        name="测试股份",
        industry_key="BK0001",
        industry_name="通信设备",
        prices=prices,
        is_st=False,
        suspended=False,
        limit_up=False,
        quality_status="VERIFIED",
        evidence_references=("quality:stock",),
    )
    industry = IndustryDiscoveryInput(
        industry_key="BK0001",
        industry_name="通信设备",
        classification="MAINLINE",
        relative_strength_5d=Decimal("4"),
        relative_strength_10d=Decimal("8"),
        relative_strength_20d=Decimal("12"),
        amount_share=Decimal("0.08"),
        advance_ratio=Decimal("0.66"),
        limit_up_count=2,
        leader_strength=Decimal("8"),
        new_high_ratio=Decimal("0.2"),
        net_inflow_1d=Decimal("100000000"),
        net_inflow_5d=Decimal("300000000"),
        net_inflow_10d=Decimal("500000000"),
        broken_limit_rate=Decimal("0.1"),
        quality_status="VERIFIED",
        evidence_references=("quality:industry",),
        constituents=(stock,),
    )
    return DiscoverySnapshot(
        market=MarketDiscoveryInput(
            trade_date=date(2026, 7, 24),
            state=market_state,
            quality_status=market_quality,
            evidence_references=("market-regime:1",),
        ),
        industries=(industry,),
        observed_at=NOW,
    )


def test_discovery_run_persists_immutable_results_and_is_idempotent(session):
    service = CandidateDiscoveryService(session)
    first = service.run(now=NOW, snapshot=_snapshot())
    second = service.run(now=NOW, snapshot=_snapshot())
    assert first.id == second.id
    assert first.status == "COMPLETED"
    assert first.candidates_generated == 1
    assert session.scalar(select(func.count(CandidateDiscoveryRun.id))) == 1
    assert session.scalar(select(func.count(CandidateIndustryAssessment.id))) == 1
    assert session.scalar(select(func.count(DiscoveryCandidate.id))) == 1


def test_market_blocked_run_persists_no_candidates(session):
    run = CandidateDiscoveryService(session).run(
        now=NOW,
        snapshot=_snapshot(market_quality="STALE"),
    )
    assert run.status == "BLOCKED"
    assert run.quality_status == "STALE"
    assert run.candidates_generated == 0


def test_snapshot_revalidates_stored_market_state_quality_bindings(session):
    session.add(
        MarketRegimeSnapshot(
            market_id="CN-A",
            trade_date=date(2026, 7, 24),
            state="EXPANSION",
            previous_state="REPAIR",
            transition="REPAIR->EXPANSION",
            product_snapshot_hash="a" * 64,
            observed_at=to_market_storage_naive(NOW),
            quality_status="VERIFIED",
            quality_bindings=[],
        )
    )
    session.commit()
    snapshot = CandidateDiscoveryService(session).build_snapshot(now=NOW)
    assert snapshot.market.quality_status == "MISSING"
    assert snapshot.industries == ()


def test_candidate_expiry_blocks_promotion_and_preserves_history(session):
    run = CandidateDiscoveryService(session).run(now=NOW, snapshot=_snapshot())
    candidate = session.scalar(
        select(DiscoveryCandidate).where(DiscoveryCandidate.discovery_run_id == run.id)
    )
    candidate.expires_at = datetime(2026, 7, 24, 10, 0)
    session.commit()
    try:
        promote_candidate(
            session,
            candidate.id,
            thesis="等待进一步研究",
            analysis_capital=Decimal("300000"),
            waiting_conditions=[],
            now=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
    except Exception as exc:
        assert getattr(exc, "code", None) == "DISCOVERY_CANDIDATE_EXPIRED"
    else:
        raise AssertionError("expired candidate promotion must fail")
    session.refresh(candidate)
    assert candidate.status == "EXPIRED"
    assert session.get(CandidateDiscoveryRun, run.id) is not None


def test_promotion_creates_one_watchlist_and_reanalysis_without_trade_plan(
    session, monkeypatch
):
    run = CandidateDiscoveryService(session).run(now=NOW, snapshot=_snapshot())
    candidate = session.scalar(
        select(DiscoveryCandidate).where(DiscoveryCandidate.discovery_run_id == run.id)
    )
    import app.discovery.service as service_module

    reanalysis_calls = []
    monkeypatch.setattr(
        service_module,
        "execute_reanalysis",
        lambda db, request_id: (
            reanalysis_calls.append(request_id)
            or SimpleNamespace(id=99, request_id=request_id)
        ),
    )
    before_plans = session.scalar(select(func.count(TradePlan.id)))
    first = promote_candidate(
        session,
        candidate.id,
        thesis="主线内尚未加速，等待正式分析",
        analysis_capital=Decimal("300000"),
        waiting_conditions=["等待确定性计划"],
        now=NOW,
    )
    second = promote_candidate(
        session,
        candidate.id,
        thesis="不得覆盖既有观察项",
        analysis_capital=Decimal("300000"),
        waiting_conditions=[],
        now=NOW + timedelta(hours=1),
    )
    item = session.get(WatchlistItem, first["watchlist_item"]["id"])
    assert item.source_type == "CANDIDATE_DISCOVERY"
    assert item.source_reference == str(candidate.id)
    assert item.status == "DISCOVERED"
    assert item.monitoring_enabled is False
    assert second["watchlist_item"]["id"] == item.id
    assert second["idempotent"] is True
    assert session.scalar(select(func.count(WatchlistRevision.id))) == 1
    assert session.scalar(select(func.count(ReanalysisRequest.id))) == 1
    assert session.scalar(select(func.count(TradePlan.id))) == before_plans
    assert len(reanalysis_calls) == 1


def test_promotion_api_rejects_formal_plan_fields(client, session):
    run = CandidateDiscoveryService(session).run(now=NOW, snapshot=_snapshot())
    candidate = session.scalar(
        select(DiscoveryCandidate).where(DiscoveryCandidate.discovery_run_id == run.id)
    )
    response = client.post(
        f"/api/discovery/candidates/{candidate.id}/promote",
        json={
            "thesis": "研究",
            "analysis_capital": "300000",
            "waiting_conditions": [],
            "hard_stop": "9.5",
            "entry_low": "10",
        },
    )
    assert response.status_code == 422


def test_discovery_run_api_rejects_client_supplied_authority_time(client):
    response = client.post(
        "/api/discovery/runs",
        json={"now": "2026-07-24T19:30:00+08:00"},
    )
    assert response.status_code == 422


def test_candidate_list_endpoint_exposes_research_only_in_contraction(client, session):
    run = CandidateDiscoveryService(session).run(
        now=NOW,
        snapshot=_snapshot(market_state="CONTRACTION"),
    )
    response = client.get(f"/api/discovery/runs/{run.id}/candidates")
    assert response.status_code == 200
    assert response.json()[0]["candidate_type"] == "RESEARCH_ONLY"
