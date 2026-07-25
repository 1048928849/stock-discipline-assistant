from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from app.config import Settings
from app.data_hub.contracts import (
    DailyBar,
    DataProvider,
    ProviderMetadata,
    ProviderUnavailableError,
    Quote,
    validate_daily_bar_contract,
)
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import (
    SHANGHAI_TZ,
    TradingPhase,
    XSHGTradingCalendar,
)
from app.domain.quality import DataQualityStatus
from app.models import DataProviderCallLog
from app.providers.akshare_provider import AKShareProvider
from app.providers.external_http_provider import ProfessionalMarketApiProvider
from app.providers.tushare_provider import TushareProvider
from app.services.market_cache import persist_market_quote, resolve_cached_quote


MORNING = datetime(2026, 7, 24, 10, 0, tzinfo=SHANGHAI_TZ)
AFTERNOON = datetime(2026, 7, 24, 14, 0, tzinfo=SHANGHAI_TZ)


class QuoteProvider(DataProvider):
    def __init__(self, provider_id, quote, *, priority=1):
        self.quote = quote
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=(
                "market.quote.realtime",
                "market.quote.latest_close",
            ),
            priority=priority,
            realtime_supported=True,
        )

    def health_check(self, probe=False):
        return {"status": "healthy"}

    def get_quote(self, symbol):
        return self.quote


def _quote(
    *,
    quote_type="realtime",
    symbol="300502",
    observed_at=MORNING,
    fetched_at=MORNING + timedelta(seconds=1),
    price_unit="CNY",
):
    return Quote(
        symbol=symbol,
        name="test",
        price=Decimal("10"),
        quote_type=quote_type,
        observed_at=observed_at,
        price_unit=price_unit,
        source="test",
        source_api="fixture",
        fetched_at=fetched_at,
    )


def _router(session, now, *providers):
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return DataHubRouter(
        session,
        registry,
        calendar=XSHGTradingCalendar(),
        now_fn=lambda: now,
    )


def test_market_phase_morning_session():
    calendar = XSHGTradingCalendar()
    assert calendar.market_phase(MORNING) == TradingPhase.MORNING_SESSION


def test_market_phase_lunch_break():
    calendar = XSHGTradingCalendar()
    at_boundary = datetime(2026, 7, 24, 11, 30, tzinfo=SHANGHAI_TZ)
    assert calendar.market_phase(at_boundary) == TradingPhase.LUNCH_BREAK
    assert calendar.is_realtime_session(at_boundary) is False


def test_market_phase_afternoon_session():
    calendar = XSHGTradingCalendar()
    assert calendar.market_phase(AFTERNOON) == TradingPhase.AFTERNOON_SESSION


def test_market_phase_after_close():
    calendar = XSHGTradingCalendar()
    at_boundary = datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ)
    assert calendar.market_phase(at_boundary) == TradingPhase.CLOSED
    assert calendar.is_realtime_session(at_boundary) is False


def test_market_phase_weekend():
    calendar = XSHGTradingCalendar()
    saturday = datetime(2026, 7, 25, 10, 0, tzinfo=SHANGHAI_TZ)
    assert calendar.market_phase(saturday) == TradingPhase.NON_TRADING_DAY


def test_market_phase_exchange_holiday():
    calendar = XSHGTradingCalendar()
    holiday = datetime(2026, 2, 20, 10, 0, tzinfo=SHANGHAI_TZ)
    assert calendar.market_phase(holiday) == TradingPhase.NON_TRADING_DAY


def test_session_close_is_1500_shanghai_time():
    close = XSHGTradingCalendar().session_close_at(date(2026, 7, 24))
    assert close == datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ)


def test_router_never_falls_back_quote_observed_at_to_fetched_at():
    quote = _quote(observed_at=None)
    assert DataHubRouter._observed_at(
        "market.quote.realtime", quote, quote.fetched_at
    ) is None


