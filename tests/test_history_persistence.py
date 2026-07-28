from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import Settings
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import SHANGHAI_TZ
from app.models import DataQualityRecord, MarketDailyBar, MarketTurnoverSnapshot
from app.providers.freestockdb import FreeStockDBProvider
from app.services.history_persistence import persist_stock_history_bundle


NOW = datetime(2026, 7, 24, 18, 0, tzinfo=SHANGHAI_TZ)


def _native_rows(symbol="600519"):
    return [
        {
            "date": 20260724,
            "code": symbol,
            "name": "fixture",
            "open": "10.5",
            "high": "10.9",
            "low": "10.3",
            "close": "10.8",
            "pre_close": "10.4",
            "volume": "1200",
            "amount": "12800",
            "turnover": "1.4",
        },
        {
            "date": 20260723,
            "code": symbol,
            "name": "fixture",
            "open": "10.0",
            "high": "10.7",
            "low": "9.8",
            "close": "10.2",
            "pre_close": "9.6",
            "volume": "1000",
            "amount": "10200",
            "turnover": "1.2",
        },
    ]


class FixtureClient:
    def __init__(self, rows):
        self.rows = rows

    @staticmethod
    def _response(payload, operation, digest):
        from app.providers.freestockdb import FreeStockDBResponse

        return FreeStockDBResponse(
            payload=payload,
            raw_response_digest=digest * 64,
            request_digest=("d" if operation == "daily" else "f") * 64,
            source_url="http://127.0.0.1:7899/",
            operation=operation,
        )

    def daily_history(self, symbol, start, end):
        assert symbol == self.rows[0]["code"]
        assert start <= end
        return self._response(self.rows, "daily", "a")

    def adjustment_factors(self, symbol):
        assert symbol == self.rows[0]["code"]
        return self._response([], "factors", "b")


def _router(session, rows=None):
    settings = Settings(
        freestockdb_enabled=True,
        freestockdb_history_lookback_sessions=2,
    )
    provider = FreeStockDBProvider(
        settings,
        client=FixtureClient(rows or _native_rows()),
        now_fn=lambda: NOW,
    )
    registry = ProviderRegistry()
    registry.register(provider)
    return DataHubRouter(session, registry, now_fn=lambda: NOW)


