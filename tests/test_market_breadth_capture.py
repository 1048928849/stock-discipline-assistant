from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app import scheduler as scheduler_module
from app.data_hub.contracts import (
    DataProvider,
    MarketBreadthDaily,
    ObservedRows,
    ProviderMetadata,
    ProviderUnavailableError,
)
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.models import DataQualityRecord, MarketBreadthSnapshot
from app.services.market_breadth_capture import (
    capture_latest_market_breadth,
    capture_market_breadth,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 29, 9, 41, 15, 241225, tzinfo=SHANGHAI)
DAY = date(2026, 7, 28)


class Provider(DataProvider):
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.advancing = 2588
        self.metadata = ProviderMetadata(
            provider_id="market-breadth-eod",
            supported_capabilities=("market.breadth.daily",),
            enabled=True,
        )

    def health_check(self, probe=False):
        return {"status": "READY"}

    def get_market_breadth(self, day):
        self.calls += 1
        if self.fail:
            raise ProviderUnavailableError("source unavailable")
        observed = datetime(2026, 7, 28, 15, 0, tzinfo=SHANGHAI)
        row = MarketBreadthDaily(
            trade_date=day,
            advancing=self.advancing,
            declining=2746,
            unchanged=152,
            limit_up=61,
            limit_down=48,
            new_highs=None,
            new_lows=None,
            median_change_pct=Decimal("-0.040866"),
            above_ma20_ratio=None,
            above_ma50_ratio=None,
            observed_at=observed,
            source="freestockdb+akshare-limit-pools",
            fetched_at=NOW,
        )
        return ObservedRows(
            [row],
            observed_at=observed,
            fetched_at=NOW,
            provider_lineage={"normalized_result_digest": "a" * 64},
        )


def _router(session, provider):
    registry = ProviderRegistry()
    registry.register(provider)
    return DataHubRouter(session, registry, now_fn=lambda: NOW)


def test_same_day_capture_is_idempotent(session):
    provider = Provider()
    router = _router(session, provider)
    first = capture_market_breadth(session, DAY, router=router, evaluated_at=NOW)
    second = capture_market_breadth(session, DAY, router=router, evaluated_at=NOW)
    assert first.reused is False
    assert second.reused is True
    assert second.quality_record_id == first.quality_record_id
    assert provider.calls == 1
    assert session.scalar(select(func.count(MarketBreadthSnapshot.id))) == 1
    assert session.scalar(select(func.count(DataQualityRecord.id))) == 1


def test_force_refresh_changed_success_creates_new_lineage(session):
    provider = Provider()
    router = _router(session, provider)
    first = capture_market_breadth(session, DAY, router=router, evaluated_at=NOW)
    provider.advancing += 1
    second = capture_market_breadth(
        session,
        DAY,
        router=router,
        evaluated_at=NOW,
        force_refresh=True,
    )
    assert second.quality_record_id != first.quality_record_id
    row = session.scalar(select(MarketBreadthSnapshot))
    assert row.advancing == 2589
    assert row.quality_record_id == second.quality_record_id


def test_failed_refresh_preserves_old_trusted_cache(session):
    provider = Provider()
    router = _router(session, provider)
    first = capture_market_breadth(session, DAY, router=router, evaluated_at=NOW)
    provider.fail = True
    with pytest.raises(ProviderUnavailableError, match="source unavailable"):
        capture_market_breadth(
            session,
            DAY,
            router=router,
            evaluated_at=NOW,
            force_refresh=True,
        )
    row = session.scalar(select(MarketBreadthSnapshot))
    assert row.quality_record_id == first.quality_record_id
    failed = session.scalar(
        select(DataQualityRecord).order_by(DataQualityRecord.id.desc())
    )
    assert failed.persisted is False


def test_latest_capture_uses_resolved_completed_session(session):
    provider = Provider()
    result = capture_latest_market_breadth(
        session,
        router=_router(session, provider),
        evaluated_at=NOW,
    )
    assert result.trade_date == DAY


class SessionContext:
    def __enter__(self):
        return object()

    def __exit__(self, *args):
        return False


def test_scheduler_runs_after_close_with_database_lease(monkeypatch):
    now = datetime(2026, 7, 29, 16, 10, tzinfo=SHANGHAI)
    calls = []
    leases = []
    monkeypatch.setattr(scheduler_module, "shanghai_now", lambda: now)
    monkeypatch.setattr(scheduler_module, "SessionLocal", SessionContext)
    monkeypatch.setattr(
        scheduler_module,
        "get_settings",
        lambda: SimpleNamespace(
            market_breadth_enabled=True,
            market_breadth_capture_lease_seconds=180,
        ),
    )
    monkeypatch.setattr(
        scheduler_module,
        "acquire_monitor_lease",
        lambda db, **kwargs: leases.append(("acquire", kwargs)) or True,
    )
    monkeypatch.setattr(
        scheduler_module,
        "release_monitor_lease",
        lambda db, **kwargs: leases.append(("release", kwargs)) or True,
    )
    monkeypatch.setattr(
        scheduler_module,
        "capture_latest_market_breadth",
        lambda db, **kwargs: calls.append(kwargs),
    )
    scheduler_module.run_market_breadth_capture()
    assert len(calls) == 1
    assert [item[0] for item in leases] == ["acquire", "release"]
    assert {item[1]["lease_name"] for item in leases} == {
        "market_breadth_capture"
    }


def test_scheduler_skips_before_close_and_when_lease_is_held(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduler_module,
        "get_settings",
        lambda: SimpleNamespace(
            market_breadth_enabled=True,
            market_breadth_capture_lease_seconds=180,
        ),
    )
    monkeypatch.setattr(
        scheduler_module,
        "capture_latest_market_breadth",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        scheduler_module,
        "shanghai_now",
        lambda: datetime(2026, 7, 29, 14, 59, tzinfo=SHANGHAI),
    )
    scheduler_module.run_market_breadth_capture()
    monkeypatch.setattr(
        scheduler_module,
        "shanghai_now",
        lambda: datetime(2026, 7, 29, 16, 10, tzinfo=SHANGHAI),
    )
    monkeypatch.setattr(scheduler_module, "SessionLocal", SessionContext)
    monkeypatch.setattr(
        scheduler_module, "acquire_monitor_lease", lambda *args, **kwargs: False
    )
    scheduler_module.run_market_breadth_capture()
    assert calls == []