def test_realtime_capability_rejects_latest_close_quote(session):
    quote = _quote(
        quote_type="latest_close",
        observed_at=datetime(2026, 7, 23, 15, 0, tzinfo=SHANGHAI_TZ),
    )
    result = _router(session, MORNING, QuoteProvider("wrong", quote)).get_quote(
        "300502"
    )
    assert result.quality_status == DataQualityStatus.MISSING


def test_latest_close_capability_rejects_realtime_quote(session):
    result = _router(
        session, MORNING, QuoteProvider("wrong", _quote())
    ).get_latest_close("300502")
    assert result.quality_status == DataQualityStatus.MISSING


def test_realtime_rejects_missing_observed_at(session):
    result = _router(
        session, MORNING, QuoteProvider("missing-time", _quote(observed_at=None))
    ).get_quote("300502")
    assert result.quality_status == DataQualityStatus.MISSING


def test_realtime_rejects_future_observed_at(session):
    result = _router(
        session,
        MORNING,
        QuoteProvider(
            "future",
            _quote(
                observed_at=MORNING + timedelta(minutes=1),
                fetched_at=MORNING,
            ),
        ),
    ).get_quote("300502")
    assert result.quality_status == DataQualityStatus.MISSING


def test_realtime_rejects_wrong_symbol(session):
    result = _router(
        session, MORNING, QuoteProvider("wrong-symbol", _quote(symbol="600000"))
    ).get_quote("300502")
    assert result.quality_status == DataQualityStatus.MISSING


def test_realtime_rejects_wrong_unit(session):
    result = _router(
        session, MORNING, QuoteProvider("wrong-unit", _quote(price_unit="USD"))
    ).get_quote("300502")
    assert result.quality_status == DataQualityStatus.MISSING


def test_contract_failure_is_audited_as_provider_failure(session):
    result = _router(
        session, MORNING, QuoteProvider("invalid", _quote(price_unit="USD"))
    ).get_quote("300502")
    log = session.query(DataProviderCallLog).filter_by(provider_id="invalid").one()
    assert result.quality_status == DataQualityStatus.MISSING
    assert log.status == "failed"
    assert "contract" in log.error.lower()


def test_invalid_provider_is_excluded_when_another_provider_is_valid(session):
    router = _router(
        session,
        MORNING,
        QuoteProvider("invalid", _quote(price_unit="USD")),
        QuoteProvider("valid", _quote(), priority=2),
    )
    result = router.get_quote("300502")
    assert result.provider_id == "valid"
    assert result.quality_status == DataQualityStatus.SINGLE_SOURCE
    assert any("invalid" in error for error in result.errors)


def _spot_frame(day="2026-07-24"):
    return pd.DataFrame(
        [{"代码": "300502", "名称": "test", "最新价": 10, "日期": day}]
    )


def test_akshare_active_session_quote_is_realtime():
    provider = AKShareProvider(now_fn=lambda: MORNING)
    quote = provider._quote_from_frame(
        _spot_frame(), "300502", "akshare-test", "spot-test"
    )
    assert quote.quote_type == "realtime"
    assert quote.observed_at == quote.fetched_at == MORNING


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 7, 24, 12, 0, tzinfo=SHANGHAI_TZ),
        datetime(2026, 7, 24, 15, 30, tzinfo=SHANGHAI_TZ),
        datetime(2026, 7, 25, 10, 0, tzinfo=SHANGHAI_TZ),
    ],
    ids=["lunch", "after-close", "weekend"],
)
def test_akshare_non_active_phase_does_not_claim_realtime(now):
    day = "2026-07-23" if now.hour == 12 else "2026-07-24"
    provider = AKShareProvider(now_fn=lambda: now)
    quote = provider._quote_from_frame(
        _spot_frame(day), "300502", "akshare-test", "spot-test"
    )
    assert quote.quote_type == "latest_close"


