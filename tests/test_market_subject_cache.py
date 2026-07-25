from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.data_hub.contracts import (
    DailyBar,
    DataProvider,
    ProviderMetadata,
    ProviderUnavailableError,
    Quote,
)
from app.data_hub.market_subjects import (
    index_daily_subject,
    sector_daily_subject,
    stock_daily_subject,
    stock_quote_subject,
)
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter, request_fingerprint
from app.data_hub.trading_calendar import (
    get_trading_calendar,
    shanghai_now,
    shanghai_today,
)
from app.domain.quality import DataQualityStatus
from app.domain.quality_subject import SubjectRef, canonical_semantic_key
from app.models import (
    Account,
    CompanyProfile,
    DataQualityRecord,
    DataQualitySubjectHead,
    Holding,
    MarketDailyBar,
    MarketQuote,
    TechnicalSnapshot,
)
from app.services.one_click_pipeline import (
    _cached_quote_step,
    _market_assessment,
    _sector_assessment,
    _sync_stock,
)
from app.services.market_cache import (
    canonical_series_row,
    mapping_series_bars,
    replace_market_series,
    resolve_cached_quote,
    resolve_cached_series,
    validate_series_for_persistence,
)
from app.services.technical_snapshots import load_qfq_frame, snapshot_all_holdings


class MarketStub(DataProvider):
    def __init__(
        self,
        provider_id: str,
        *,
        priority: int = 10,
        quote_price: str = "10.00",
        quote_type: str = "realtime",
        daily_close: str = "10.00",
        adjustment: str = "qfq",
        row_count: int = 3,
        series_row_count: int = 20,
        observed_at: datetime | None = None,
        failures: set[str] | None = None,
    ):
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=(
                "market.quote.realtime",
                "market.quote.latest_close",
                "market.daily.qfq",
                "market.daily.unadjusted",
                "market.index_daily",
                "market.sector_daily",
            ),
            priority=priority,
            realtime_supported=True,
        )
        self.quote_price = Decimal(quote_price)
        self.quote_type = quote_type
        self.daily_close = Decimal(daily_close)
        self.adjustment = adjustment
        self.row_count = row_count
        self.series_row_count = series_row_count
        self.observed_at = observed_at
        self.failures = failures or set()

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def _fail_if_requested(self, operation: str) -> None:
        if operation in self.failures:
            raise ProviderUnavailableError(f"{operation} unavailable")

    def get_quote(self, symbol: str) -> Quote:
        self._fail_if_requested("get_quote")
        now = self.observed_at or shanghai_now()
        if self.quote_type == "latest_close" and self.observed_at is None:
            calendar = get_trading_calendar()
            now = calendar.session_close_at(calendar.latest_completed_session())
        return Quote(
            symbol=symbol,
            name=f"stock-{symbol}",
            price=self.quote_price,
            quote_type=self.quote_type,
            observed_at=now,
            price_unit="CNY",
            source=self.provider_id,
            source_api="quote",
            fetched_at=shanghai_now(),
        )

    def get_quote_alias(self, symbol: str) -> Quote:
        return self.get_quote(symbol)

    def get_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        self._fail_if_requested("get_history")
        calendar = get_trading_calendar()
        fetched_at = shanghai_now()
        latest = min(end, calendar.latest_completed_session(fetched_at))
        trade_dates = []
        candidate = latest
        while len(trade_dates) < self.row_count:
            try:
                calendar.session_close_at(candidate)
            except ValueError:
                candidate -= timedelta(days=1)
                continue
            trade_dates.append(candidate)
            candidate -= timedelta(days=1)
        trade_dates.reverse()
        rows = []
        for trade_date in trade_dates:
            rows.append(
                DailyBar(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=self.daily_close,
                    high=self.daily_close + Decimal("0.10"),
                    low=self.daily_close - Decimal("0.10"),
                    close=self.daily_close,
                    volume=Decimal("10000"),
                    adjustment=self.adjustment,
                    price_unit="CNY",
                    volume_unit="share",
                    observed_at=calendar.session_close_at(trade_date),
                    source=self.provider_id,
                    fetched_at=fetched_at,
                )
            )
        return rows

    def get_index_history(self, symbol: str, start: date, end: date) -> dict:
        self._fail_if_requested("get_index_history")
        return self._series_payload(end)

    def get_sector_history(self, industry: str, start: date, end: date) -> dict:
        self._fail_if_requested("get_sector_history")
        return self._series_payload(end)

    def _series_payload(self, end: date) -> dict:
        calendar = get_trading_calendar()
        fetched_at = shanghai_now()
        latest = min(end, calendar.latest_completed_session(fetched_at))
        trade_dates = []
        candidate = latest
        while len(trade_dates) < self.series_row_count:
            try:
                calendar.session_close_at(candidate)
            except ValueError:
                candidate -= timedelta(days=1)
                continue
            trade_dates.append(candidate)
            candidate -= timedelta(days=1)
        trade_dates.reverse()
        return {
            "rows": [
                {
                    "date": trade_date,
                    "close": self.daily_close,
                    "volume": Decimal("10000"),
                }
                for trade_date in trade_dates
            ],
            "source": self.provider_id,
            "fetched_at": fetched_at,
        }


def _router(session, *providers: MarketStub) -> DataHubRouter:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return DataHubRouter(session, registry)


def _quality_record(session, result) -> DataQualityRecord:
    return session.get(DataQualityRecord, result.quality_record_id)


def _persist_quote(session, router: DataHubRouter, result) -> MarketQuote:
    quote = result.require_value()
    stored = MarketQuote(
        symbol=quote.symbol,
        name=quote.name,
        price=quote.price,
        quote_type=quote.quote_type,
        observed_at=quote.observed_at,
        price_unit=quote.price_unit,
        quality_status=result.quality_status.value,
        quality_record_id=result.quality_record_id,
        source=quote.source,
        source_api=quote.source_api,
        fetched_at=quote.fetched_at,
    )
    session.add(stored)
    session.flush()
    router.mark_persisted(result)
    session.commit()
    return stored


def _persist_daily(
    session,
    router: DataHubRouter,
    result,
    *,
    source: str | None = None,
    row_count: int | None = None,
    duplicate_date: bool = False,
    mixed_sources: bool = False,
) -> list[MarketDailyBar]:
    rows = list(result.require_value())
    if row_count is not None:
        rows = rows[:row_count]
    stored = []
    for index, bar in enumerate(rows):
        trade_date = rows[0].trade_date if duplicate_date and index == 1 else bar.trade_date
        row_source = (
            f"{source or bar.source}-{index}"
            if mixed_sources or (duplicate_date and index == 1)
            else source or bar.source
        )
        item = MarketDailyBar(
            symbol=bar.symbol,
            trade_date=trade_date,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            adjustment=bar.adjustment,
            price_unit=bar.price_unit,
            volume_unit=bar.volume_unit,
            observed_at=bar.observed_at,
            quality_status=result.quality_status.value,
            quality_record_id=result.quality_record_id,
            source=row_source,
            fetched_at=bar.fetched_at,
        )
        session.add(item)
        stored.append(item)
    session.flush()
    router.mark_persisted(result)
    session.commit()
    return stored


def _persist_mapping_series(
    session,
    router: DataHubRouter,
    result,
    *,
    cache_symbol: str,
    adjustment: str,
) -> list[MarketDailyBar]:
    payload = result.require_value()
    calendar = get_trading_calendar()
    stored = []
    for row in payload["rows"]:
        close = Decimal(str(row["close"]))
        item = MarketDailyBar(
            symbol=cache_symbol,
            trade_date=row["date"],
            open=close,
            high=close,
            low=close,
            close=close,
            volume=Decimal(str(row["volume"])),
            adjustment=adjustment,
            price_unit="CNY",
            volume_unit="share",
            observed_at=calendar.session_close_at(row["date"]),
            quality_status=result.quality_status.value,
            quality_record_id=result.quality_record_id,
            source=payload["source"],
            fetched_at=payload["fetched_at"],
        )
        session.add(item)
        stored.append(item)
    session.flush()
    router.mark_persisted(result)
    session.commit()
    return stored


def _series_selection(session, *, symbol: str = "300502", min_rows: int = 3):
    subject = stock_daily_subject(symbol, "qfq", "CNY", "share")
    return resolve_cached_series(
        session,
        cache_symbol=symbol,
        capability="market.daily.qfq",
        subject=subject,
        adjustment="qfq",
        price_unit="CNY",
        volume_unit="share",
        min_rows=min_rows,
    )


def test_stock_quote_subject_is_stable():
    first = stock_quote_subject(" 300502 ", "realtime", "cny")
    second = stock_quote_subject("300502", "REALTIME", "CNY")
    assert first == second
    assert first.stable_key == ("stock", "300502", "realtime/CNY")


def test_stock_qfq_subject_is_stable():
    assert stock_daily_subject("300502", "QFQ", "cny", "SHARE") == SubjectRef(
        subject_type="stock",
        subject_id="300502",
        semantic_key="qfq/CNY/share",
    )


def test_index_subject_normalizes_csi000300():
    assert index_daily_subject(
        "csi000300", "unadjusted", "CNY", "share"
    ).stable_key == ("index", "CSI000300", "unadjusted/CNY/share")


