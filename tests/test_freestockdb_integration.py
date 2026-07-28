import os
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import Settings
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import get_trading_calendar, shanghai_now
from app.models import DataQualityRecord, MarketDailyBar, MarketTurnoverSnapshot
from app.providers.freestockdb import FreeStockDBHttpClient, FreeStockDBProvider
from app.services.history_persistence import persist_stock_history_bundle


pytestmark = pytest.mark.freestockdb_integration


def _provider() -> FreeStockDBProvider:
    settings = Settings(
        freestockdb_enabled=True,
        freestockdb_base_url=os.environ.get(
            "FREESTOCKDB_BASE_URL", "http://127.0.0.1:7899"
        ),
    )
    return FreeStockDBProvider(
        settings,
        client=FreeStockDBHttpClient(settings),
    )


def test_real_native_freestockdb_stock_history_contract():
    provider = _provider()
    calendar = get_trading_calendar()
    end = calendar.latest_completed_session(shanghai_now())
    start = end - timedelta(days=365)

    assert provider.health_check(probe=True)["status"] == "READY"
    raw = provider.client.daily_history("600519", start, end)
    qfq = provider.get_history("600519", start, end)
    turnover = provider.get_turnover_daily("600519", start, end)

    assert len(raw.payload) >= 80
    assert len(qfq) >= 80
    assert len(turnover) >= 80
    assert [row.trade_date for row in qfq] == [row.trade_date for row in turnover]
    latest_raw = max(raw.payload, key=lambda row: int(row["date"]))
    assert qfq[-1].close == Decimal(str(latest_raw["close"]))
    assert qfq[-1].volume == Decimal(str(latest_raw["volume"]))
    assert turnover[-1].amount == Decimal(str(latest_raw["amount"]))
    assert turnover[-1].turnover_rate == Decimal(str(latest_raw["turnover"]))
    for key in (
        "raw_daily_request_digest",
        "raw_daily_response_digest",
        "factor_request_digest",
        "factor_response_digest",
    ):
        assert len(qfq.provider_lineage[key]) == 64


def test_real_native_freestockdb_repeated_requests_are_deterministic():
    provider = _provider()
    calendar = get_trading_calendar()
    end = calendar.latest_completed_session(shanghai_now())
    start = end - timedelta(days=365)

    first = provider.get_history("600519", start, end)
    second = provider.get_history("600519", start, end)

    def stable_rows(rows):
        return [
            (
                row.symbol,
                row.trade_date,
                row.open,
                row.high,
                row.low,
                row.close,
                row.volume,
                row.adjustment,
                row.price_unit,
                row.volume_unit,
                row.observed_at,
                row.source,
            )
            for row in rows
        ]

    assert stable_rows(first) == stable_rows(second)
    assert first.provider_lineage == second.provider_lineage


def test_real_native_freestockdb_reports_missing_csi300_capability():
    provider = _provider()
    assert "market.index_daily" not in provider.metadata.supported_capabilities


def test_real_partial_window_preserves_complete_persisted_cache(session):
    provider = _provider()
    registry = ProviderRegistry()
    registry.register(provider)
    router = DataHubRouter(session, registry)
    calendar = get_trading_calendar()
    end = calendar.latest_completed_session(shanghai_now())
    full_start = end - timedelta(days=365)

    daily = router.get_history("600519", full_start, end)
    turnover = router.get_turnover_daily("600519", full_start, end)
    persist_stock_history_bundle(
        session,
        router,
        daily_result=daily,
        turnover_result=turnover,
        requested_start=full_start,
        requested_end=end,
        minimum_rows=80,
    )
    session.commit()
    old_daily = session.scalars(
        select(MarketDailyBar).where(MarketDailyBar.symbol == "600519")
    ).all()
    old_turnover = session.scalars(
        select(MarketTurnoverSnapshot).where(
            MarketTurnoverSnapshot.symbol == "600519"
        )
    ).all()
    old_daily_ids = {row.quality_record_id for row in old_daily}
    old_turnover_ids = {row.quality_record_id for row in old_turnover}

    partial_start = end - timedelta(days=30)
    partial_daily = router.get_history("600519", partial_start, end)
    partial_turnover = router.get_turnover_daily("600519", partial_start, end)

    assert partial_daily.value is None
    assert partial_turnover.value is None
    assert "insufficient" in ";".join(partial_daily.errors)
    assert "insufficient" in ";".join(partial_turnover.errors)
    assert {
        row.quality_record_id
        for row in session.scalars(
            select(MarketDailyBar).where(MarketDailyBar.symbol == "600519")
        )
    } == old_daily_ids
    assert {
        row.quality_record_id
        for row in session.scalars(
            select(MarketTurnoverSnapshot).where(
                MarketTurnoverSnapshot.symbol == "600519"
            )
        )
    } == old_turnover_ids
    assert session.get(
        DataQualityRecord, partial_daily.quality_record_id
    ).persisted is False
    assert session.get(
        DataQualityRecord, partial_turnover.quality_record_id
    ).persisted is False
