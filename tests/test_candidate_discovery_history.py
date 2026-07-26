from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.data_hub.market_subjects import stock_daily_subject, stock_turnover_subject
from app.data_hub.trading_calendar import to_market_storage_naive
from app.discovery.contracts import CandidateStockInput, PriceHistoryPoint
from app.discovery.service import CandidateDiscoveryService, _neutral_member_sort_key
from app.models import (
    DataQualityRecord,
    IndustryConstituentSnapshot,
    MarketDailyBar,
    MarketTurnoverSnapshot,
)


DAY = date(2026, 7, 24)
NOW = datetime(2026, 7, 24, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _sessions_ending(day: date, count: int) -> list[date]:
    sessions = []
    current = day
    while len(sessions) < count:
        if current.weekday() < 5:
            sessions.append(current)
        current -= timedelta(days=1)
    return sorted(sessions)


def _quality(session, *, capability, subject, row_count):
    stored_now = to_market_storage_naive(NOW)
    record = DataQualityRecord(
        symbol=subject.subject_id if subject.subject_type == "stock" else None,
        capability=capability,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        semantic_key=subject.semantic_key,
        quality_status="SINGLE_SOURCE",
        observed_at=stored_now,
        fetched_at=stored_now,
        provider_id="candidate-history-fixture",
        provider_observations=[],
        normalized_digest="a" * 64,
        conflict_fields=[],
        row_count=row_count,
        trusted=True,
        persisted=True,
    )
    session.add(record)
    session.flush()
    return record


def _member(*, symbol="300001", weight="1", change_pct="0"):
    stored_now = to_market_storage_naive(NOW)
    return IndustryConstituentSnapshot(
        industry_key="BK0001",
        industry_name="通信设备",
        symbol=symbol,
        name=f"测试{symbol}",
        weight=Decimal(weight),
        change_pct=Decimal(change_pct),
        latest_price=Decimal("10"),
        high_52w=Decimal("12"),
        is_new_high=False,
        snapshot_date=DAY,
        observed_at=stored_now,
        source="fixture",
        fetched_at=stored_now,
        quality_record_id=1,
    )


def _seed_history(session, *, daily_end=DAY, turnover_end=DAY, count=50):
    daily_subject = stock_daily_subject("300001", "qfq", "CNY", "share")
    turnover_subject = stock_turnover_subject("300001")
    daily_quality = _quality(
        session,
        capability="market.daily.qfq",
        subject=daily_subject,
        row_count=count,
    )
    turnover_quality = _quality(
        session,
        capability="market.turnover.daily",
        subject=turnover_subject,
        row_count=count,
    )
    stored_now = to_market_storage_naive(NOW)
    for trade_day in _sessions_ending(daily_end, count):
        session.add(
            MarketDailyBar(
                symbol="300001",
                trade_date=trade_day,
                open=Decimal("10"),
                high=Decimal("10.2"),
                low=Decimal("9.8"),
                close=Decimal("10"),
                volume=Decimal("1000000"),
                adjustment="qfq",
                price_unit="CNY",
                volume_unit="share",
                observed_at=stored_now,
                quality_status="SINGLE_SOURCE",
                quality_record_id=daily_quality.id,
                source="fixture",
                fetched_at=stored_now,
            )
        )
    for trade_day in _sessions_ending(turnover_end, count):
        session.add(
            MarketTurnoverSnapshot(
                symbol="300001",
                trade_date=trade_day,
                turnover_rate=Decimal("3"),
                amount=Decimal("200000000"),
                observed_at=stored_now,
                source="fixture",
                fetched_at=stored_now,
                quality_record_id=turnover_quality.id,
            )
        )
    session.commit()


def _assess(session):
    return CandidateDiscoveryService(session, router=object())._stock_input(
        _member(),
        limit_up_symbols=set(),
        trade_date=DAY,
        minimum_history_rows=50,
        now=NOW,
    )


def test_current_aligned_stock_history_is_ready(session):
    _seed_history(session)
    assessment = _assess(session)
    assert assessment.reason is None
    assert assessment.suspended is False
    assert assessment.stock is not None
    assert assessment.stock.prices[-1].trade_date == DAY


def test_missing_current_daily_bar_is_suspended_and_not_ready(session):
    _seed_history(session, daily_end=date(2026, 7, 23))
    assessment = _assess(session)
    assert assessment.stock is None
    assert assessment.reason == "DATA_NOT_CURRENT"
    assert assessment.suspended is True


def test_missing_current_turnover_is_data_not_current(session):
    _seed_history(session, turnover_end=date(2026, 7, 23))
    assessment = _assess(session)
    assert assessment.stock is None
    assert assessment.reason == "DATA_NOT_CURRENT"


def test_duplicate_daily_date_does_not_inflate_aligned_history(session):
    _seed_history(session)
    existing = session.query(MarketDailyBar).order_by(MarketDailyBar.trade_date).first()
    session.add(
        MarketDailyBar(
            symbol=existing.symbol,
            trade_date=existing.trade_date,
            open=existing.open,
            high=existing.high,
            low=existing.low,
            close=existing.close,
            volume=existing.volume,
            adjustment=existing.adjustment,
            price_unit=existing.price_unit,
            volume_unit=existing.volume_unit,
            observed_at=existing.observed_at,
            quality_status=existing.quality_status,
            quality_record_id=existing.quality_record_id,
            source="duplicate-source",
            fetched_at=existing.fetched_at,
        )
    )
    session.commit()
    assessment = _assess(session)
    assert assessment.stock is None
    assert assessment.reason == "DUPLICATE_HISTORY_DATE"


def test_neutral_prescreen_ignores_daily_change_pct():
    points = tuple(
        PriceHistoryPoint(
            trade_date=trade_day,
            close=Decimal("10"),
            amount=Decimal("200000000"),
            turnover_rate=Decimal("3"),
        )
        for trade_day in _sessions_ending(DAY, 50)
    )

    def stock(member):
        return CandidateStockInput(
            symbol=member.symbol,
            name=member.name,
            industry_key=member.industry_key,
            industry_name=member.industry_name,
            prices=points,
            is_st=False,
            suspended=False,
            limit_up=False,
            quality_status="SINGLE_SOURCE",
            evidence_references=("quality:1",),
        )

    high_weight = _member(symbol="300002", weight="2", change_pct="-8")
    low_weight = _member(symbol="300001", weight="1", change_pct="9")
    ordered = sorted(
        [(low_weight, stock(low_weight)), (high_weight, stock(high_weight))],
        key=_neutral_member_sort_key,
    )
    assert [member.symbol for member, _ in ordered] == ["300002", "300001"]