def test_sector_subject_is_deterministic_and_length_safe():
    first = sector_daily_subject(" 通信设备 ", "unadjusted", "CNY", "share")
    second = sector_daily_subject("通信设备", "unadjusted", "CNY", "share")
    assert first == second
    assert first.subject_type == "sector"
    assert len(first.subject_id) == 12
    assert first.subject_id.startswith("S")


def test_different_sector_names_have_distinct_subjects():
    first = sector_daily_subject("通信设备", "unadjusted", "CNY", "share")
    second = sector_daily_subject("半导体", "unadjusted", "CNY", "share")
    assert first.subject_id != second.subject_id


def test_market_semantic_keys_are_canonical():
    subjects = [
        stock_quote_subject("300502", " REALTIME ", " cny "),
        stock_daily_subject("300502", " QFQ ", " cny ", " SHARE "),
        index_daily_subject("CSI000300", " UNADJUSTED ", " cny ", " SHARE "),
        sector_daily_subject("通信设备", " UNADJUSTED ", " cny ", " SHARE "),
    ]
    assert [item.semantic_key for item in subjects] == [
        "realtime/CNY",
        "qfq/CNY/share",
        "unadjusted/CNY/share",
        "unadjusted/CNY/share",
    ]
    assert all(
        canonical_semantic_key(item.semantic_key) == item.semantic_key
        for item in subjects
    )


def test_market_router_rejects_missing_subject(session):
    router = _router(session, MarketStub("a"))
    with pytest.raises(ProviderUnavailableError, match="subject"):
        router.invoke("market.quote.realtime", "get_quote", "300502")
    assert session.scalar(select(func.count(DataQualityRecord.id))) == 0


def test_quote_quality_record_has_stock_subject(session):
    result = _router(session, MarketStub("a")).get_quote("300502")
    record = _quality_record(session, result)
    assert (record.subject_type, record.subject_id, record.semantic_key) == (
        "stock",
        "300502",
        "realtime/CNY",
    )


def test_qfq_quality_record_has_stock_subject(session):
    result = _router(session, MarketStub("a")).get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    record = _quality_record(session, result)
    assert (record.subject_type, record.subject_id, record.semantic_key) == (
        "stock",
        "300502",
        "qfq/CNY/share",
    )


def test_index_quality_record_has_index_subject(session):
    result = _router(session, MarketStub("a")).get_index_history(
        "csi000300", date.today() - timedelta(days=30), date.today()
    )
    record = _quality_record(session, result)
    assert (record.subject_type, record.subject_id) == ("index", "CSI000300")


def test_sector_quality_record_has_stable_sector_subject(session):
    result = _router(session, MarketStub("a")).get_sector_history(
        "通信设备", date.today() - timedelta(days=30), date.today()
    )
    record = _quality_record(session, result)
    assert record.subject_type == "sector"
    assert record.subject_id == result.subject.subject_id
    assert len(record.subject_id) == 12


def test_provider_result_exposes_operation_and_subject(session):
    result = _router(session, MarketStub("a")).get_quote("300502")
    assert result.operation == "get_quote"
    assert result.subject == stock_quote_subject("300502", "realtime", "CNY")
    assert result.public_meta()["subject_id"] == "300502"
    assert result.public_meta()["operation"] == "get_quote"


def test_market_call_results_do_not_overwrite_other_subjects(session):
    router = _router(session, MarketStub("a"))
    first = router.get_quote("300502")
    second = router.get_quote("600000")
    assert router.result_for(
        "market.quote.realtime",
        "get_quote",
        first.subject,
        first.request_fingerprint,
    ) is first
    assert router.result_for(
        "market.quote.realtime",
        "get_quote",
        second.subject,
        second.request_fingerprint,
    ) is second


def test_market_call_results_do_not_overwrite_other_operations(session):
    router = _router(session, MarketStub("a"))
    subject = stock_quote_subject("300502", "realtime", "CNY")
    first = router.invoke(
        "market.quote.realtime",
        "get_quote",
        "300502",
        symbol="300502",
        subject=subject,
    )
    second = router.invoke(
        "market.quote.realtime",
        "get_quote_alias",
        "300502",
        symbol="300502",
        subject=subject,
    )
    assert router.result_for(
        "market.quote.realtime",
        "get_quote",
        subject,
        first.request_fingerprint,
    ) is first
    assert (
        router.result_for(
            "market.quote.realtime",
            "get_quote_alias",
            subject,
            second.request_fingerprint,
        )
        is second
    )


def test_first_quality_signal_creates_subject_head(session):
    result = _router(session, MarketStub("a")).get_quote("300502")
    head = session.scalar(select(DataQualitySubjectHead))
    assert head.current_record_id == result.quality_record_id
    assert head.generation == 1


def test_later_signal_advances_generation(session):
    router = _router(session, MarketStub("a"))
    first = router.get_quote("300502")
    second = router.get_quote("300502")
    head = session.scalar(select(DataQualitySubjectHead))
    assert head.current_record_id == second.quality_record_id
    assert head.current_record_id != first.quality_record_id
    assert head.generation == 2


def test_conflicted_signal_becomes_current_head(session):
    result = _router(
        session,
        MarketStub("a", quote_price="10.00"),
        MarketStub("b", priority=20, quote_price="11.00"),
    ).get_quote("300502")
    head = session.scalar(select(DataQualitySubjectHead))
    assert result.quality_status == DataQualityStatus.CONFLICTED
    assert head.current_record_id == result.quality_record_id


def test_missing_attempt_becomes_current_head_without_deleting_cache(session):
    provider = MarketStub("a")
    router = _router(session, provider)
    cached_result = router.get_quote("300502")
    cached = _persist_quote(session, router, cached_result)
    provider.failures.add("get_quote")
    missing = router.get_quote("300502")
    session.commit()
    head = session.scalar(select(DataQualitySubjectHead))
    assert missing.quality_status == DataQualityStatus.MISSING
    assert head.current_record_id == missing.quality_record_id
    assert session.get(MarketQuote, cached.id) is not None


def test_head_update_rolls_back_with_transaction(session):
    _router(session, MarketStub("a")).get_quote("300502")
    assert session.scalar(select(DataQualitySubjectHead)) is not None
    session.rollback()
    assert session.scalar(select(DataQualitySubjectHead)) is None
    assert session.scalar(select(DataQualityRecord)) is None


def test_canonical_scope_cannot_create_duplicate_heads(session):
    router = _router(session, MarketStub("a"))
    subject = stock_quote_subject("300502", "realtime", "CNY")
    router.invoke(
        "market.quote.realtime",
        "get_quote",
        "300502",
        symbol="300502",
        subject=subject,
    )
    router.invoke(
        "market.quote.realtime",
        "get_quote_alias",
        "300502",
        symbol="300502",
        subject=SubjectRef(
            subject_type="stock",
            subject_id="300502",
            semantic_key=" realtime/CNY ",
        ),
    )
    assert session.scalar(select(func.count(DataQualitySubjectHead.id))) == 1
    assert session.scalar(select(DataQualitySubjectHead.generation)) == 2


def _conflict_then_refresh(
    session,
    *,
    conflict_time: datetime,
    refresh_time: datetime,
    refresh_sources: int = 2,
    refresh_subject: SubjectRef | None = None,
):
    conflict_router = _router(
        session,
        MarketStub("conflict-a", quote_price="10.00", observed_at=conflict_time),
        MarketStub(
            "conflict-b",
            priority=20,
            quote_price="11.00",
            observed_at=conflict_time,
        ),
    )
    conflict = conflict_router.get_quote("300502")
    refresh_providers = [
        MarketStub("refresh-a", quote_price="10.00", observed_at=refresh_time)
    ]
    if refresh_sources == 2:
        refresh_providers.append(
            MarketStub(
                "refresh-b",
                priority=20,
                quote_price="10.00",
                observed_at=refresh_time,
            )
        )
    refresh_router = _router(session, *refresh_providers)
    subject = refresh_subject or stock_quote_subject("300502", "realtime", "CNY")
    refresh = refresh_router.invoke(
        "market.quote.realtime",
        "get_quote",
        subject.subject_id,
        symbol=subject.subject_id,
        subject=subject,
    )
    return _quality_record(session, conflict), _quality_record(session, refresh)


def test_verified_refresh_supersedes_same_scope_conflict(session):
    now = shanghai_now()
    conflict, refresh = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=2),
        refresh_time=now - timedelta(minutes=1),
    )
    assert refresh.quality_status == "VERIFIED"
    assert refresh.supersedes_record_id == conflict.id


def test_verified_provider_fallback_supersedes_same_scope_conflict(session):
    now = shanghai_now()
    conflict, _ = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=3),
        refresh_time=now - timedelta(minutes=2),
        refresh_sources=1,
    )
    refresh_router = _router(
        session,
        MarketStub("offline", priority=5, failures={"get_quote"}),
        MarketStub("refresh-a", priority=10, observed_at=now - timedelta(minutes=1)),
        MarketStub("refresh-b", priority=20, observed_at=now - timedelta(minutes=1)),
    )
    refresh = refresh_router.get_quote("300502")
    record = _quality_record(session, refresh)
    assert refresh.quality_status == DataQualityStatus.VERIFIED
    assert refresh.fallback_used is True
    assert refresh.cache_used is False
    assert record.supersedes_record_id == conflict.id


