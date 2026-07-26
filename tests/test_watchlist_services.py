from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from queue import Queue
from types import SimpleNamespace
from threading import Barrier, Thread

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.data_hub.contracts import (
    DataProvider,
    ProviderMetadata,
    ProviderUnavailableError,
    Quote,
)
from app.data_hub.market_subjects import (
    index_daily_subject,
    industry_universe_subject,
    market_amount_subject,
    market_breadth_subject,
)
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import (
    SHANGHAI_TZ,
    TradingPhase,
    to_market_storage_naive,
    to_utc_storage_naive,
)
from app.database import Base
from app.errors import AppError
from app.models import (
    DataQualityRecord,
    IndustryAnalysisSnapshot,
    MarketQuote,
    MarketRegimeSnapshot,
    MonitoringEvent,
    ReanalysisRequest,
    ReanalysisRun,
    TradePlan,
    WatchlistItem,
    WatchlistRevision,
    WatchlistTransition,
)
from app.watchlist.contracts import (
    MonitoringRuleType,
    WatchlistCreateRequest,
    WatchlistPatchRequest,
    WatchlistSourceType,
    WatchlistStatus,
)
from app.watchlist.events import create_event_once
from app.watchlist.lease import acquire_monitor_lease, release_monitor_lease
from app.watchlist.monitoring import WatchlistMonitoringService
from app.watchlist.service import (
    archive_watchlist_item,
    create_watchlist_item,
    patch_watchlist_item,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=SHANGHAI_TZ)


class ClosedCalendar:
    def market_phase(self, now=None):
        return TradingPhase.NON_TRADING_DAY


class ForbiddenRouter:
    def get_quote(self, symbol):
        raise AssertionError("quote provider must not run outside a trading session")


class ActiveCalendar:
    def market_phase(self, now=None):
        return TradingPhase.MORNING_SESSION

    def is_realtime_session(self, now=None):
        return True


class QuoteProvider(DataProvider):
    def __init__(self, provider_id: str, price: str, priority: int):
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=("market.quote.realtime",),
            priority=priority,
            realtime_supported=True,
        )
        self.price = Decimal(price)

    def health_check(self, probe=False):
        return {"status": "healthy"}

    def get_quote(self, symbol):
        return Quote(
            symbol=symbol,
            name="测试公司",
            price=self.price,
            quote_type="realtime",
            observed_at=NOW,
            price_unit="CNY",
            source=self.metadata.provider_id,
            source_api="watchlist-test",
            fetched_at=NOW,
        )


def _manual_item(session):
    return create_watchlist_item(
        session,
        WatchlistCreateRequest(
            source_type=WatchlistSourceType.MANUAL,
            symbol="300502",
            thesis="等待正式分析",
            analysis_capital=Decimal("300000"),
        ),
    )


def test_manual_watchlist_creates_immutable_revision_one(session):
    item = _manual_item(session)
    first = session.scalar(
        select(WatchlistRevision).where(WatchlistRevision.watchlist_item_id == item.id)
    )
    assert first is not None
    assert first.revision_number == 1
    assert first.previous_revision_number is None
    assert first.snapshot["thesis"] == "等待正式分析"
    assert item.status == WatchlistStatus.DISCOVERED.value


def test_plan_patch_appends_revision_without_overwriting_history(session):
    item = _manual_item(session)
    patch_watchlist_item(
        session,
        item,
        WatchlistPatchRequest(thesis="等待价格与市场共振"),
    )
    rows = session.scalars(
        select(WatchlistRevision)
        .where(WatchlistRevision.watchlist_item_id == item.id)
        .order_by(WatchlistRevision.revision_number)
    ).all()
    assert [row.revision_number for row in rows] == [1, 2]
    assert rows[0].snapshot["thesis"] == "等待正式分析"
    assert rows[1].snapshot["thesis"] == "等待价格与市场共振"