def test_stock_history_bundle_persists_exact_aligned_lineage(session):
    router = _router(session)
    daily = router.get_history("600519", date(2026, 7, 23), date(2026, 7, 24))
    turnover = router.get_turnover_daily(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    assert daily.quality_status.value == "SINGLE_SOURCE"
    assert daily.provider_observations[0]["adapter_version"] == "1.0.0"
    assert daily.provider_observations[0]["raw_daily_response_digest"] == "a" * 64
    assert "?" not in daily.provider_observations[0]["source_url"]

    written = persist_stock_history_bundle(
        session,
        router,
        daily_result=daily,
        turnover_result=turnover,
        requested_start=date(2026, 7, 23),
        requested_end=date(2026, 7, 24),
        minimum_rows=2,
    )

    assert written == 4
    assert session.query(MarketDailyBar).count() == 2
    assert session.query(MarketTurnoverSnapshot).count() == 2
    assert {
        row.persisted
        for row in session.scalars(
            select(DataQualityRecord).where(
                DataQualityRecord.id.in_([daily.quality_record_id, turnover.quality_record_id])
            )
        )
    } == {True}


def test_stock_history_bundle_rejects_misaligned_dates_before_delete(session):
    old_router = _router(session)
    old_daily = old_router.get_history(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    old_turnover = old_router.get_turnover_daily(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    persist_stock_history_bundle(
        session,
        old_router,
        daily_result=old_daily,
        turnover_result=old_turnover,
        requested_start=date(2026, 7, 23),
        requested_end=date(2026, 7, 24),
        minimum_rows=2,
    )
    session.commit()
    old_ids = {row.quality_record_id for row in session.query(MarketDailyBar)}

    rows = _native_rows()
    rows[0]["turnover"] = None
    router = _router(session, rows)
    daily = router.get_history("600519", date(2026, 7, 23), date(2026, 7, 24))
    missing = router.get_turnover_daily(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    assert missing.value is None

    assert {row.quality_record_id for row in session.query(MarketDailyBar)} == old_ids
    assert session.get(DataQualityRecord, daily.quality_record_id).persisted is False


def test_qfq_rewrite_updates_window_and_preserves_older_cache(session):
    router = _router(session)
    daily = router.get_history("600519", date(2026, 7, 23), date(2026, 7, 24))
    turnover = router.get_turnover_daily(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    session.add(
        MarketDailyBar(
            symbol="600519",
            trade_date=date(2026, 7, 22),
            open=Decimal("9"), high=Decimal("9"), low=Decimal("9"), close=Decimal("9"),
            volume=Decimal("1"), adjustment="qfq", price_unit="CNY",
            volume_unit="share", observed_at=datetime(2026, 7, 22, 15, 0),
            quality_status="SINGLE_SOURCE", quality_record_id=daily.quality_record_id,
            source="free-stockdb", fetched_at=datetime(2026, 7, 22, 18, 0),
        )
    )
    session.flush()
    persist_stock_history_bundle(
        session,
        router,
        daily_result=daily,
        turnover_result=turnover,
        requested_start=date(2026, 7, 23),
        requested_end=date(2026, 7, 24),
        minimum_rows=2,
    )
    rows = session.scalars(
        select(MarketDailyBar).order_by(MarketDailyBar.trade_date)
    ).all()
    assert [row.trade_date for row in rows] == [
        date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)
    ]


def test_qfq_rewrite_accepts_legitimate_historical_price_change(session):
    first_router = _router(session)
    first_daily = first_router.get_history(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    first_turnover = first_router.get_turnover_daily(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    persist_stock_history_bundle(
        session,
        first_router,
        daily_result=first_daily,
        turnover_result=first_turnover,
        requested_start=date(2026, 7, 23),
        requested_end=date(2026, 7, 24),
        minimum_rows=2,
    )
    session.commit()

    changed = _native_rows()
    changed[1].update(close="10.4", high="10.7")
    second_router = _router(session, changed)
    second_daily = second_router.get_history(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    second_turnover = second_router.get_turnover_daily(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    persist_stock_history_bundle(
        session,
        second_router,
        daily_result=second_daily,
        turnover_result=second_turnover,
        requested_start=date(2026, 7, 23),
        requested_end=date(2026, 7, 24),
        minimum_rows=2,
    )

    rows = session.scalars(
        select(MarketDailyBar).where(MarketDailyBar.symbol == "600519")
        .order_by(MarketDailyBar.trade_date)
    ).all()
    assert len(rows) == 2
    assert rows[0].close == Decimal("10.4000")
    assert {row.quality_record_id for row in rows} == {second_daily.quality_record_id}
    assert second_daily.normalized_digest != first_daily.normalized_digest


def test_mark_persisted_failure_rolls_back_both_series_and_preserves_old_cache(
    session, monkeypatch
):
    router = _router(session)
    daily = router.get_history("600519", date(2026, 7, 23), date(2026, 7, 24))
    turnover = router.get_turnover_daily(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    session.add(
        MarketDailyBar(
            symbol="600519", trade_date=date(2026, 7, 23), open=Decimal("8"),
            high=Decimal("8"), low=Decimal("8"), close=Decimal("8"), volume=Decimal("1"),
            adjustment="qfq", price_unit="CNY", volume_unit="share",
            observed_at=datetime(2026, 7, 23, 15), quality_status="SINGLE_SOURCE",
            quality_record_id=daily.quality_record_id, source="old-source",
            fetched_at=datetime(2026, 7, 23, 18),
        )
    )
    session.flush()
    monkeypatch.setattr(
        router,
        "mark_persisted",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("mark failed")),
    )
    with pytest.raises(RuntimeError, match="mark failed"):
        persist_stock_history_bundle(
            session,
            router,
            daily_result=daily,
            turnover_result=turnover,
            requested_start=date(2026, 7, 23),
            requested_end=date(2026, 7, 24),
            minimum_rows=2,
        )
    rows = session.scalars(select(MarketDailyBar)).all()
    assert len(rows) == 1
    assert rows[0].source == "old-source"
    assert session.get(DataQualityRecord, daily.quality_record_id).persisted is False
    assert session.get(DataQualityRecord, turnover.quality_record_id).persisted is False


def test_index_history_is_honestly_unavailable(session):
    router = _router(session)
    result = router.get_index_history(
        "CSI000300", date(2026, 7, 23), date(2026, 7, 24)
    )
    assert result.value is None
    assert result.quality_status.value == "MISSING"
    assert result.provider_observations == []
    assert session.query(MarketDailyBar).count() == 0
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is False