def test_stale_resolution_does_not_hide_conflict_from_fresh_refresh(session):
    now = shanghai_now()
    conflict, stale_resolution = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=3),
        refresh_time=now - timedelta(minutes=2),
    )
    assert stale_resolution.supersedes_record_id == conflict.id
    conflict.observed_at = now - timedelta(minutes=61)
    stale_resolution.observed_at = now - timedelta(minutes=60)
    session.flush()

    refresh = _router(
        session,
        MarketStub("refresh-a", observed_at=now - timedelta(minutes=1)),
        MarketStub(
            "refresh-b",
            priority=20,
            observed_at=now - timedelta(minutes=1),
        ),
    ).get_quote("300502")
    record = _quality_record(session, refresh)
    assert refresh.quality_status == DataQualityStatus.VERIFIED
    assert record.supersedes_record_id == conflict.id


def test_single_source_refresh_does_not_supersede_conflict(session):
    now = shanghai_now()
    _, refresh = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=2),
        refresh_time=now - timedelta(minutes=1),
        refresh_sources=1,
    )
    assert refresh.quality_status == "SINGLE_SOURCE"
    assert refresh.supersedes_record_id is None


def test_backdated_verified_refresh_does_not_supersede(session):
    now = shanghai_now()
    _, refresh = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=1),
        refresh_time=now - timedelta(minutes=2),
    )
    assert refresh.quality_status == "VERIFIED"
    assert refresh.supersedes_record_id is None


def test_future_verified_refresh_does_not_supersede(session):
    now = shanghai_now()
    _, refresh = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=1),
        refresh_time=now + timedelta(minutes=1),
    )
    assert refresh.quality_status == "MISSING"
    assert refresh.supersedes_record_id is None


def test_different_semantic_key_does_not_supersede(session):
    now = shanghai_now()
    _, refresh = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=2),
        refresh_time=now - timedelta(minutes=1),
        refresh_subject=SubjectRef(
            subject_type="stock",
            subject_id="300502",
            semantic_key="latest_close/CNY",
        ),
    )
    assert refresh.supersedes_record_id is None


def test_different_subject_does_not_supersede(session):
    now = datetime.now()
    _, refresh = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=2),
        refresh_time=now - timedelta(minutes=1),
        refresh_subject=stock_quote_subject("600000", "realtime", "CNY"),
    )
    assert refresh.supersedes_record_id is None


def test_mark_persisted_rejects_mismatched_lineage(session):
    router = _router(session, MarketStub("a"))
    result = router.get_quote("300502")
    mismatches = [
        replace(result, capability="market.quote.latest_close"),
        replace(
            result,
            subject=stock_quote_subject("600000", "realtime", "CNY"),
        ),
        replace(
            result,
            subject=stock_quote_subject("300502", "latest_close", "CNY"),
        ),
        replace(result, quality_record_id=None),
    ]
    for mismatch in mismatches:
        with pytest.raises(ProviderUnavailableError):
            router.mark_persisted(mismatch)


def test_cached_realtime_quote_ages_to_stale(session):
    router = _router(session, MarketStub("a"))
    result = router.get_quote("300502")
    quote = _persist_quote(session, router, result)
    selected = resolve_cached_quote(
        session,
        symbol="300502",
        capability="market.quote.realtime",
        evaluated_at=quote.observed_at + timedelta(minutes=31),
    )
    assert selected.effective_quality.effective_quality == DataQualityStatus.STALE
    assert selected.executable is False


def test_cached_latest_close_cannot_satisfy_realtime_request(session):
    router = _router(session, MarketStub("a", quote_type="latest_close"))
    result = router.get_latest_close("300502")
    _persist_quote(session, router, result)
    selected = resolve_cached_quote(
        session,
        symbol="300502",
        capability="market.quote.realtime",
    )
    assert selected.value is None
    assert selected.effective_quality.effective_quality == DataQualityStatus.MISSING


def test_new_quote_conflict_blocks_old_quote_cache(session):
    router = _router(session, MarketStub("cached"))
    result = router.get_quote("300502")
    _persist_quote(session, router, result)
    _router(
        session,
        MarketStub("a", quote_price="10.00"),
        MarketStub("b", priority=20, quote_price="11.00"),
    ).get_quote("300502")
    session.commit()
    selected = resolve_cached_quote(
        session,
        symbol="300502",
        capability="market.quote.realtime",
    )
    assert selected.effective_quality.effective_quality == DataQualityStatus.CONFLICTED
    assert selected.executable is False


def test_legacy_quote_without_lineage_is_missing(session):
    now = datetime.now()
    session.add(
        MarketQuote(
            symbol="300502",
            name="legacy",
            price=Decimal("10"),
            quote_type="realtime",
            observed_at=now,
            price_unit="CNY",
            quality_status="SINGLE_SOURCE",
            quality_record_id=None,
            source="legacy",
            source_api="legacy",
            fetched_at=now,
        )
    )
    session.commit()
    selected = resolve_cached_quote(
        session,
        symbol="300502",
        capability="market.quote.realtime",
    )
    assert selected.effective_quality.effective_quality == DataQualityStatus.MISSING
    assert selected.executable is False


def test_series_selector_uses_one_quality_record(session):
    first_router = _router(session, MarketStub("older", daily_close="10"))
    first = first_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, first_router, first)
    second_router = _router(session, MarketStub("newer", daily_close="11"))
    second = second_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, second_router, second)
    selected = _series_selection(session)
    assert selected.quality_record_id == second.quality_record_id
    assert {item.quality_record_id for item in selected.bars} == {
        second.quality_record_id
    }


def test_series_selector_never_mixes_providers(session):
    valid_router = _router(session, MarketStub("valid"))
    valid = valid_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, valid_router, valid)
    mixed_router = _router(session, MarketStub("mixed"))
    mixed = mixed_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, mixed_router, mixed, mixed_sources=True)
    selected = _series_selection(session)
    assert selected.quality_record_id == valid.quality_record_id
    assert {item.source for item in selected.bars} == {"valid"}


def test_duplicate_trade_dates_in_lineage_are_rejected(session):
    valid_router = _router(session, MarketStub("valid"))
    valid = valid_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, valid_router, valid)
    invalid_router = _router(session, MarketStub("duplicate"))
    invalid = invalid_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, invalid_router, invalid, duplicate_date=True)
    selected = _series_selection(session)
    assert selected.quality_record_id == valid.quality_record_id


def test_latest_complete_executable_series_is_selected(session):
    older_router = _router(session, MarketStub("older"))
    older = older_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, older_router, older)
    latest_router = _router(session, MarketStub("latest", daily_close="12"))
    latest = latest_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, latest_router, latest)
    selected = _series_selection(session)
    assert selected.quality_record_id == latest.quality_record_id
    assert selected.bars[-1].close == Decimal("12.0000")


def test_incomplete_new_series_does_not_replace_complete_series(session):
    complete_router = _router(session, MarketStub("complete"))
    complete = complete_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, complete_router, complete)
    incomplete_router = _router(session, MarketStub("incomplete"))
    incomplete = incomplete_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, incomplete_router, incomplete, row_count=1)
    selected = _series_selection(session)
    assert selected.quality_record_id == complete.quality_record_id
    assert len(selected.bars) == 3


def test_new_conflict_blocks_old_complete_series(session):
    cached_router = _router(session, MarketStub("cached"))
    cached = cached_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, cached_router, cached)
    _router(
        session,
        MarketStub("a", daily_close="10"),
        MarketStub("b", priority=20, daily_close="11"),
    ).get_history("300502", date.today() - timedelta(days=3), date.today())
    session.commit()
    selected = _series_selection(session)
    assert selected.bars == []
    assert selected.effective_quality.effective_quality == DataQualityStatus.CONFLICTED


def test_unrelated_subject_conflict_does_not_block_series(session):
    cached_router = _router(session, MarketStub("cached"))
    cached = cached_router.get_history(
        "300502", date.today() - timedelta(days=3), date.today()
    )
    _persist_daily(session, cached_router, cached)
    _router(
        session,
        MarketStub("a", daily_close="10"),
        MarketStub("b", priority=20, daily_close="11"),
    ).get_history("600000", date.today() - timedelta(days=3), date.today())
    session.commit()
    selected = _series_selection(session)
    assert selected.quality_record_id == cached.quality_record_id
    assert selected.executable is True


def test_legacy_series_without_lineage_is_missing(session):
    now = datetime.now()
    for offset in range(3):
        session.add(
            MarketDailyBar(
                symbol="300502",
                trade_date=date.today() - timedelta(days=2 - offset),
                open=10,
                high=11,
                low=9,
                close=10,
                volume=100,
                adjustment="qfq",
                price_unit="CNY",
                volume_unit="share",
                observed_at=now,
                quality_status="SINGLE_SOURCE",
                quality_record_id=None,
                source="legacy",
                fetched_at=now,
            )
        )
    session.commit()
    selected = _series_selection(session)
    assert selected.bars == []
    assert selected.effective_quality.effective_quality == DataQualityStatus.MISSING


def test_index_cache_uses_index_subject(session):
    router = _router(session, MarketStub("index"))
    result = router.get_index_history(
        "csi000300", date.today() - timedelta(days=30), date.today()
    )
    subject = index_daily_subject(
        "csi000300", "unadjusted", "CNY", "share"
    )
    _persist_mapping_series(
        session,
        router,
        result,
        cache_symbol=subject.subject_id,
        adjustment="unadjusted",
    )
    selected = resolve_cached_series(
        session,
        cache_symbol=subject.subject_id,
        capability="market.index_daily",
        subject=subject,
        adjustment="unadjusted",
        price_unit="CNY",
        volume_unit="share",
        min_rows=20,
    )
    assert selected.subject == subject
    assert selected.quality_record_id == result.quality_record_id