def test_akshare_lunch_break_does_not_claim_realtime():
    now = datetime(2026, 7, 24, 12, 0, tzinfo=SHANGHAI_TZ)
    quote = AKShareProvider(now_fn=lambda: now)._quote_from_frame(
        _spot_frame("2026-07-23"), "300502", "akshare-test", "spot-test"
    )
    assert quote.quote_type == "latest_close"


def test_akshare_after_close_does_not_claim_realtime():
    now = datetime(2026, 7, 24, 15, 30, tzinfo=SHANGHAI_TZ)
    quote = AKShareProvider(now_fn=lambda: now)._quote_from_frame(
        _spot_frame(), "300502", "akshare-test", "spot-test"
    )
    assert quote.quote_type == "latest_close"


def test_akshare_weekend_does_not_claim_realtime():
    now = datetime(2026, 7, 25, 10, 0, tzinfo=SHANGHAI_TZ)
    quote = AKShareProvider(now_fn=lambda: now)._quote_from_frame(
        _spot_frame(), "300502", "akshare-test", "spot-test"
    )
    assert quote.quote_type == "latest_close"


def test_akshare_latest_close_uses_session_close_time():
    now = datetime(2026, 7, 24, 15, 30, tzinfo=SHANGHAI_TZ)
    quote = AKShareProvider(now_fn=lambda: now)._quote_from_frame(
        _spot_frame(), "300502", "akshare-test", "spot-test"
    )
    assert quote.observed_at == datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ)


def test_akshare_realtime_request_rejects_after_hours_snapshot(session, monkeypatch):
    now = datetime(2026, 7, 24, 15, 30, tzinfo=SHANGHAI_TZ)
    provider = AKShareProvider(now_fn=lambda: now, retries=1)

    class FakeAK:
        @staticmethod
        def stock_zh_a_spot_em():
            return _spot_frame()

    monkeypatch.setattr(provider, "_ak", lambda: FakeAK)
    result = _router(session, now, provider).get_quote("300502")
    assert result.quality_status == DataQualityStatus.MISSING


class FakeTusharePro:
    def __init__(self, trade_date):
        self.trade_date = trade_date

    def daily(self, **_kwargs):
        return pd.DataFrame([{"trade_date": self.trade_date, "close": 10}])


def _tushare(now, trade_date="20260723"):
    provider = TushareProvider(
        Settings(tushare_enabled=True, tushare_token="test"),
        now_fn=lambda: now,
    )
    provider._pro = lambda: FakeTusharePro(trade_date)
    return provider


def test_tushare_quote_is_latest_close_only():
    quote = _tushare(MORNING).get_quote("300502")
    assert quote.quote_type == "latest_close"


def test_tushare_close_observed_at_is_1500():
    quote = _tushare(MORNING).get_quote("300502")
    assert quote.observed_at == datetime(2026, 7, 23, 15, 0, tzinfo=SHANGHAI_TZ)


def test_tushare_old_close_is_stale(session):
    provider = _tushare(MORNING, "20260722")
    result = _router(session, MORNING, provider).get_latest_close("300502")
    assert result.quality_status == DataQualityStatus.STALE


def test_tushare_never_registers_realtime_capability():
    capabilities = _tushare(MORNING).metadata.supported_capabilities
    assert "market.quote.realtime" not in capabilities


def test_tushare_history_is_explicitly_unadjusted():
    provider = _tushare(MORNING)
    provider._pro = lambda: type(
        "HistoryPro",
        (),
        {
            "daily": lambda self, **kwargs: pd.DataFrame(
                [
                    {
                        "trade_date": "20260723",
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10,
                        "vol": 1,
                    }
                ]
            )
        },
    )()
    bar = provider.get_history("300502", date(2026, 7, 23), date(2026, 7, 23))[0]
    assert (bar.adjustment, bar.price_unit, bar.volume_unit) == (
        "unadjusted",
        "CNY",
        "share",
    )


def _professional():
    return ProfessionalMarketApiProvider(
        Settings(
            professional_market_api_enabled=True,
            professional_market_api_url="https://example.test",
            professional_market_api_key="secret",
        )
    )