def test_archive_is_explicit_and_monitoring_cannot_restore_item(session):
    item = _manual_item(session)
    archive_watchlist_item(session, item)
    summary = WatchlistMonitoringService(
        session,
        router=ForbiddenRouter(),
        calendar=ClosedCalendar(),
        settings=Settings(watchlist_monitor_batch_size=10),
    ).scan(now=NOW)
    assert item.status == WatchlistStatus.ARCHIVED.value
    assert item.monitoring_enabled is False
    assert summary.scanned == 0


def test_monitoring_cannot_be_enabled_without_complete_active_plan(session):
    item = _manual_item(session)
    with pytest.raises(AppError) as exc_info:
        patch_watchlist_item(
            session,
            item,
            WatchlistPatchRequest(monitoring_enabled=True),
        )
    assert exc_info.value.code == "WATCHLIST_MONITORING_PLAN_REQUIRED"


def test_event_dedupe_and_cooldown_are_database_backed(session):
    item = _manual_item(session)
    first, created_first = create_event_once(
        session,
        item,
        event_type=MonitoringRuleType.DATA_QUALITY_DEGRADED,
        severity="ATTENTION",
        title="数据质量阻断",
        reason_codes=["MISSING"],
        observed_at=NOW,
        payload={"quality_status": "MISSING"},
        reanalysis_required=False,
        cooldown_seconds=300,
    )
    second, created_second = create_event_once(
        session,
        item,
        event_type=MonitoringRuleType.DATA_QUALITY_DEGRADED,
        severity="ATTENTION",
        title="数据质量阻断",
        reason_codes=["MISSING"],
        observed_at=NOW + timedelta(seconds=10),
        payload={"quality_status": "MISSING"},
        reanalysis_required=False,
        cooldown_seconds=300,
    )
    session.commit()
    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert session.scalar(select(MonitoringEvent)) is first