def test_sector_cache_uses_stable_sector_subject(session):
    router = _router(session, MarketStub("sector"))
    result = router.get_sector_history(
        "通信设备", date.today() - timedelta(days=30), date.today()
    )
    subject = sector_daily_subject(
        "通信设备", "unadjusted", "CNY", "share"
    )
    _persist_mapping_series(
        session,
        router,
        result,
        cache_symbol=subject.subject_id,
        adjustment="unadjusted",
    )
    selected = resolve_cached_series(
        session,
        cache_symbol=subject.subject_id,
        capability="market.sector_daily",
        subject=subject,
        adjustment="unadjusted",
        price_unit="CNY",
        volume_unit="share",
        min_rows=20,
    )
    assert selected.subject == subject
    assert len(subject.subject_id) == 12
    assert selected.quality_record_id == result.quality_record_id


def test_same_stock_history_windows_do_not_overwrite_call_results(session):
    router = _router(session, MarketStub("a"))
    subject = stock_daily_subject("300502", "qfq", "CNY", "share")
    first = router.get_history("300502", date(2026, 7, 1), date(2026, 7, 10))
    second = router.get_history("300502", date(2026, 7, 11), date(2026, 7, 20))

    assert first.request_fingerprint != second.request_fingerprint
    assert router.result_for(
        "market.daily.qfq",
        "get_history",
        subject,
        first.request_fingerprint,
    ) is first
    assert router.result_for(
        "market.daily.qfq",
        "get_history",
        subject,
        second.request_fingerprint,
    ) is second


def test_identical_requests_generate_identical_fingerprints(session):
    router = _router(session, MarketStub("a"))
    first = router.get_history("300502", date(2026, 7, 1), date(2026, 7, 10))
    second = router.get_history("300502", date(2026, 7, 1), date(2026, 7, 10))
    assert first.request_fingerprint == second.request_fingerprint


def test_request_fingerprint_ignores_dict_order():
    first = request_fingerprint(
        capability="market.test",
        operation="fetch",
        args=(),
        kwargs={"filters": {"symbol": " 300502 ", "limit": 10}},
    )
    second = request_fingerprint(
        capability="market.test",
        operation="fetch",
        args=(),
        kwargs={"filters": {"limit": 10, "symbol": "300502"}},
    )
    assert first == second


def test_request_fingerprint_normalizes_dates_datetimes_and_decimals():
    utc = datetime(2026, 7, 24, 4, 0, tzinfo=timezone.utc)
    cst = datetime.fromisoformat("2026-07-24T12:00:00+08:00")
    first = request_fingerprint(
        capability="market.test",
        operation="fetch",
        args=(date(2026, 7, 24), utc, Decimal("10.00")),
        kwargs={},
    )
    second = request_fingerprint(
        capability="market.test",
        operation="fetch",
        args=(date.fromisoformat("2026-07-24"), cst, Decimal("10")),
        kwargs={},
    )
    assert first == second


def test_provider_result_public_meta_exposes_request_fingerprint(session):
    result = _router(session, MarketStub("a")).get_quote("300502")
    assert len(result.request_fingerprint) == 64
    assert result.public_meta()["request_fingerprint"] == result.request_fingerprint


def test_result_for_requires_exact_request_fingerprint(session):
    router = _router(session, MarketStub("a"))
    result = router.get_quote("300502")
    assert router.result_for(
        result.capability,
        result.operation,
        result.subject,
        result.request_fingerprint,
    ) is result
    assert router.result_for(
        result.capability,
        result.operation,
        result.subject,
        "0" * 64,
    ) is None


def test_quote_and_history_request_identities_are_distinct(session):
    router = _router(session, MarketStub("a"))
    quote = router.get_quote("300502")
    history = router.get_history("300502", date(2026, 7, 1), date(2026, 7, 10))
    assert quote.request_fingerprint != history.request_fingerprint


@pytest.mark.parametrize(
    "change",
    [
        {"normalized_digest": "0" * 64},
        {"observed_at": datetime(2020, 1, 1)},
        {"provider_id": "forged-provider"},
        {"operation": "forged-operation"},
        {"request_fingerprint": "f" * 64},
    ],
)
def test_mark_persisted_rejects_runtime_lineage_mismatch(session, change):
    router = _router(session, MarketStub("a"))
    result = router.get_quote("300502")
    with pytest.raises(ProviderUnavailableError):
        router.mark_persisted(replace(result, **change))


def test_mark_persisted_rejects_dataclass_copy_of_current_result(session):
    router = _router(session, MarketStub("a"))
    result = router.get_quote("300502")
    with pytest.raises(ProviderUnavailableError):
        router.mark_persisted(replace(result))


def test_mark_persisted_allows_exact_current_lineage(session):
    router = _router(session, MarketStub("a"))
    result = router.get_quote("300502")
    router.mark_persisted(result)
    record = _quality_record(session, result)
    assert record.persisted is True


def test_series_rejects_partial_cache_even_above_min_rows(session):
    router = _router(session, MarketStub("a", row_count=100))
    result = router.get_history("300502", date(2026, 1, 1), date(2026, 7, 24))
    _persist_daily(session, router, result, row_count=90)
    selected = _series_selection(session, min_rows=80)
    assert selected.executable is False
    assert selected.structure_reason == "row_count_mismatch"


def test_series_rejects_lineage_not_persisted(session):
    router = _router(session, MarketStub("a"))
    result = router.get_history("300502", date(2026, 7, 1), date(2026, 7, 24))
    rows = result.require_value()
    for bar in rows:
        session.add(
            MarketDailyBar(
                symbol=bar.symbol,
                trade_date=bar.trade_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                adjustment=bar.adjustment,
                price_unit=bar.price_unit,
                volume_unit=bar.volume_unit,
                observed_at=bar.observed_at,
                quality_status=result.quality_status.value,
                quality_record_id=result.quality_record_id,
                source=bar.source,
                fetched_at=bar.fetched_at,
            )
        )
    session.commit()
    selected = _series_selection(session)
    assert selected.executable is False
    assert selected.structure_reason == "lineage_not_persisted"


def test_series_rejects_capability_mismatch(session):
    router = _router(session, MarketStub("a"))
    result = router.get_history("300502", date(2026, 7, 1), date(2026, 7, 24))
    _persist_daily(session, router, result)
    subject = stock_daily_subject("300502", "qfq", "CNY", "share")
    selected = resolve_cached_series(
        session,
        cache_symbol="300502",
        capability="market.daily.unadjusted",
        subject=subject,
        adjustment="qfq",
        price_unit="CNY",
        volume_unit="share",
        min_rows=3,
    )
    assert selected.executable is False
    assert selected.structure_reason == "lineage_scope_mismatch"


def test_series_rejects_subject_mismatch(session):
    router = _router(session, MarketStub("a"))
    result = router.get_history("300502", date(2026, 7, 1), date(2026, 7, 24))
    _persist_daily(session, router, result)
    selected = resolve_cached_series(
        session,
        cache_symbol="300502",
        capability="market.daily.qfq",
        subject=stock_daily_subject("600000", "qfq", "CNY", "share"),
        adjustment="qfq",
        price_unit="CNY",
        volume_unit="share",
        min_rows=3,
    )
    assert selected.executable is False
    assert selected.structure_reason == "lineage_scope_mismatch"


def test_series_rejects_semantic_key_mismatch(session):
    router = _router(session, MarketStub("a"))
    result = router.get_history("300502", date(2026, 7, 1), date(2026, 7, 24))
    _persist_daily(session, router, result)
    selected = resolve_cached_series(
        session,
        cache_symbol="300502",
        capability="market.daily.qfq",
        subject=stock_daily_subject("300502", "hfq", "CNY", "share"),
        adjustment="qfq",
        price_unit="CNY",
        volume_unit="share",
        min_rows=3,
    )
    assert selected.executable is False
    assert selected.structure_reason == "lineage_scope_mismatch"


def test_series_rejects_observed_at_mismatch(session):
    router = _router(session, MarketStub("a"))
    result = router.get_history("300502", date(2026, 7, 1), date(2026, 7, 24))
    stored = _persist_daily(session, router, result)
    stored[-1].observed_at += timedelta(minutes=1)
    session.commit()
    selected = _series_selection(session)
    assert selected.executable is False
    assert selected.structure_reason == "observed_at_mismatch"


def test_series_complete_lineage_has_no_structure_error(session):
    router = _router(session, MarketStub("a"))
    result = router.get_history("300502", date(2026, 7, 1), date(2026, 7, 24))
    _persist_daily(session, router, result)
    selected = _series_selection(session)
    assert selected.executable is True
    assert selected.structure_reason is None


def test_preflight_rejects_partial_refresh_without_changing_cache(session):
    router = _router(session, MarketStub("cached"))
    current = router.get_history("300502", date(2026, 7, 1), date(2026, 7, 24))
    stored = _persist_daily(session, router, current)
    old_ids = [item.id for item in stored]
    refresh = _router(session, MarketStub("refresh")).get_history(
        "300502", date(2026, 7, 1), date(2026, 7, 24)
    )
    with pytest.raises(ValueError, match="insufficient_rows"):
        validate_series_for_persistence(
            refresh.require_value()[:2],
            subject=refresh.subject,
            min_rows=3,
        )
    assert session.scalars(select(MarketDailyBar.id).order_by(MarketDailyBar.id)).all() == old_ids


