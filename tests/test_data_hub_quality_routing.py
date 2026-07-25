from datetime import date, datetime, timezone
from decimal import Decimal

from app.data_hub.contracts import DataProvider, ProviderMetadata, Quote
from app.data_hub.market_subjects import (
    index_daily_subject,
    sector_daily_subject,
    stock_quote_subject,
)
from app.data_hub.quality import (
    DataQualityStatus,
    QualityObservation,
    assess_quality,
    canonical_digest,
    policy_for,
)
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import XSHGTradingCalendar


class StubProvider(DataProvider):
    def __init__(
        self,
        provider_id,
        capability,
        value=None,
        *,
        priority=10,
        error=None,
    ):
        self.value = value
        self.error = error
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=(capability,),
            priority=priority,
        )

    def health_check(self, probe=False):
        return {"status": "healthy"}

    def fetch(self):
        if self.error:
            raise self.error
        return self.value


def router_for(session, capability, *providers):
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return DataHubRouter(session, registry=registry), capability


def test_data_hub_real_router_verified(session):
    now = datetime.now()
    first = Quote(
        "300502", "test", Decimal("10.00"), "realtime", now, "CNY", "a", "a", now
    )
    second = Quote(
        "300502", "test", Decimal("10.0"), "realtime", now, "CNY", "b", "b", now
    )
    router, capability = router_for(
        session,
        "market.quote.realtime",
        StubProvider("a", "market.quote.realtime", first),
        StubProvider("b", "market.quote.realtime", second, priority=20),
    )
    result = router.invoke(
        capability,
        "fetch",
        symbol="300502",
        subject=stock_quote_subject("300502", "realtime", "CNY"),
    )
    assert result.quality_status == DataQualityStatus.VERIFIED
    assert len(result.provider_observations) == 2


def test_data_hub_real_router_conflicted(session):
    now = datetime.now()
    router, capability = router_for(
        session,
        "market.quote.realtime",
        StubProvider(
            "a",
            "market.quote.realtime",
            Quote("300502", "test", Decimal("10"), "realtime", now, "CNY", "a", "a", now),
        ),
        StubProvider(
            "b",
            "market.quote.realtime",
            Quote("300502", "test", Decimal("11"), "realtime", now, "CNY", "b", "b", now),
            priority=20,
        ),
    )
    result = router.invoke(
        capability,
        "fetch",
        symbol="300502",
        subject=stock_quote_subject("300502", "realtime", "CNY"),
    )
    assert result.quality_status == DataQualityStatus.CONFLICTED
    assert result.conflict_fields


def test_data_hub_single_valid_source(session):
    now = datetime.now()
    router, capability = router_for(
        session,
        "market.quote.realtime",
        StubProvider(
            "a",
            "market.quote.realtime",
            Quote("300502", "test", Decimal("10"), "realtime", now, "CNY", "a", "a", now),
        ),
        StubProvider(
            "b",
            "market.quote.realtime",
            error=RuntimeError("offline"),
            priority=20,
        ),
    )
    result = router.invoke(
        capability,
        "fetch",
        symbol="300502",
        subject=stock_quote_subject("300502", "realtime", "CNY"),
    )
    assert result.quality_status == DataQualityStatus.SINGLE_SOURCE
    assert result.errors


def test_data_hub_only_stale_sources(session):
    old_rows = {
        "rows": [
            {"date": date(2020, 1, 2), "close": 10, "volume": 100}
            for _ in range(20)
        ]
    }
    router, capability = router_for(
        session,
        "market.index_daily",
        StubProvider("a", "market.index_daily", old_rows),
        StubProvider("b", "market.index_daily", old_rows, priority=20),
    )
    result = router.invoke(
        capability,
        "fetch",
        subject=index_daily_subject("000300", "unadjusted", "CNY", "share"),
    )
    assert result.quality_status == DataQualityStatus.STALE


def test_data_hub_missing_when_all_providers_fail(session):
    router, capability = router_for(
        session,
        "market.quote",
        StubProvider("a", "market.quote", error=RuntimeError("offline")),
        StubProvider("b", "market.quote", error=RuntimeError("offline"), priority=20),
    )
    result = router.invoke(capability, "fetch")
    assert result.value is None
    assert result.quality_status == DataQualityStatus.MISSING


def test_decimal_and_float_equivalent_values_do_not_conflict():
    policy = policy_for("market.quote")
    observations = [
        QualityObservation("a", {"symbol": "300502", "price": Decimal("10.00")}),
        QualityObservation("b", {"symbol": "300502.SZ", "price": 10.0}),
    ]
    assert assess_quality(observations, policy) == DataQualityStatus.VERIFIED


def test_equivalent_timezone_datetimes_do_not_conflict():
    policy = policy_for("fundamental.profile")
    utc = datetime(2026, 7, 24, 4, tzinfo=timezone.utc)
    cst = datetime.fromisoformat("2026-07-24T12:00:00+08:00")
    assert canonical_digest({"observed_at": utc}, policy) == canonical_digest(
        {"observed_at": cst}, policy
    )


def test_provider_metadata_does_not_cause_false_conflict():
    policy = policy_for("market.quote")
    first = {"symbol": "300502", "price": 10, "source": "a", "fetched_at": "x"}
    second = {"symbol": "300502", "price": 10, "source": "b", "fetched_at": "y"}
    assert canonical_digest(first, policy) == canonical_digest(second, policy)


def test_stale_index_rows_rejected_by_trading_calendar(session):
    rows = {
        "rows": [
            {"date": date(2020, 1, 2), "close": 10, "volume": 100}
            for _ in range(20)
        ]
    }
    router, capability = router_for(
        session,
        "market.index_daily",
        StubProvider("a", "market.index_daily", rows),
    )
    assert (
        router.invoke(
            capability,
            "fetch",
            subject=index_daily_subject("000300", "unadjusted", "CNY", "share"),
        ).quality_status
        == DataQualityStatus.STALE
    )


def test_stale_sector_rows_rejected_by_trading_calendar(session):
    rows = {
        "rows": [
            {"date": date(2020, 1, 2), "close": 10, "volume": 100}
            for _ in range(20)
        ]
    }
    router, capability = router_for(
        session,
        "market.sector_daily",
        StubProvider("a", "market.sector_daily", rows),
    )
    assert (
        router.invoke(
            capability,
            "fetch",
            subject=sector_daily_subject("bank", "unadjusted", "CNY", "share"),
        ).quality_status
        == DataQualityStatus.STALE
    )


def test_weekend_does_not_make_friday_bar_stale():
    calendar = XSHGTradingCalendar()
    saturday = datetime(2026, 7, 25, 12, 0)
    assert calendar.session_lag(date(2026, 7, 24), saturday) == 0


def test_exchange_holiday_does_not_make_previous_bar_stale():
    calendar = XSHGTradingCalendar()
    holiday = datetime(2026, 2, 20, 12, 0)
    assert calendar.session_lag(date(2026, 2, 13), holiday) == 0