def test_database_lease_race_allows_only_one_independent_session(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'lease.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    barrier = Barrier(3)
    outcomes = Queue()

    def worker(token):
        with factory() as db:
            barrier.wait()
            outcomes.put(
                (token, acquire_monitor_lease(db, owner_token=token, lease_seconds=60, now=NOW))
            )

    threads = [Thread(target=worker, args=(token,)) for token in ("worker-a", "worker-b")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    results = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert sorted(acquired for _, acquired in results) == [False, True]
    winner = next(token for token, acquired in results if acquired)
    with factory() as first, factory() as second:
        assert release_monitor_lease(first, owner_token=winner)
        assert acquire_monitor_lease(
            second, owner_token="worker-b", lease_seconds=60, now=NOW
        )


def test_non_trading_session_never_calls_realtime_provider(session):
    item = _manual_item(session)
    item.status = WatchlistStatus.WATCHING.value
    item.monitoring_enabled = True
    item.entry_low = Decimal("10.40")
    item.entry_high = Decimal("10.60")
    item.hard_stop = Decimal("9.70")
    session.commit()
    summary = WatchlistMonitoringService(
        session,
        router=ForbiddenRouter(),
        calendar=ClosedCalendar(),
        settings=Settings(watchlist_monitor_batch_size=10),
    ).scan(now=NOW)
    assert summary.scanned == 1
    assert summary.unchanged == 1
    assert item.status == WatchlistStatus.WATCHING.value


def _watching_item(session):
    item = _manual_item(session)
    item.status = WatchlistStatus.WATCHING.value
    item.monitoring_enabled = True
    item.entry_low = Decimal("10.40")
    item.entry_high = Decimal("10.60")
    item.hard_stop = Decimal("9.70")
    session.commit()
    return item


def _quote_router(session, *providers):
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return DataHubRouter(
        session,
        registry,
        calendar=ActiveCalendar(),
        now_fn=lambda: NOW,
    )


def _quality_binding(session, capability, subject):
    stored_now = to_market_storage_naive(NOW)
    record = DataQualityRecord(
        capability=capability,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        semantic_key=subject.semantic_key,
        quality_status="SINGLE_SOURCE",
        observed_at=stored_now,
        fetched_at=stored_now,
        cached_at=stored_now,
        provider_id="watchlist-context",
        provider_observations=[],
        normalized_digest="c" * 64,
        conflict_fields=[],
        row_count=1,
        trusted=True,
        persisted=True,
    )
    session.add(record)
    session.flush()
    return {
        "capability": capability,
        "subject": subject.model_dump(mode="json"),
        "quality_record_id": record.id,
        "observed_at": NOW.isoformat(),
    }


def _persist_monitoring_contexts(
    session,
    *,
    market_state="CONTRACTION",
    industry_state="FADING",
):
    index = index_daily_subject("CSI000300", "unadjusted", "CNY", "share")
    market_bindings = [
        _quality_binding(session, "market.breadth.daily", market_breadth_subject()),
        _quality_binding(session, "market.amount.daily", market_amount_subject()),
        _quality_binding(session, "market.index_daily", index),
    ]
    industry_bindings = [
        _quality_binding(
            session,
            "market.industry.daily",
            industry_universe_subject(),
        ),
        _quality_binding(
            session,
            "market.industry.constituents",
            industry_universe_subject(constituents=True),
        ),
        market_bindings[-1],
    ]
    session.add_all(
        [
            MarketRegimeSnapshot(
                market_id="CN-A",
                trade_date=date(2026, 7, 27),
                state=market_state,
                previous_state="EXPANSION",
                transition=f"EXPANSION->{market_state}",
                product_snapshot_hash="d" * 64,
                observed_at=to_market_storage_naive(NOW),
                quality_status="SINGLE_SOURCE",
                quality_bindings=market_bindings,
            ),
            IndustryAnalysisSnapshot(
                industry_name="半导体",
                trade_date=date(2026, 7, 27),
                classification=industry_state,
                product_snapshot_hash="e" * 64,
                observed_at=to_market_storage_naive(NOW),
                quality_status="SINGLE_SOURCE",
                quality_bindings=industry_bindings,
            ),
        ]
    )
    session.commit()


def test_monitoring_uses_datahub_quality_and_persists_exact_quote(session):
    item = _watching_item(session)
    summary = WatchlistMonitoringService(
        session,
        router=_quote_router(session, QuoteProvider("quote-a", "11.00", 1)),
        calendar=ActiveCalendar(),
        settings=Settings(watchlist_monitor_batch_size=10),
    ).scan(now=NOW)
    session.refresh(item)
    quote = session.scalar(select(MarketQuote).where(MarketQuote.symbol == item.symbol))
    assert quote is not None, [
        (row.quality_status, row.provider_observations)
        for row in session.scalars(select(DataQualityRecord)).all()
    ]
    record = session.get(DataQualityRecord, quote.quality_record_id)
    assert summary.unchanged == 1
    assert item.status == WatchlistStatus.WATCHING.value
    assert item.monitoring_health == "HEALTHY"
    assert item.current_price == Decimal("11.0000")
    assert record.persisted is True


def test_conflicted_quote_blocks_price_trigger_and_preserves_logic_status(session):
    item = _watching_item(session)
    summary = WatchlistMonitoringService(
        session,
        router=_quote_router(
            session,
            QuoteProvider("quote-a", "10.50", 1),
            QuoteProvider("quote-b", "10.60", 2),
        ),
        calendar=ActiveCalendar(),
        settings=Settings(watchlist_monitor_batch_size=10),
    ).scan(now=NOW)
    session.refresh(item)
    assert summary.blocked == 1
    assert item.status == WatchlistStatus.WATCHING.value
    assert item.monitoring_health == "CONFLICTED", [
        (row.quality_status, row.provider_observations)
        for row in session.scalars(select(DataQualityRecord)).all()
    ]
    assert item.current_price is None


def test_market_regime_downgrade_is_idempotent_and_requests_reanalysis(
    session, monkeypatch
):
    item = _watching_item(session)
    item.market_state = "EXPANSION"
    _persist_monitoring_contexts(session, market_state="CONTRACTION")
    monkeypatch.setattr(
        "app.watchlist.monitoring.execute_reanalysis",
        lambda db, request_id: SimpleNamespace(status="SUCCEEDED"),
    )
    service = WatchlistMonitoringService(
        session,
        router=_quote_router(session, QuoteProvider("quote-a", "11.00", 1)),
        calendar=ActiveCalendar(),
        settings=Settings(watchlist_monitor_interval_seconds=300),
    )
    service.scan(now=NOW)
    service.scan(now=NOW + timedelta(seconds=10))
    events = session.scalars(
        select(MonitoringEvent).where(
            MonitoringEvent.event_type == "MARKET_REGIME_DOWNGRADE"
        )
    ).all()
    requests = session.scalars(select(ReanalysisRequest)).all()
    assert len(events) == 1
    assert events[0].payload["previous_state"] == "EXPANSION"
    assert events[0].payload["current_state"] == "CONTRACTION"
    assert len(requests) == 1


def test_industry_status_downgrade_does_not_invalidate(session, monkeypatch):
    item = _watching_item(session)
    item.industry_name = "半导体"
    item.industry_state = "MAINLINE"
    _persist_monitoring_contexts(session, industry_state="FADING")
    monkeypatch.setattr(
        "app.watchlist.monitoring.execute_reanalysis",
        lambda db, request_id: SimpleNamespace(status="SUCCEEDED"),
    )
    WatchlistMonitoringService(
        session,
        router=_quote_router(session, QuoteProvider("quote-a", "11.00", 1)),
        calendar=ActiveCalendar(),
        settings=Settings(),
    ).scan(now=NOW)
    session.refresh(item)
    event = session.scalar(
        select(MonitoringEvent).where(
            MonitoringEvent.event_type == "INDUSTRY_STATUS_DOWNGRADE"
        )
    )
    assert event is not None
    assert item.status == WatchlistStatus.WATCHING.value
    assert session.scalar(select(ReanalysisRequest)) is not None


def test_stale_plan_event_and_request_are_deduplicated_in_window(
    session, monkeypatch
):
    item = _watching_item(session)
    item.last_analyzed_at = to_utc_storage_naive(NOW - timedelta(hours=2))
    session.commit()
    monkeypatch.setattr(
        "app.watchlist.monitoring.execute_reanalysis",
        lambda db, request_id: SimpleNamespace(status="SUCCEEDED"),
    )
    service = WatchlistMonitoringService(
        session,
        router=_quote_router(session, QuoteProvider("quote-a", "11.00", 1)),
        calendar=ActiveCalendar(),
        settings=Settings(watchlist_plan_max_age_seconds=3600),
    )
    service.scan(now=NOW)
    service.scan(now=NOW + timedelta(seconds=10))
    events = session.scalars(
        select(MonitoringEvent).where(
            MonitoringEvent.event_type == "PLAN_BECAME_STALE"
        )
    ).all()
    assert len(events) == 1
    assert session.query(ReanalysisRequest).count() == 1


def test_structured_price_invalidation_is_critical_and_permanent(session):
    item = _watching_item(session)
    item.invalidation_rule_specs = [
        {
            "rule_type": "PRICE_AT_OR_BELOW_HARD_STOP",
            "threshold": "9.70",
            "source": "PRODUCT_ANALYSIS",
            "evidence_reference": "package:bound",
            "created_at": NOW.isoformat(),
        }
    ]
    session.commit()
    WatchlistMonitoringService(
        session,
        router=_quote_router(session, QuoteProvider("quote-a", "9.69", 1)),
        calendar=ActiveCalendar(),
        settings=Settings(),
    ).scan(now=NOW)
    session.refresh(item)
    event = session.scalar(
        select(MonitoringEvent).where(
            MonitoringEvent.event_type == "PRICE_BREAK_HARD_STOP"
        )
    )
    assert item.status == WatchlistStatus.INVALIDATED.value
    assert event is not None and event.severity == "CRITICAL"
    assert event.payload["matched_rule"]["threshold"] == "9.70"


def test_structured_market_invalidation_uses_trusted_state(session):
    item = _watching_item(session)
    item.market_state = "EXPANSION"
    item.invalidation_rule_specs = [
        {
            "rule_type": "MARKET_REGIME_AT_OR_WORSE_THAN",
            "threshold": "CONTRACTION",
            "source": "USER",
            "evidence_reference": "evidence:market-policy",
            "created_at": NOW.isoformat(),
        }
    ]
    _persist_monitoring_contexts(session, market_state="PANIC")
    WatchlistMonitoringService(
        session,
        router=_quote_router(session, QuoteProvider("quote-a", "11.00", 1)),
        calendar=ActiveCalendar(),
        settings=Settings(),
    ).scan(now=NOW)
    session.refresh(item)
    event = session.scalar(
        select(MonitoringEvent).where(
            MonitoringEvent.event_type == "PLAN_INVALIDATION_TRIGGERED"
        )
    )
    assert item.status == WatchlistStatus.INVALIDATED.value
    assert event is not None and event.severity == "CRITICAL"
    assert event.payload["matched_rule"]["threshold"] == "CONTRACTION"
    assert "quality_record:" in " ".join(event.payload["evidence_references"])


def test_below_entry_requests_reassessment_without_buy_trigger(session, monkeypatch):
    item = _watching_item(session)
    monkeypatch.setattr(
        "app.watchlist.monitoring.execute_reanalysis",
        lambda db, request_id: SimpleNamespace(status="SUCCEEDED"),
    )
    WatchlistMonitoringService(
        session,
        router=_quote_router(session, QuoteProvider("quote-a", "10.00", 1)),
        calendar=ActiveCalendar(),
        settings=Settings(),
    ).scan(now=NOW)
    session.refresh(item)
    request = session.scalar(select(ReanalysisRequest))
    assert item.status == WatchlistStatus.WATCHING.value
    assert request is not None
    assert request.reason_codes == ["BELOW_ENTRY_REQUIRES_REASSESSMENT"]


def test_missing_market_context_blocks_entry_without_invalidating(session):
    item = _watching_item(session)
    item.market_state = "EXPANSION"
    session.commit()
    WatchlistMonitoringService(
        session,
        router=_quote_router(session, QuoteProvider("quote-a", "10.50", 1)),
        calendar=ActiveCalendar(),
        settings=Settings(),
    ).scan(now=NOW)
    session.refresh(item)
    assert item.status == WatchlistStatus.WATCHING.value
    assert item.monitoring_health == "DATA_BLOCKED"
    assert session.scalar(
        select(MonitoringEvent).where(
            MonitoringEvent.event_type == "PRICE_ENTER_ENTRY_ZONE"
        )
    ) is None


def test_new_market_conflict_blocks_downgrade_and_entry(session):
    item = _watching_item(session)
    item.market_state = "EXPANSION"
    _persist_monitoring_contexts(session, market_state="PANIC")
    bound = session.scalar(
        select(DataQualityRecord).where(
            DataQualityRecord.capability == "market.breadth.daily"
        )
    )
    session.add(
        DataQualityRecord(
            capability=bound.capability,
            subject_type=bound.subject_type,
            subject_id=bound.subject_id,
            semantic_key=bound.semantic_key,
            quality_status="CONFLICTED",
            observed_at=bound.observed_at,
            fetched_at=bound.fetched_at,
            provider_id="conflicting-market-source",
            provider_observations=[],
            normalized_digest="f" * 64,
            conflict_fields=["canonical_business_value"],
            row_count=1,
            trusted=False,
            persisted=False,
        )
    )
    session.commit()
    WatchlistMonitoringService(
        session,
        router=_quote_router(session, QuoteProvider("quote-a", "10.50", 1)),
        calendar=ActiveCalendar(),
        settings=Settings(),
    ).scan(now=NOW)
    session.refresh(item)
    event_types = set(session.scalars(select(MonitoringEvent.event_type)).all())
    assert item.monitoring_health == "CONFLICTED"
    assert item.status == WatchlistStatus.WATCHING.value
    assert "MARKET_REGIME_DOWNGRADE" not in event_types
    assert "PRICE_ENTER_ENTRY_ZONE" not in event_types


def test_stale_industry_context_blocks_downgrade_and_entry(session):
    item = _watching_item(session)
    item.industry_name = "半导体"
    item.industry_state = "MAINLINE"
    _persist_monitoring_contexts(session, industry_state="FADING")
    snapshot = session.scalar(select(IndustryAnalysisSnapshot))
    old_time = NOW - timedelta(days=30)
    old_stored = to_market_storage_naive(old_time)
    for binding in snapshot.quality_bindings:
        record = session.get(DataQualityRecord, binding["quality_record_id"])
        record.observed_at = old_stored
        record.cached_at = old_stored
        binding["observed_at"] = old_time.isoformat()
    snapshot.quality_bindings = list(snapshot.quality_bindings)
    session.commit()
    WatchlistMonitoringService(
        session,
        router=_quote_router(session, QuoteProvider("quote-a", "10.50", 1)),
        calendar=ActiveCalendar(),
        settings=Settings(),
    ).scan(now=NOW)
    session.refresh(item)
    event_types = set(session.scalars(select(MonitoringEvent.event_type)).all())
    assert item.monitoring_health == "STALE"
    assert item.status == WatchlistStatus.WATCHING.value
    assert "INDUSTRY_STATUS_DOWNGRADE" not in event_types
    assert "PRICE_ENTER_ENTRY_ZONE" not in event_types


def test_reanalysis_recalculation_binds_transition_to_new_revision(session):
    from app.watchlist.reanalysis import _recalculate_status
    from app.watchlist.service import append_revision

    item = _watching_item(session)
    item.status = WatchlistStatus.ENTRY_TRIGGERED.value
    item.current_price = Decimal("11.00")
    item.current_price_observed_at = to_market_storage_naive(NOW)
    item.latest_package_hash = "a" * 64
    item.latest_snapshot_hash = "b" * 64
    append_revision(
        session,
        item,
        reason="AUTOMATIC_REANALYSIS",
        changed_by="SYSTEM",
    )
    _recalculate_status(session, item, observed_at=NOW)
    session.commit()
    transition = session.scalar(
        select(WatchlistTransition).where(
            WatchlistTransition.watchlist_item_id == item.id
        )
    )
    assert item.status == WatchlistStatus.WATCHING.value
    assert transition.revision_number == item.revision == 2


def test_watchlist_openapi_exposes_complete_mvp(client):
    paths = client.app.openapi()["paths"]
    assert {
        "/api/watchlist/items",
        "/api/watchlist/items/{item_id}",
        "/api/watchlist/items/{item_id}/archive",
        "/api/watchlist/items/{item_id}/reanalyze",
        "/api/watchlist/items/{item_id}/revisions",
        "/api/watchlist/items/{item_id}/transitions",
        "/api/watchlist/events",
        "/api/watchlist/events/{event_id}/acknowledge",
        "/api/watchlist/scan",
    } <= set(paths)


def test_watchlist_page_contains_console_and_server_reference_action(client):
    response = client.get("/watchlist")
    assert response.status_code == 200
    assert "观察名单" in response.text
    script = client.get("/static/product-v1.js?v=20260726")
    assert script.status_code == 200
    assert "add-analysis-to-watchlist" in script.text
    assert 'analysis_run_id: Number(action.dataset.runId)' in script.text


def test_manual_api_rejects_untrusted_plan_fields(client):
    response = client.post(
        "/api/watchlist/items",
        json={
            "source_type": "PRODUCT_ANALYSIS",
            "analysis_run_id": 1,
            "thesis": "forged",
            "entry_low": "1",
            "hard_stop": "0.5",
        },
    )
    assert response.status_code == 422


def test_product_analysis_creates_bound_watchlist_and_real_reanalysis(
    client, session, monkeypatch
):
    from test_one_click_pipeline import _analyze_bound_plan

    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    package = analyzed["decision_package"]
    response = client.post(
        "/api/watchlist/items",
        json={
            "source_type": "PRODUCT_ANALYSIS",
            "analysis_run_id": analyzed["run_id"],
            "thesis": "等待价格进入确定性买入区",
        },
    )
    assert response.status_code == 201, response.text
    item = response.json()
    binding = package["strategy_bindings"][0]
    assert Decimal(str(item["entry_low"])) == Decimal(
        str(analyzed["plan"]["buy_plan"]["buy_zone"][0])
    )
    assert Decimal(str(item["hard_stop"])) == Decimal(
        str(analyzed["plan"]["buy_plan"]["hard_stop"])
    )
    assert item["strategy_id"] == binding["strategy_id"]
    assert item["strategy_binding_hash"] == binding["binding_hash"]
    assert item["latest_snapshot_hash"] == package["product_snapshot_hash"]
    assert item["latest_package_hash"] == package["package_hash"]

    reanalyzed = client.post(f"/api/watchlist/items/{item['id']}/reanalyze")
    assert reanalyzed.status_code == 200, reanalyzed.text
    run = session.get(ReanalysisRun, reanalyzed.json()["id"])
    assert run is not None
    assert run.status in {"SUCCEEDED", "BLOCKED"}, (
        run.error_code,
        run.error_message,
    )
    assert session.query(TradePlan).count() == 0


def test_manual_discovered_item_uses_real_reanalysis_lifecycle(
    client, session, monkeypatch
):
    from test_one_click_pipeline import _analyze_bound_plan

    _analyze_bound_plan(client, session, monkeypatch)
    created = client.post(
        "/api/watchlist/items",
        json={
            "source_type": "MANUAL",
            "symbol": "300502",
            "thesis": "等待完整确定性分析",
            "analysis_capital": "300000",
        },
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]
    response = client.post(f"/api/watchlist/items/{item_id}/reanalyze")
    assert response.status_code == 200, response.text
    item = session.get(WatchlistItem, item_id)
    assert response.json()["status"] == "SUCCEEDED"
    assert item.status in {
        WatchlistStatus.WATCHING.value,
        WatchlistStatus.NEAR_ENTRY.value,
        WatchlistStatus.ENTRY_TRIGGERED.value,
    }
    assert item.monitoring_enabled is True
    transitions = session.scalars(
        select(WatchlistTransition)
        .where(WatchlistTransition.watchlist_item_id == item_id)
        .order_by(WatchlistTransition.id)
    ).all()
    assert transitions[0].from_status == WatchlistStatus.DISCOVERED.value
    assert transitions[0].to_status == WatchlistStatus.RESEARCHING.value
    assert transitions[-1].revision_number == item.revision


def test_manual_reanalysis_data_block_preserves_researching_without_fake_plan(
    client, session, monkeypatch
):
    from test_one_click_pipeline import _analyze_bound_plan

    _analyze_bound_plan(client, session, monkeypatch)
    quote = session.scalar(select(MarketQuote).where(MarketQuote.symbol == "300502"))
    record = session.get(DataQualityRecord, quote.quality_record_id)
    old_time = to_market_storage_naive(NOW - timedelta(days=30))
    quote.observed_at = old_time
    record.observed_at = old_time
    record.cached_at = old_time
    session.commit()

    def unavailable_quote(self, symbol):
        raise ProviderUnavailableError(f"quote unavailable for {symbol}")

    monkeypatch.setattr(
        "app.providers.akshare_provider.AKShareProvider.get_quote",
        unavailable_quote,
    )
    created = client.post(
        "/api/watchlist/items",
        json={
            "source_type": "MANUAL",
            "symbol": "300502",
            "thesis": "等待数据恢复",
            "analysis_capital": "300000",
        },
    )
    item_id = created.json()["id"]
    response = client.post(f"/api/watchlist/items/{item_id}/reanalyze")
    item = session.get(WatchlistItem, item_id)
    assert response.status_code == 200
    assert response.json()["status"] == "BLOCKED"
    assert item.status == WatchlistStatus.RESEARCHING.value
    assert item.monitoring_health in {"DATA_BLOCKED", "STALE", "CONFLICTED"}
    assert item.entry_low is None
    assert item.revision == 1