def test_preflight_accepts_complete_same_source_refresh(session):
    result = _router(session, MarketStub("a")).get_history(
        "300502", date(2026, 7, 1), date(2026, 7, 24)
    )
    validated = validate_series_for_persistence(
        result.require_value(),
        subject=result.subject,
        min_rows=3,
    )
    assert validated == result.require_value()


def test_preflight_rejects_duplicate_dates(session):
    result = _router(session, MarketStub("a")).get_history(
        "300502", date(2026, 7, 1), date(2026, 7, 24)
    )
    bars = list(result.require_value())
    bars[1] = replace(bars[1], trade_date=bars[0].trade_date)
    with pytest.raises(ValueError, match="duplicate_trade_dates"):
        validate_series_for_persistence(bars, subject=result.subject, min_rows=3)


def test_preflight_rejects_mixed_sources(session):
    result = _router(session, MarketStub("a")).get_history(
        "300502", date(2026, 7, 1), date(2026, 7, 24)
    )
    bars = list(result.require_value())
    bars[1] = replace(bars[1], source="other")
    with pytest.raises(ValueError, match="mixed_sources"):
        validate_series_for_persistence(bars, subject=result.subject, min_rows=3)


@pytest.mark.parametrize(
    "change",
    [
        {"adjustment": "unadjusted"},
        {"price_unit": "USD"},
        {"volume_unit": "lot"},
    ],
)
def test_preflight_rejects_mixed_dimensions(session, change):
    result = _router(session, MarketStub("a")).get_history(
        "300502", date(2026, 7, 1), date(2026, 7, 24)
    )
    bars = list(result.require_value())
    bars[1] = replace(bars[1], **change)
    with pytest.raises(ValueError, match="mixed_dimensions"):
        validate_series_for_persistence(bars, subject=result.subject, min_rows=3)


def test_preflight_rejects_invalid_ohlc(session):
    result = _router(session, MarketStub("a")).get_history(
        "300502", date(2026, 7, 1), date(2026, 7, 24)
    )
    bars = list(result.require_value())
    bars[1] = replace(bars[1], high=bars[1].low - Decimal("1"))
    with pytest.raises(ValueError, match="invalid_ohlc"):
        validate_series_for_persistence(bars, subject=result.subject, min_rows=3)


def _company_profile(session, *, industry: str = "通信设备") -> CompanyProfile:
    profile = CompanyProfile(
        symbol="300502",
        name="测试公司",
        industry=industry,
        market="创业板",
        main_business=industry,
        source="fixture",
        source_url="https://example.test/profile",
        raw_data={},
        fetched_at=datetime.now(),
    )
    session.add(profile)
    session.commit()
    return profile


def test_cached_quote_step_recomputes_natural_staleness(session):
    router = _router(session, MarketStub("cached"))
    result = router.get_quote("300502")
    quote = _persist_quote(session, router, result)
    stale_at = datetime.now() - timedelta(minutes=31)
    quote.observed_at = stale_at
    _quality_record(session, result).observed_at = stale_at
    session.commit()

    step = _cached_quote_step(session, "300502")
    assert step["status"] == "partial"
    assert step["effective_quality"] == "STALE"
    assert step["executable"] is False


def test_cached_quote_step_new_conflict_blocks_old_quote(session):
    router = _router(session, MarketStub("cached"))
    cached = router.get_quote("300502")
    _persist_quote(session, router, cached)
    _router(
        session,
        MarketStub("a", quote_price="10"),
        MarketStub("b", priority=20, quote_price="11"),
    ).get_quote("300502")
    session.commit()

    step = _cached_quote_step(session, "300502")
    assert step["effective_quality"] == "CONFLICTED"
    assert step["executable"] is False
    assert step["blocking_record_id"] is not None


def test_quote_api_reports_effective_quality_and_dynamic_staleness(
    client, session
):
    router = _router(session, MarketStub("cached"))
    result = router.get_quote("300502")
    quote = _persist_quote(session, router, result)
    stale_at = datetime.now() - timedelta(minutes=31)
    quote.observed_at = stale_at
    _quality_record(session, result).observed_at = stale_at
    session.commit()

    response = client.get("/api/v1/market/quote/300502")
    assert response.status_code == 200
    body = response.json()
    assert body["quote_type"] == "realtime"
    assert body["effective_quality"] == "STALE"
    assert body["executable"] is False
    assert body["quality_record_id"] == result.quality_record_id


def test_quote_api_new_conflict_is_not_executable(client, session):
    router = _router(session, MarketStub("cached"))
    cached = router.get_quote("300502")
    _persist_quote(session, router, cached)
    _router(
        session,
        MarketStub("a", quote_price="10"),
        MarketStub("b", priority=20, quote_price="11"),
    ).get_quote("300502")
    session.commit()

    body = client.get("/api/v1/market/quote/300502").json()
    assert body["effective_quality"] == "CONFLICTED"
    assert body["executable"] is False
    assert body["blocking_reason"] == "newer_conflict"


def test_history_api_new_conflict_is_blocked(client, session):
    router = _router(session, MarketStub("cached", row_count=260))
    cached = router.get_history(
        "300502", date.today() - timedelta(days=365), date.today()
    )
    _persist_daily(session, router, cached)
    _router(
        session,
        MarketStub("a", row_count=260, daily_close="10"),
        MarketStub("b", priority=20, row_count=260, daily_close="11"),
    ).get_history("300502", date.today() - timedelta(days=365), date.today())
    session.commit()

    response = client.get("/api/v1/market/history/300502")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "MARKET_DATA_CONFLICTED"
    assert error["details"]["executable"] is False


def test_quote_api_does_not_return_latest_close_as_realtime(client, session):
    router = _router(session, MarketStub("close", quote_type="latest_close"))
    result = router.get_latest_close("300502")
    _persist_quote(session, router, result)
    response = client.get("/api/v1/market/quote/300502")
    assert response.status_code == 404


def test_load_qfq_frame_uses_exactly_one_lineage(session):
    first_router = _router(session, MarketStub("older", row_count=260))
    first = first_router.get_history(
        "300502", date(2026, 1, 1), date(2026, 7, 24)
    )
    _persist_daily(session, first_router, first)
    second_router = _router(
        session,
        MarketStub("newer", row_count=260, daily_close="12"),
    )
    second = second_router.get_history(
        "300502", date(2026, 1, 1), date(2026, 7, 24)
    )
    _persist_daily(session, second_router, second)

    frame = load_qfq_frame(session, "300502")
    assert len(frame) == 260
    assert frame.attrs["quality_record_id"] == second.quality_record_id
    assert frame.attrs["data_source"] == "newer"


def test_new_daily_conflict_blocks_technical_frame(session):
    router = _router(session, MarketStub("cached", row_count=260))
    result = router.get_history("300502", date(2026, 1, 1), date(2026, 7, 24))
    _persist_daily(session, router, result)
    _router(
        session,
        MarketStub("a", row_count=260, daily_close="10"),
        MarketStub("b", priority=20, row_count=260, daily_close="11"),
    ).get_history("300502", date(2026, 1, 1), date(2026, 7, 24))
    session.commit()
    with pytest.raises(ValueError, match="CONFLICTED"):
        load_qfq_frame(session, "300502")


def test_unrelated_stock_conflict_does_not_block_technical_frame(session):
    router = _router(session, MarketStub("cached", row_count=260))
    result = router.get_history("300502", date(2026, 1, 1), date(2026, 7, 24))
    _persist_daily(session, router, result)
    _router(
        session,
        MarketStub("a", row_count=260, daily_close="10"),
        MarketStub("b", priority=20, row_count=260, daily_close="11"),
    ).get_history("600000", date(2026, 1, 1), date(2026, 7, 24))
    session.commit()
    assert len(load_qfq_frame(session, "300502")) == 260


def test_new_daily_conflict_blocks_holding_snapshot_and_price_update(session):
    router = _router(session, MarketStub("cached", row_count=260))
    cached = router.get_history("300502", date(2026, 1, 1), date(2026, 7, 24))
    _persist_daily(session, router, cached)
    account = Account(
        name="snapshot-account",
        total_assets=Decimal("100000"),
        cash=Decimal("50000"),
        available_cash=Decimal("50000"),
    )
    session.add(account)
    session.flush()
    holding = Holding(
        account_id=account.id,
        symbol="300502",
        name="test-stock",
        quantity=100,
        cost_price=Decimal("9"),
        current_price=Decimal("9"),
        price_source="manual",
    )
    session.add(holding)
    session.commit()
    _router(
        session,
        MarketStub("a", row_count=260, daily_close="10"),
        MarketStub("b", priority=20, row_count=260, daily_close="11"),
    ).get_history("300502", date(2026, 1, 1), date(2026, 7, 24))
    session.commit()

    result = snapshot_all_holdings(session, refresh_market=False)

    session.refresh(holding)
    assert result["processed"] == 0
    assert result["errors"]
    assert holding.current_price == Decimal("9.0000")
    assert holding.price_source == "manual"
    assert session.scalar(select(TechnicalSnapshot)) is None


