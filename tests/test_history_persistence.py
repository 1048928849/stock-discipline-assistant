from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import Settings
from app.data_hub.market_subjects import index_daily_subject
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import SHANGHAI_TZ
from app.models import DataQualityRecord, MarketDailyBar, MarketTurnoverSnapshot
from app.providers.freestockdb import FreeStockDBProvider
from app.services.history_persistence import (
    persist_index_history_window,
    persist_stock_history_bundle,
)

from test_freestockdb_provider import _payload


NOW = datetime(2026, 7, 24, 18, 0, tzinfo=SHANGHAI_TZ)


class FixtureClient:
    def __init__(self, payload):
        self.payload = payload

    def daily_history(self, **_kwargs):
        from app.providers.freestockdb import FreeStockDBResponse

        return FreeStockDBResponse(
            payload=self.payload,
            raw_response_digest="a" * 64,
            schema_version="1.0",
            source_url="http://127.0.0.1:7899/api/v1/history/daily",
        )

    def health(self):
        return {"status": "ok", "schema_version": "1.0"}


def _router(session, payload=None):
    settings = Settings(
        freestockdb_enabled=True,
        freestockdb_history_lookback_sessions=2,
        freestockdb_csi300_symbol="fixture-csi300",
    )
    provider = FreeStockDBProvider(
        settings,
        client=FixtureClient(payload or _payload()),
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
    assert daily.provider_observations[0]["raw_response_digest"] == "a" * 64
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

    payload = _payload()
    payload["data"]["rows"][0]["turnover_rate"] = None
    router = _router(session, payload)
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

    changed = _payload()
    changed["data"]["rows"][0].update(close="10.4", high="10.7")
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


def test_index_history_window_uses_existing_market_bar_cache(session):
    payload = _payload(symbol="fixture-csi300", adjustment="unadjusted")
    template = payload["data"]["rows"][0]
    payload["data"]["rows"] = [
        {**template, "trade_date": day.isoformat()}
        for day in (
            date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1),
            date(2026, 7, 2), date(2026, 7, 3), date(2026, 7, 6),
            date(2026, 7, 7), date(2026, 7, 8), date(2026, 7, 9),
            date(2026, 7, 10), date(2026, 7, 13), date(2026, 7, 14),
            date(2026, 7, 15), date(2026, 7, 16), date(2026, 7, 17),
            date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22),
            date(2026, 7, 23), date(2026, 7, 24),
        )
    ]
    router = _router(session, payload)
    result = router.get_index_history(
        "CSI000300", date(2026, 6, 29), date(2026, 7, 24)
    )
    count = persist_index_history_window(
        session,
        router,
        result=result,
        subject=index_daily_subject("CSI000300", "unadjusted", "CNY", "share"),
        requested_start=date(2026, 6, 29),
        requested_end=date(2026, 7, 24),
        minimum_rows=20,
    )
    assert count == 20
    assert {row.symbol for row in session.query(MarketDailyBar)} == {"CSI000300"}
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is True