def _assert_professional_quote_rejects_missing(missing):
    provider = _professional()
    row = {
        "symbol": "300502",
        "price": 10,
        "quote_type": "realtime",
        "observed_at": MORNING.isoformat(),
        "fetched_at": (MORNING + timedelta(seconds=1)).isoformat(),
        "price_unit": "CNY",
    }
    row.pop(missing)
    provider._get = lambda *_args, **_kwargs: row
    with pytest.raises(ProviderUnavailableError):
        provider.get_quote("300502")


def test_professional_quote_requires_explicit_quote_type():
    _assert_professional_quote_rejects_missing("quote_type")


def test_professional_quote_requires_explicit_observed_at():
    _assert_professional_quote_rejects_missing("observed_at")


def test_professional_quote_rejects_latest_close_for_realtime():
    provider = _professional()
    provider._get = lambda *_args, **_kwargs: {
        "symbol": "300502",
        "price": 10,
        "quote_type": "latest_close",
        "observed_at": MORNING.isoformat(),
        "fetched_at": MORNING.isoformat(),
        "price_unit": "CNY",
    }
    with pytest.raises(ProviderUnavailableError):
        provider.get_quote("300502")


def _history_row(**changes):
    row = {
        "symbol": "300502",
        "date": "2026-07-23",
        "open": 10,
        "high": 11,
        "low": 9,
        "close": 10,
        "volume": 100,
        "adjustment": "qfq",
        "price_unit": "CNY",
        "volume_unit": "share",
        "fetched_at": MORNING.isoformat(),
    }
    row.update(changes)
    return row


def _assert_professional_history_rejected(rows):
    provider = _professional()
    provider._get = lambda *_args, **_kwargs: {"rows": rows}
    with pytest.raises(ProviderUnavailableError):
        provider.get_history("300502", date(2026, 7, 23), date(2026, 7, 24))


def test_professional_history_requires_explicit_qfq():
    _assert_professional_history_rejected([_history_row(adjustment=None)])


def test_professional_history_rejects_mixed_adjustment():
    _assert_professional_history_rejected(
        [_history_row(), _history_row(date="2026-07-24", adjustment="unadjusted")]
    )


def test_professional_history_rejects_missing_units():
    _assert_professional_history_rejected([_history_row(price_unit=None)])


def test_professional_history_requires_explicit_fetched_at():
    _assert_professional_history_rejected([_history_row(fetched_at=None)])


@pytest.mark.parametrize(
    "row",
    [
        _history_row(date="not-a-date"),
        _history_row(close="not-a-number"),
        "not-an-object",
    ],
)
def test_professional_history_invalid_fields_use_provider_error(row):
    _assert_professional_history_rejected([row])


def test_professional_history_rejects_symbol_mismatch():
    _assert_professional_history_rejected([_history_row(symbol="600000")])


def _bar(day, **changes):
    values = {
        "symbol": "300502",
        "trade_date": day,
        "open": Decimal("10"),
        "high": Decimal("11"),
        "low": Decimal("9"),
        "close": Decimal("10.5"),
        "volume": Decimal("100"),
        "adjustment": "qfq",
        "price_unit": "CNY",
        "volume_unit": "share",
        "observed_at": datetime.combine(day, datetime.min.time()),
        "source": "test",
        "fetched_at": MORNING,
    }
    values.update(changes)
    return DailyBar(**values)


@pytest.mark.parametrize(
    "bars",
    [
        [_bar(date(2026, 7, 23), adjustment="unadjusted")],
        [_bar(date(2026, 7, 23), price_unit="USD")],
        [_bar(date(2026, 7, 23), symbol="600000")],
        [_bar(date(2026, 7, 23), high=Decimal("9"))],
        [_bar(date(2026, 7, 24)), _bar(date(2026, 7, 23))],
    ],
)
def test_daily_bar_contract_rejects_semantic_mismatch(bars):
    with pytest.raises(ProviderUnavailableError):
        validate_daily_bar_contract(
            bars, capability="market.daily.qfq", expected_symbol="300502"
        )