def test_scheduler_refresh_persists_conflict_without_updating_holding(
    session, monkeypatch
):
    router = _router(session, MarketStub("cached", row_count=260))
    cached = router.get_history(
        "300502", date.today() - timedelta(days=365), date.today()
    )
    _persist_daily(session, router, cached)
    account = Account(
        name="refresh-account",
        total_assets=Decimal("100000"),
        cash=Decimal("50000"),
        available_cash=Decimal("50000"),
    )
    session.add(account)
    session.flush()
    holding = Holding(
        account_id=account.id,
        symbol="300502",
        name="test-stock",
        quantity=100,
        cost_price=Decimal("9"),
        current_price=Decimal("9"),
        price_source="manual",
    )
    session.add(holding)
    session.commit()
    conflict_router = _router(
        session,
        MarketStub("a", row_count=260, daily_close="10"),
        MarketStub("b", priority=20, row_count=260, daily_close="11"),
    )
    monkeypatch.setattr(
        "app.services.technical_snapshots.build_data_hub",
        lambda db: conflict_router,
    )

    result = snapshot_all_holdings(session, refresh_market=True)

    session.refresh(holding)
    latest = session.scalar(
        select(DataQualityRecord)
        .where(
            DataQualityRecord.capability == "market.daily.qfq",
            DataQualityRecord.subject_id == "300502",
        )
        .order_by(DataQualityRecord.id.desc())
    )
    assert result["processed"] == 0
    assert result["errors"]
    assert latest.quality_status == "CONFLICTED"
    assert latest.persisted is False
    assert holding.current_price == Decimal("9.0000")
    assert holding.price_source == "manual"
    assert session.scalar(select(TechnicalSnapshot)) is None


def test_new_index_conflict_blocks_old_market_assessment(session):
    cached_router = _router(
        session,
        MarketStub("cached", series_row_count=80),
    )
    cached = cached_router.get_index_history(
        "csi000300", date(2026, 1, 1), date(2026, 7, 24)
    )
    subject = index_daily_subject("csi000300", "unadjusted", "CNY", "share")
    _persist_mapping_series(
        session,
        cached_router,
        cached,
        cache_symbol=subject.subject_id,
        adjustment="unadjusted",
    )
    _router(
        session,
        MarketStub("a", series_row_count=80, daily_close="10"),
        MarketStub("b", priority=20, series_row_count=80, daily_close="11"),
    ).get_index_history("csi000300", date(2026, 1, 1), date(2026, 7, 24))
    session.commit()

    assessment, step = _market_assessment(
        session,
        _router(session, MarketStub("offline", failures={"get_index_history"})),
    )
    assert assessment["state"] == "无法判断"
    assert step["effective_quality"] == "CONFLICTED"
    assert step["executable"] is False


def test_new_sector_conflict_blocks_old_sector_assessment(session):
    profile = _company_profile(session)
    subject = sector_daily_subject("通信设备", "unadjusted", "CNY", "share")
    cached_router = _router(
        session,
        MarketStub("cached", series_row_count=80),
    )
    cached = cached_router.get_sector_history(
        "通信设备", date(2026, 1, 1), date(2026, 7, 24)
    )
    _persist_mapping_series(
        session,
        cached_router,
        cached,
        cache_symbol=subject.subject_id,
        adjustment="unadjusted",
    )
    _router(
        session,
        MarketStub("a", series_row_count=80, daily_close="10"),
        MarketStub("b", priority=20, series_row_count=80, daily_close="11"),
    ).get_sector_history("通信设备", date(2026, 1, 1), date(2026, 7, 24))
    session.commit()

    assessment, step = _sector_assessment(
        session,
        _router(session, MarketStub("offline", failures={"get_sector_history"})),
        profile,
        {"return_20d": 1},
    )
    assert assessment["state"] == "无法判断"
    assert step["effective_quality"] == "CONFLICTED"
    assert step["executable"] is False


def test_unrelated_sector_conflict_does_not_block_current_sector(session):
    profile = _company_profile(session)
    subject = sector_daily_subject("通信设备", "unadjusted", "CNY", "share")
    cached_router = _router(
        session,
        MarketStub("cached", series_row_count=80),
    )
    cached = cached_router.get_sector_history(
        "通信设备", date(2026, 1, 1), date(2026, 7, 24)
    )
    _persist_mapping_series(
        session,
        cached_router,
        cached,
        cache_symbol=subject.subject_id,
        adjustment="unadjusted",
    )
    _router(
        session,
        MarketStub("a", series_row_count=80, daily_close="10"),
        MarketStub("b", priority=20, series_row_count=80, daily_close="11"),
    ).get_sector_history("半导体", date(2026, 1, 1), date(2026, 7, 24))
    session.commit()

    assessment, step = _sector_assessment(
        session,
        _router(session, MarketStub("offline", failures={"get_sector_history"})),
        profile,
        {"return_20d": 1},
    )
    assert assessment["state"] != "无法判断"
    assert step["executable"] is True


def test_market_sync_replaces_same_source_with_one_complete_lineage(
    client, session, monkeypatch
):
    today = shanghai_today()
    old_router = _router(session, MarketStub("same-source", row_count=270))
    old = old_router.get_history(
        "300502", today - timedelta(days=365), today
    )
    _persist_daily(session, old_router, old)
    new_router = _router(
        session,
        MarketStub("same-source", row_count=260, daily_close="12"),
    )
    monkeypatch.setattr("app.api.advanced.build_data_hub", lambda db: new_router)

    response = client.post("/api/v1/market/sync?symbol=300502&days=365")

    assert response.status_code == 200, response.text
    bars = session.scalars(
        select(MarketDailyBar)
        .where(MarketDailyBar.symbol == "300502")
        .order_by(MarketDailyBar.trade_date)
    ).all()
    assert len(bars) == 260
    assert {bar.quality_record_id for bar in bars} == {
        new_router.result_for(
            "market.daily.qfq",
            "get_history",
            stock_daily_subject("300502", "qfq", "CNY", "share"),
            request_fingerprint(
                capability="market.daily.qfq",
                operation="get_history",
                args=(
                    "300502",
                    today - timedelta(days=365),
                    today,
                ),
                symbol="300502",
            ),
        ).quality_record_id
    }
    assert {bar.close for bar in bars} == {Decimal("12.0000")}


def test_market_sync_rolls_back_replacement_when_lineage_mark_fails(
    client, session, monkeypatch
):
    old_router = _router(session, MarketStub("same-source", row_count=260))
    old = old_router.get_history(
        "300502", date.today() - timedelta(days=365), date.today()
    )
    old_bars = _persist_daily(session, old_router, old)
    old_quality_record_id = old.quality_record_id
    old_quote = old_router.get_quote("300502")
    _persist_quote(session, old_router, old_quote)
    new_router = _router(
        session,
        MarketStub("same-source", row_count=260, daily_close="12"),
    )
    original_mark_persisted = new_router.mark_persisted

    def fail_history_lineage(result, cached_at=None):
        if result.capability == "market.daily.qfq":
            raise ProviderUnavailableError("lineage mark failed")
        return original_mark_persisted(result, cached_at=cached_at)

    monkeypatch.setattr(new_router, "mark_persisted", fail_history_lineage)
    monkeypatch.setattr("app.api.advanced.build_data_hub", lambda db: new_router)

    response = client.post("/api/v1/market/sync?symbol=300502&days=365")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "stale_fallback"
    bars = session.scalars(
        select(MarketDailyBar).where(MarketDailyBar.symbol == "300502")
    ).all()
    assert len(bars) == len(old_bars)
    assert {bar.quality_record_id for bar in bars} == {old_quality_record_id}
    assert {bar.close for bar in bars} == {Decimal("10.0000")}


def _index_refresh_fixture(session, *, close: str, provider_id: str = "index"):
    router = _router(
        session,
        MarketStub(provider_id, series_row_count=100, daily_close=close),
    )
    result = router.get_index_history(
        "csi000300", date.today() - timedelta(days=365), date.today()
    )
    subject = index_daily_subject("csi000300", "unadjusted", "CNY", "share")
    bars = mapping_series_bars(
        result.require_value(),
        cache_symbol=subject.subject_id,
        adjustment="unadjusted",
    )
    return router, result, subject, bars


def _list_refresh_fixture(
    session,
    *,
    close: str = "10",
    provider_id: str = "list-series",
):
    router = _router(
        session,
        MarketStub(provider_id, row_count=100, daily_close=close),
    )
    result = router.get_history(
        "300502", date.today() - timedelta(days=365), date.today()
    )
    subject = stock_daily_subject("300502", "qfq", "CNY", "share")
    return router, result, subject, list(result.require_value())


def test_mapping_series_subset_is_rejected_before_delete(session):
    old_router, old, subject, old_bars = _index_refresh_fixture(
        session, close="10"
    )
    replace_market_series(
        session,
        old_router,
        old,
        old_bars,
        subject=subject,
        min_rows=60,
    )
    session.commit()
    new_router, new, _, new_bars = _index_refresh_fixture(session, close="12")

    with pytest.raises(ProviderUnavailableError, match="row count"):
        replace_market_series(
            session,
            new_router,
            new,
            new_bars[:60],
            subject=subject,
            min_rows=60,
        )

    stored = session.scalars(
        select(MarketDailyBar).where(MarketDailyBar.symbol == subject.subject_id)
    ).all()
    assert len(stored) == 100
    assert {item.quality_record_id for item in stored} == {old.quality_record_id}
    assert _quality_record(session, new).persisted is False


