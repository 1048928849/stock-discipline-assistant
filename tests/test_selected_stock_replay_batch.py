from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.data_hub.market_subjects import index_daily_subject, stock_daily_subject
from app.data_hub.trading_calendar import get_trading_calendar, to_market_storage_naive
from app.models import DataQualityRecord, MarketDailyBar
from app.selected_stock.replay import BatchReplayRequest, SelectedStockReplayService


NOW = datetime(2026, 7, 31, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
END = date(2026, 7, 31)


def _sessions(count: int) -> list[date]:
    calendar = get_trading_calendar()
    result = []
    day = END
    while len(result) < count:
        if calendar.is_session(day):
            result.append(day)
        day -= timedelta(days=1)
    return sorted(result)


def _seed_series(session, *, symbol: str, capability: str, adjustment: str, offset: Decimal):
    subject = (
        stock_daily_subject(symbol, adjustment, "CNY", "share")
        if capability == "market.daily.qfq"
        else index_daily_subject(symbol, adjustment, "CNY", "share")
    )
    sessions = _sessions(320)
    observed = to_market_storage_naive(get_trading_calendar().session_close_at(END))
    quality = DataQualityRecord(
        symbol=symbol,
        capability=capability,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        semantic_key=subject.semantic_key,
        quality_status="SINGLE_SOURCE",
        observed_at=observed,
        fetched_at=observed,
        provider_id="batch-replay-fixture",
        provider_observations=[],
        normalized_digest=("a" if capability == "market.daily.qfq" else "b") * 64,
        conflict_fields=[],
        adjustment=adjustment,
        price_unit="CNY",
        volume_unit="share",
        row_count=len(sessions),
        trusted=True,
        persisted=True,
    )
    session.add(quality)
    session.flush()
    for index, day in enumerate(sessions):
        close = offset + Decimal(index) / Decimal("100")
        session.add(
            MarketDailyBar(
                symbol=symbol,
                trade_date=day,
                open=close - Decimal("0.03"),
                high=close + Decimal("0.08"),
                low=close - Decimal("0.08"),
                close=close,
                volume=Decimal("1000000") + index,
                adjustment=adjustment,
                price_unit="CNY",
                volume_unit="share",
                observed_at=to_market_storage_naive(
                    get_trading_calendar().session_close_at(day)
                ),
                quality_status="SINGLE_SOURCE",
                quality_record_id=quality.id,
                source=f"fixture-{symbol}",
                fetched_at=observed,
            )
        )
    session.commit()
    return sessions


def test_batch_replay_uses_persisted_data_and_is_deterministic(session):
    sessions = _seed_series(
        session,
        symbol="300308",
        capability="market.daily.qfq",
        adjustment="qfq",
        offset=Decimal("10"),
    )
    _seed_series(
        session,
        symbol="CSI000300",
        capability="market.index_daily",
        adjustment="unadjusted",
        offset=Decimal("4000"),
    )
    request = BatchReplayRequest(
        symbol="300308",
        start_date=sessions[120],
        end_date=sessions[-21],
        sampling_frequency="WEEKLY",
    )
    first = SelectedStockReplayService(session).run(request)
    second = SelectedStockReplayService(session).run(request)

    assert first == second
    assert first.replay_digest == second.replay_digest
    assert first.analysis_sample_count >= 30
    assert first.sample_status == "SAMPLE_INSUFFICIENT"
    assert set(first.horizon_returns) == {"5", "10", "20"}
    assert set(first.strategy_comparisons) == {
        "CSV_V2_BASELINE",
        "CSV_V2_SURVIVAL",
        "PRODUCT_V1_SHADOW",
    }
    assert set(first.discipline_rule_outcomes) == {
        "TREND_POSITION_COORDINATION",
        "CHASE_RISK",
        "HIGH_VOLUME_STALL",
        "VOLUME_DECLINE_BREAKDOWN",
        "INDUSTRY_ROLE_RISK",
        "HARD_STOP_PRIORITY",
    }


def test_batch_replay_refuses_missing_persisted_benchmark(session):
    sessions = _seed_series(
        session,
        symbol="300308",
        capability="market.daily.qfq",
        adjustment="qfq",
        offset=Decimal("10"),
    )
    request = BatchReplayRequest(
        symbol="300308",
        start_date=sessions[120],
        end_date=sessions[-21],
    )
    try:
        SelectedStockReplayService(session).run(request)
    except ValueError as exc:
        assert "persisted stock and CSI300 histories" in str(exc)
    else:
        raise AssertionError("missing benchmark must block replay")