def _persist_active_realtime_quote(session, observed_at=MORNING):
    router = _router(
        session,
        observed_at,
        QuoteProvider(
            "active",
            _quote(
                observed_at=observed_at,
                fetched_at=observed_at + timedelta(seconds=1),
            ),
        ),
    )
    result = router.get_quote("300502")
    persist_market_quote(session, router, result)
    session.commit()
    return result


def test_realtime_quote_becomes_non_executable_at_lunch(session):
    _persist_active_realtime_quote(session)
    lunch = datetime(2026, 7, 24, 12, 0, tzinfo=SHANGHAI_TZ)
    selected = resolve_cached_quote(
        session,
        symbol="300502",
        capability="market.quote.realtime",
        evaluated_at=lunch,
    )
    assert selected.executable is False
    assert selected.effective_quality.effective_quality == DataQualityStatus.STALE


def test_realtime_quote_becomes_non_executable_after_close(session):
    _persist_active_realtime_quote(session)
    close = datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ)
    selected = resolve_cached_quote(
        session,
        symbol="300502",
        capability="market.quote.realtime",
        evaluated_at=close,
    )
    assert selected.executable is False
    assert selected.effective_quality.effective_quality == DataQualityStatus.STALE


def test_realtime_quote_becomes_non_executable_on_holiday(session):
    prior_session = datetime(2026, 2, 13, 10, 0, tzinfo=SHANGHAI_TZ)
    _persist_active_realtime_quote(session, prior_session)
    holiday = datetime(2026, 2, 20, 10, 0, tzinfo=SHANGHAI_TZ)
    selected = resolve_cached_quote(
        session,
        symbol="300502",
        capability="market.quote.realtime",
        evaluated_at=holiday,
    )
    assert selected.executable is False


def test_latest_close_remains_display_only(session):
    close = datetime(2026, 7, 23, 15, 0, tzinfo=SHANGHAI_TZ)
    provider = QuoteProvider(
        "close",
        _quote(
            quote_type="latest_close",
            observed_at=close,
            fetched_at=MORNING,
        ),
    )
    router = _router(session, MORNING, provider)
    assert router.get_latest_close("300502").quality_status in {
        DataQualityStatus.SINGLE_SOURCE,
        DataQualityStatus.STALE,
    }
    assert router.get_quote("300502").quality_status == DataQualityStatus.MISSING


def test_one_click_does_not_generate_realtime_binding_from_after_hours_spot(
    session, monkeypatch
):
    now = datetime(2026, 7, 24, 15, 30, tzinfo=SHANGHAI_TZ)
    provider = AKShareProvider(now_fn=lambda: now, retries=1)

    class FakeAK:
        @staticmethod
        def stock_zh_a_spot_em():
            return _spot_frame()

    monkeypatch.setattr(provider, "_ak", lambda: FakeAK)
    result = _router(session, now, provider).get_quote("300502")
    assert result.value is None
    assert result.quality_status == DataQualityStatus.MISSING
    assert result.quality_record_id is not None


def test_bound_realtime_quote_is_non_executable_after_close(session):
    _persist_active_realtime_quote(session)
    at_confirmation = datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ)
    binding_selection = resolve_cached_quote(
        session,
        symbol="300502",
        capability="market.quote.realtime",
        evaluated_at=at_confirmation,
    )
    assert binding_selection.executable is False
    assert binding_selection.blocking_reason == "observation_stale"


def test_bound_realtime_quote_is_executable_during_active_session(session):
    result = _persist_active_realtime_quote(session)
    binding_selection = resolve_cached_quote(
        session,
        symbol="300502",
        capability="market.quote.realtime",
        evaluated_at=MORNING + timedelta(minutes=1),
    )
    assert binding_selection.executable is True
    assert binding_selection.quality_record_id == result.quality_record_id