def test_mapping_series_row_count_matches_provider_payload(session):
    router, result, subject, bars = _index_refresh_fixture(session, close="10")
    assert len(result.require_value()["rows"]) == 100
    assert len(bars) == 100
    stored = replace_market_series(
        session,
        router,
        result,
        bars,
        subject=subject,
        min_rows=60,
    )
    assert len(stored) == 100


def test_mapping_series_row_count_matches_quality_record(session):
    router, result, subject, bars = _index_refresh_fixture(session, close="10")
    _quality_record(session, result).row_count = 99
    session.flush()

    with pytest.raises(ProviderUnavailableError, match="quality record row count"):
        replace_market_series(
            session,
            router,
            result,
            bars,
            subject=subject,
            min_rows=60,
        )

    assert session.scalar(
        select(func.count(MarketDailyBar.id)).where(
            MarketDailyBar.symbol == subject.subject_id
        )
    ) == 0
    assert _quality_record(session, result).persisted is False


def test_invalid_mapping_refresh_preserves_existing_cache(session):
    old_router, old, subject, old_bars = _index_refresh_fixture(
        session, close="10"
    )
    replace_market_series(
        session,
        old_router,
        old,
        old_bars,
        subject=subject,
        min_rows=60,
    )
    session.commit()
    new_router, new, _, new_bars = _index_refresh_fixture(session, close="12")

    with pytest.raises(ProviderUnavailableError):
        replace_market_series(
            session,
            new_router,
            new,
            new_bars[:-1],
            subject=subject,
            min_rows=60,
        )

    stored = session.scalars(
        select(MarketDailyBar).where(MarketDailyBar.symbol == subject.subject_id)
    ).all()
    assert len(stored) == 100
    assert {item.close for item in stored} == {Decimal("10.0000")}
    assert {item.quality_record_id for item in stored} == {old.quality_record_id}


def test_complete_mapping_refresh_replaces_existing_cache(session):
    old_router, old, subject, old_bars = _index_refresh_fixture(
        session, close="10"
    )
    replace_market_series(
        session,
        old_router,
        old,
        old_bars,
        subject=subject,
        min_rows=60,
    )
    session.commit()
    new_router, new, _, new_bars = _index_refresh_fixture(session, close="12")
    replace_market_series(
        session,
        new_router,
        new,
        new_bars,
        subject=subject,
        min_rows=60,
    )

    stored = session.scalars(
        select(MarketDailyBar).where(MarketDailyBar.symbol == subject.subject_id)
    ).all()
    assert len(stored) == 100
    assert {item.close for item in stored} == {Decimal("12.0000")}
    assert {item.quality_record_id for item in stored} == {new.quality_record_id}


def test_same_length_changed_close_is_rejected_before_delete(session):
    router, result, subject, bars = _list_refresh_fixture(session)
    tampered = [*bars]
    tampered[0] = replace(tampered[0], close=tampered[0].close + Decimal("0.01"))
    with pytest.raises(ProviderUnavailableError, match="content does not match"):
        replace_market_series(
            session, router, result, tampered, subject=subject, min_rows=60
        )


def test_same_length_changed_volume_is_rejected_before_delete(session):
    router, result, subject, bars = _list_refresh_fixture(session)
    tampered = [*bars]
    tampered[0] = replace(tampered[0], volume=tampered[0].volume + Decimal("1"))
    with pytest.raises(ProviderUnavailableError, match="content does not match"):
        replace_market_series(
            session, router, result, tampered, subject=subject, min_rows=60
        )


def test_same_length_changed_trade_date_is_rejected_before_delete(session):
    router, result, subject, bars = _list_refresh_fixture(session)
    tampered = [*bars]
    tampered[0] = replace(
        tampered[0], trade_date=tampered[0].trade_date - timedelta(days=1)
    )
    with pytest.raises(ProviderUnavailableError, match="content does not match"):
        replace_market_series(
            session, router, result, tampered, subject=subject, min_rows=60
        )


def test_same_length_changed_source_is_rejected_before_delete(session):
    router, result, subject, bars = _list_refresh_fixture(session)
    tampered = [replace(item, source="tampered-source") for item in bars]
    with pytest.raises(ProviderUnavailableError, match="content does not match"):
        replace_market_series(
            session, router, result, tampered, subject=subject, min_rows=60
        )


def test_mapping_series_same_length_tampering_is_rejected(session):
    router, result, subject, bars = _index_refresh_fixture(session, close="10")
    tampered = [*bars]
    tampered[0] = replace(
        tampered[0],
        open=Decimal("20"),
        high=Decimal("20"),
        low=Decimal("20"),
        close=Decimal("20"),
    )
    with pytest.raises(ProviderUnavailableError, match="content does not match"):
        replace_market_series(
            session, router, result, tampered, subject=subject, min_rows=60
        )


def test_list_series_same_length_tampering_is_rejected(session):
    router, result, subject, bars = _list_refresh_fixture(session)
    tampered = [*bars]
    tampered[-1] = replace(
        tampered[-1], volume=tampered[-1].volume + Decimal("100")
    )
    with pytest.raises(ProviderUnavailableError, match="content does not match"):
        replace_market_series(
            session, router, result, tampered, subject=subject, min_rows=60
        )


def test_content_mismatch_preserves_existing_complete_cache(session):
    old_router, old, subject, old_bars = _index_refresh_fixture(
        session, close="8", provider_id="same-source"
    )
    replace_market_series(
        session, old_router, old, old_bars, subject=subject, min_rows=60
    )
    session.commit()
    new_router, new, _, new_bars = _index_refresh_fixture(
        session, close="10", provider_id="same-source"
    )
    tampered = [
        replace(
            item,
            open=Decimal("20"),
            high=Decimal("20"),
            low=Decimal("20"),
            close=Decimal("20"),
        )
        for item in new_bars
    ]

    with pytest.raises(ProviderUnavailableError, match="content does not match"):
        replace_market_series(
            session, new_router, new, tampered, subject=subject, min_rows=60
        )

    stored = session.scalars(
        select(MarketDailyBar).where(MarketDailyBar.symbol == subject.subject_id)
    ).all()
    assert len(stored) == 100
    assert {item.quality_record_id for item in stored} == {old.quality_record_id}
    assert {item.close for item in stored} == {Decimal("8.0000")}


def test_content_mismatch_keeps_quality_record_unpersisted(session):
    router, result, subject, bars = _list_refresh_fixture(session)
    tampered = [replace(item, fetched_at=item.fetched_at + timedelta(seconds=1)) for item in bars]
    with pytest.raises(ProviderUnavailableError, match="content does not match"):
        replace_market_series(
            session, router, result, tampered, subject=subject, min_rows=60
        )
    assert _quality_record(session, result).persisted is False


def test_exact_provider_series_is_accepted(session):
    router, result, subject, bars = _list_refresh_fixture(session)
    stored = replace_market_series(
        session, router, result, bars, subject=subject, min_rows=60
    )
    assert len(stored) == len(result.require_value()) == 100
    assert _quality_record(session, result).persisted is True


def test_canonical_series_row_normalizes_decimal_and_timezone(session):
    _, _, _, bars = _list_refresh_fixture(session)
    original = bars[0]
    equivalent = replace(
        original,
        open=original.open.quantize(Decimal("0.0000")),
        high=original.high.quantize(Decimal("0.0000")),
        observed_at=original.observed_at.astimezone(timezone.utc),
        fetched_at=original.fetched_at.astimezone(timezone.utc),
    )
    assert canonical_series_row(equivalent) == canonical_series_row(original)


def test_test_environment_disables_numba_jit_by_default():
    assert os.environ["NUMBA_DISABLE_JIT"] == "1"


def test_explicit_numba_environment_is_not_overwritten():
    environment = {**os.environ, "NUMBA_DISABLE_JIT": "0"}
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, runpy; "
            "runpy.run_path('tests/conftest.py'); "
            "print(os.environ['NUMBA_DISABLE_JIT'])",
        ],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert completed.stdout.strip().splitlines()[-1] == "0"


def _seed_index_cache(session, *, close="10"):
    router, result, subject, bars = _index_refresh_fixture(
        session, close=close, provider_id="trusted-index"
    )
    replace_market_series(
        session, router, result, bars, subject=subject, min_rows=60
    )
    session.commit()
    return subject, result


def _add_index_conflict(session):
    result = _router(
        session,
        MarketStub("index-a", series_row_count=100, daily_close="10"),
        MarketStub(
            "index-b", priority=20, series_row_count=100, daily_close="11"
        ),
    ).get_index_history(
        "csi000300", date.today() - timedelta(days=240), date.today()
    )
    assert result.quality_status == DataQualityStatus.CONFLICTED
    session.commit()


def test_single_source_does_not_bypass_existing_index_conflict(session):
    _seed_index_cache(session)
    _add_index_conflict(session)
    assessment, step = _market_assessment(
        session,
        _router(
            session,
            MarketStub("single-index", series_row_count=100, daily_close="12"),
        ),
    )
    assert assessment["return_20d"] is None
    assert step["status"] != "success"
    assert step["executable"] is False


