from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
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
from app.data_hub.router import DataHubRouter
from app.domain.quality import DataQualityStatus
from app.domain.quality_subject import SubjectRef, canonical_semantic_key
from app.models import (
    DataQualityRecord,
    DataQualitySubjectHead,
    MarketDailyBar,
    MarketQuote,
)
from app.services.market_cache import resolve_cached_quote, resolve_cached_series


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
        self.observed_at = observed_at
        self.failures = failures or set()

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def _fail_if_requested(self, operation: str) -> None:
        if operation in self.failures:
            raise ProviderUnavailableError(f"{operation} unavailable")

    def get_quote(self, symbol: str) -> Quote:
        self._fail_if_requested("get_quote")
        now = self.observed_at or datetime.now()
        return Quote(
            symbol=symbol,
            name=f"stock-{symbol}",
            price=self.quote_price,
            quote_type=self.quote_type,
            observed_at=now,
            price_unit="CNY",
            source=self.provider_id,
            source_api="quote",
            fetched_at=datetime.now(),
        )

    def get_quote_alias(self, symbol: str) -> Quote:
        return self.get_quote(symbol)

    def get_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        self._fail_if_requested("get_history")
        rows = []
        for offset in range(3):
            trade_date = end - timedelta(days=2 - offset)
            observed_at = datetime.combine(trade_date, datetime.min.time())
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
                    observed_at=observed_at,
                    source=self.provider_id,
                    fetched_at=datetime.now(),
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
        return {
            "rows": [
                {
                    "date": end - timedelta(days=19 - offset),
                    "close": self.daily_close,
                    "volume": Decimal("10000"),
                }
                for offset in range(20)
            ],
            "source": self.provider_id,
            "fetched_at": datetime.now(),
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
            observed_at=datetime.combine(row["date"], datetime.min.time()),
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
        "market.quote.realtime", "get_quote", first.subject
    ) is first
    assert router.result_for(
        "market.quote.realtime", "get_quote", second.subject
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
    assert router.result_for("market.quote.realtime", "get_quote", subject) is first
    assert (
        router.result_for("market.quote.realtime", "get_quote_alias", subject)
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
    now = datetime.now()
    conflict, refresh = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=2),
        refresh_time=now - timedelta(minutes=1),
    )
    assert refresh.quality_status == "VERIFIED"
    assert refresh.supersedes_record_id == conflict.id


def test_verified_provider_fallback_supersedes_same_scope_conflict(session):
    now = datetime.now()
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


def test_single_source_refresh_does_not_supersede_conflict(session):
    now = datetime.now()
    _, refresh = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=2),
        refresh_time=now - timedelta(minutes=1),
        refresh_sources=1,
    )
    assert refresh.quality_status == "SINGLE_SOURCE"
    assert refresh.supersedes_record_id is None


def test_backdated_verified_refresh_does_not_supersede(session):
    now = datetime.now()
    _, refresh = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=1),
        refresh_time=now - timedelta(minutes=2),
    )
    assert refresh.quality_status == "VERIFIED"
    assert refresh.supersedes_record_id is None


def test_future_verified_refresh_does_not_supersede(session):
    now = datetime.now()
    _, refresh = _conflict_then_refresh(
        session,
        conflict_time=now - timedelta(minutes=1),
        refresh_time=now + timedelta(minutes=1),
    )
    assert refresh.quality_status == "VERIFIED"
    assert refresh.supersedes_record_id is None


def test_different_semantic_key_does_not_supersede(session):
    now = datetime.now()
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