def test_effective_conflict_cannot_return_success_step(session):
    _seed_index_cache(session)
    _add_index_conflict(session)
    _, step = _market_assessment(
        session,
        _router(
            session,
            MarketStub("single-index", series_row_count=100, daily_close="12"),
        ),
    )
    assert step["effective_quality"] == "CONFLICTED"
    assert step["status"] != "success"


def test_index_assessment_uses_selected_lineage_bars(session):
    subject, trusted = _seed_index_cache(session, close="10")
    assessment, step = _market_assessment(
        session,
        _router(session, MarketStub("offline", failures={"get_index_history"})),
    )
    assert assessment["close"] == 10.0
    assert step["quality_record_id"] == trusted.quality_record_id
    assert step["subject_id"] == subject.subject_id


def test_stock_single_source_after_conflict_is_not_success(session):
    trusted_router = _router(session, MarketStub("trusted", row_count=260))
    trusted = trusted_router.get_history(
        "300502", date.today() - timedelta(days=900), date.today()
    )
    _persist_daily(session, trusted_router, trusted)
    conflict = _router(
        session,
        MarketStub("stock-a", row_count=260, daily_close="10"),
        MarketStub(
            "stock-b", priority=20, row_count=260, daily_close="11"
        ),
    ).get_history("300502", date.today() - timedelta(days=900), date.today())
    assert conflict.quality_status == DataQualityStatus.CONFLICTED
    session.commit()

    step, _ = _sync_stock(
        session,
        "300502",
        _router(session, MarketStub("single", row_count=260, daily_close="12")),
        True,
    )
    assert step["status"] != "success"
    assert step["executable"] is False


def test_failed_effective_check_rolls_back_series_replacement(session):
    trusted_router = _router(session, MarketStub("trusted", row_count=260))
    trusted = trusted_router.get_history(
        "300502", date.today() - timedelta(days=900), date.today()
    )
    _persist_daily(session, trusted_router, trusted)
    _router(
        session,
        MarketStub("stock-a", row_count=260, daily_close="10"),
        MarketStub(
            "stock-b", priority=20, row_count=260, daily_close="11"
        ),
    ).get_history("300502", date.today() - timedelta(days=900), date.today())
    session.commit()
    _sync_stock(
        session,
        "300502",
        _router(session, MarketStub("single", row_count=260, daily_close="12")),
        True,
    )
    rows = session.scalars(
        select(MarketDailyBar).where(MarketDailyBar.symbol == "300502")
    ).all()
    assert {item.quality_record_id for item in rows} == {trusted.quality_record_id}
    assert {item.close for item in rows} == {Decimal("10.0000")}


def test_market_sync_success_reports_effective_quality(client, session, monkeypatch):
    router = _router(session, MarketStub("sync", row_count=260))
    monkeypatch.setattr("app.api.advanced.build_data_hub", lambda db: router)
    response = client.post("/api/v1/market/sync?symbol=300502&days=365")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"
    assert body["executable"] is True
    assert body["quote_quality"]["effective_quality"] == "SINGLE_SOURCE"
    assert body["history_quality"]["effective_quality"] == "SINGLE_SOURCE"


def _market_sync_after_history_conflict(client, session, monkeypatch):
    trusted_router = _router(session, MarketStub("trusted", row_count=260))
    trusted = trusted_router.get_history(
        "300502", date.today() - timedelta(days=365), date.today()
    )
    _persist_daily(session, trusted_router, trusted)
    quote = trusted_router.get_quote("300502")
    _persist_quote(session, trusted_router, quote)
    _router(
        session,
        MarketStub("stock-a", row_count=260, daily_close="10"),
        MarketStub(
            "stock-b", priority=20, row_count=260, daily_close="11"
        ),
    ).get_history("300502", date.today() - timedelta(days=365), date.today())
    session.commit()
    refresh = _router(session, MarketStub("single", row_count=260, daily_close="12"))
    monkeypatch.setattr("app.api.advanced.build_data_hub", lambda db: refresh)

    response = client.post("/api/v1/market/sync?symbol=300502&days=365")
    return response, trusted


def test_market_sync_single_source_after_conflict_is_blocked(
    client, session, monkeypatch
):
    response, trusted = _market_sync_after_history_conflict(
        client, session, monkeypatch
    )
    assert response.status_code != 200
    rows = session.scalars(
        select(MarketDailyBar).where(MarketDailyBar.symbol == "300502")
    ).all()
    assert {item.quality_record_id for item in rows} == {trusted.quality_record_id}
    assert {item.close for item in rows} == {Decimal("10.0000")}


def _sector_refresh_after_conflict(session):
    profile = _company_profile(session)
    trusted_router = _router(
        session, MarketStub("trusted-sector", series_row_count=100)
    )
    trusted = trusted_router.get_sector_history(
        profile.industry, date.today() - timedelta(days=240), date.today()
    )
    subject = trusted.subject
    bars = mapping_series_bars(
        trusted.require_value(),
        cache_symbol=subject.subject_id,
        adjustment="unadjusted",
    )
    replace_market_series(
        session, trusted_router, trusted, bars, subject=subject, min_rows=60
    )
    session.commit()
    conflict = _router(
        session,
        MarketStub("sector-a", series_row_count=100, daily_close="10"),
        MarketStub(
            "sector-b", priority=20, series_row_count=100, daily_close="11"
        ),
    ).get_sector_history(
        profile.industry, date.today() - timedelta(days=240), date.today()
    )
    assert conflict.quality_status == DataQualityStatus.CONFLICTED
    session.commit()
    assessment, step = _sector_assessment(
        session,
        _router(
            session,
            MarketStub("single-sector", series_row_count=100, daily_close="12"),
        ),
        profile,
        {"return_20d": 0},
        refresh=True,
    )
    return assessment, step, trusted


def test_single_source_does_not_bypass_existing_sector_conflict(session):
    _, step, _ = _sector_refresh_after_conflict(session)
    assert step["status"] != "success"
    assert step["effective_quality"] == "CONFLICTED"
    assert step["executable"] is False


def test_sector_assessment_uses_selected_lineage_bars(session):
    profile = _company_profile(session)
    router = _router(session, MarketStub("sector-selected", series_row_count=100))
    result = router.get_sector_history(
        profile.industry, date.today() - timedelta(days=240), date.today()
    )
    subject = result.subject
    bars = mapping_series_bars(
        result.require_value(),
        cache_symbol=subject.subject_id,
        adjustment="unadjusted",
    )
    replace_market_series(
        session, router, result, bars, subject=subject, min_rows=60
    )
    session.commit()
    assessment, step = _sector_assessment(
        session,
        _router(session, MarketStub("offline", failures={"get_sector_history"})),
        profile,
        {"return_20d": 0},
    )
    assert assessment["return_20d"] == 0.0
    assert step["quality_record_id"] == result.quality_record_id
    assert step["subject_id"] == subject.subject_id


def test_stock_step_never_success_when_effective_quality_blocks(session):
    trusted_router = _router(session, MarketStub("trusted", row_count=260))
    trusted = trusted_router.get_history(
        "300502", date.today() - timedelta(days=900), date.today()
    )
    _persist_daily(session, trusted_router, trusted)
    _router(
        session,
        MarketStub("stock-a", row_count=260, daily_close="10"),
        MarketStub(
            "stock-b", priority=20, row_count=260, daily_close="11"
        ),
    ).get_history("300502", date.today() - timedelta(days=900), date.today())
    session.commit()
    step, _ = _sync_stock(
        session,
        "300502",
        _router(session, MarketStub("single", row_count=260, daily_close="12")),
        True,
    )
    assert step["effective_quality"] == "CONFLICTED"
    assert step["executable"] is False
    assert step["status"] != "success"


def test_quote_step_and_rule_engine_share_same_effective_quality(session):
    conflict = _router(
        session,
        MarketStub("quote-a", quote_price="10"),
        MarketStub("quote-b", priority=20, quote_price="11"),
    ).get_quote("300502")
    assert conflict.quality_status == DataQualityStatus.CONFLICTED
    session.commit()
    step, context = _sync_stock(
        session,
        "300502",
        _router(session, MarketStub("single", row_count=260, quote_price="12")),
        True,
    )
    quote_step = context["quote_step"]
    selected = resolve_cached_quote(
        session, symbol="300502", capability="market.quote.realtime"
    )
    assert step["status"] == "success"
    assert quote_step["status"] != "success"
    assert quote_step["effective_quality"] == (
        selected.effective_quality.effective_quality.value
    )
    assert quote_step["executable"] is selected.executable is False


def test_market_sync_does_not_report_raw_quality_as_effective(
    client, session, monkeypatch
):
    response, _ = _market_sync_after_history_conflict(
        client, session, monkeypatch
    )
    latest = session.scalar(
        select(DataQualityRecord)
        .where(DataQualityRecord.capability == "market.daily.qfq")
        .order_by(DataQualityRecord.id.desc())
    )
    assert latest.quality_status == "SINGLE_SOURCE"
    assert response.status_code != 200


def test_market_sync_effective_failure_preserves_previous_cache(
    client, session, monkeypatch
):
    _, trusted = _market_sync_after_history_conflict(
        client, session, monkeypatch
    )
    rows = session.scalars(
        select(MarketDailyBar).where(MarketDailyBar.symbol == "300502")
    ).all()
    assert {item.quality_record_id for item in rows} == {trusted.quality_record_id}
    assert {item.close for item in rows} == {Decimal("10.0000")}
