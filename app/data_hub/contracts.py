from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from app.data_hub.trading_calendar import SHANGHAI_TZ, TradingCalendar, TradingPhase


class ProviderUnavailableError(RuntimeError):
    """A provider cannot return trustworthy data for the current request."""


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: Decimal
    quote_type: Literal["realtime", "delayed", "latest_close", "cache"]
    observed_at: datetime
    price_unit: str
    source: str
    source_api: str
    fetched_at: datetime


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    adjustment: Literal["qfq", "hfq", "unadjusted"]
    price_unit: str
    volume_unit: str
    observed_at: datetime
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class IntradayBar:
    symbol: str
    trade_date: date
    bar_start: datetime
    bar_end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal | None
    turnover_rate: Decimal | None
    adjustment: Literal["qfq"]
    price_unit: str
    volume_unit: str
    observed_at: datetime
    source: str
    fetched_at: datetime
    completed: bool


@dataclass(frozen=True)
class TurnoverDaily:
    symbol: str
    trade_date: date
    turnover_rate: Decimal
    amount: Decimal | None
    observed_at: datetime
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class MarketBreadthDaily:
    trade_date: date
    advancing: int
    declining: int
    unchanged: int
    limit_up: int
    limit_down: int
    new_highs: int | None
    new_lows: int | None
    median_change_pct: Decimal | None
    above_ma20_ratio: Decimal | None
    above_ma50_ratio: Decimal | None
    observed_at: datetime
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class MarketAmountDaily:
    trade_date: date
    total_amount: Decimal
    observed_at: datetime
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class IndustryDaily:
    industry: str
    trade_date: date
    change_pct: Decimal | None
    amount: Decimal | None
    amount_share: Decimal | None
    advance_ratio: Decimal | None
    limit_up_count: int | None
    leader_strength: Decimal | None
    new_high_ratio: Decimal | None
    observed_at: datetime
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class IndustryConstituent:
    industry: str
    symbol: str
    name: str
    weight: Decimal | None
    observed_at: datetime
    source: str
    fetched_at: datetime
    change_pct: Decimal | None = None
    latest_price: Decimal | None = None
    high_52w: Decimal | None = None
    is_new_high: bool | None = None


@dataclass(frozen=True)
class IndustryCapitalFlow:
    industry_key: str
    industry_name: str
    trade_date: date
    net_inflow_1d: Decimal
    net_inflow_5d: Decimal
    net_inflow_10d: Decimal
    amount: Decimal
    amount_unit: Literal["CNY"]
    source: str
    observed_at: datetime
    fetched_at: datetime


@dataclass(frozen=True)
class MarketPoolEvent:
    symbol: str
    name: str
    trade_date: date
    event_type: Literal["LIMIT_UP", "BROKEN_LIMIT"]
    first_event_at: datetime | None
    last_event_at: datetime | None
    sealed_amount: Decimal | None
    sealed_amount_unit: Literal["CNY"]
    turnover_rate: Decimal | None
    turnover_rate_unit: Literal["percent"]
    consecutive_days: int
    industry_name: str | None
    reason_summary: str | None
    source: str
    observed_at: datetime
    fetched_at: datetime


class ObservedRows(list):
    """List payload that preserves the business observation time when empty."""

    def __init__(
        self,
        rows=(),
        *,
        observed_at: datetime,
        fetched_at: datetime | None = None,
        cache_used: bool = False,
        provider_lineage: dict[str, Any] | None = None,
    ):
        super().__init__(rows)
        self.observed_at = observed_at
        self.fetched_at = fetched_at
        self.cache_used = cache_used
        self.provider_lineage = dict(provider_lineage or {})


@dataclass(frozen=True)
class ProviderMetadata:
    provider_id: str
    supported_capabilities: tuple[str, ...]
    required_credentials: tuple[str, ...] = ()
    enabled: bool = True
    priority: int = 100
    health_status: str = "unknown"
    realtime_supported: bool = False
    timeout: float = 20
    retry: int = 1
    rate_limit: str = "provider_defined"

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class DataProvider(ABC):
    metadata: ProviderMetadata

    @property
    def provider_id(self) -> str:
        return self.metadata.provider_id

    @property
    def configured(self) -> bool:
        return not self.metadata.required_credentials

    def credential_status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "required_credentials": list(self.metadata.required_credentials),
        }

    @abstractmethod
    def health_check(self, probe: bool = False) -> dict[str, Any]:
        raise NotImplementedError


class MarketDataProvider(DataProvider):
    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    @abstractmethod
    def get_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        raise NotImplementedError

    @abstractmethod
    def get_index_history(self, symbol: str, start: date, end: date) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_sector_history(self, industry: str, start: date, end: date) -> dict:
        raise NotImplementedError


class FundamentalDataProvider(DataProvider):
    @abstractmethod
    def company_profile(self, symbol: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def financial_statements(self, symbol: str) -> dict[str, list[dict]]:
        raise NotImplementedError

    @abstractmethod
    def valuation_history(self, symbol: str) -> dict[str, list[dict]]:
        raise NotImplementedError

    @abstractmethod
    def valuation_comparison(self, symbol: str) -> list[dict]:
        raise NotImplementedError


class AnnouncementProvider(DataProvider):
    @abstractmethod
    def company_announcements(self, symbol: str, start: date, end: date) -> list[dict]:
        raise NotImplementedError


class IndustryConceptProvider(DataProvider):
    @abstractmethod
    def company_industry_concepts(self, symbol: str) -> dict:
        raise NotImplementedError


class NewsProvider(DataProvider):
    @abstractmethod
    def company_news(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        raise NotImplementedError


class SocialClueProvider(DataProvider):
    @abstractmethod
    def company_social_clues(
        self, symbol: str, company_name: str | None, start: datetime, end: datetime
    ) -> list[dict]:
        raise NotImplementedError


_FUTURE_TIME_TOLERANCE = timedelta(seconds=1)


def _aware_shanghai_datetime(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderUnavailableError(
            f"market contract requires timezone-aware {field}"
        )
    return value.astimezone(SHANGHAI_TZ)


def _timezone_semantic(value: datetime) -> str:
    return str(getattr(value.tzinfo, "key", None) or value.tzinfo)


def validate_quote_contract(
    quote: Quote,
    *,
    capability: str,
    expected_symbol: str,
    evaluated_at: datetime,
    calendar: TradingCalendar,
) -> None:
    if not isinstance(quote, Quote):
        raise ProviderUnavailableError("quote contract requires a Quote value")
    if quote.symbol != expected_symbol:
        raise ProviderUnavailableError("quote contract symbol mismatch")
    if quote.price_unit != "CNY":
        raise ProviderUnavailableError("quote contract requires CNY price_unit")
    if not isinstance(quote.price, Decimal) or quote.price <= 0:
        raise ProviderUnavailableError("quote contract requires a positive price")
    if not quote.source or not quote.source_api:
        raise ProviderUnavailableError("quote contract requires source lineage")
    observed = _aware_shanghai_datetime(quote.observed_at, "observed_at")
    fetched = _aware_shanghai_datetime(quote.fetched_at, "fetched_at")
    current = _aware_shanghai_datetime(evaluated_at, "evaluated_at")
    if observed > fetched + _FUTURE_TIME_TOLERANCE:
        raise ProviderUnavailableError("quote contract observed_at is after fetched_at")
    if observed > current + _FUTURE_TIME_TOLERANCE:
        raise ProviderUnavailableError("quote contract observed_at is in the future")
    if fetched > current + _FUTURE_TIME_TOLERANCE:
        raise ProviderUnavailableError("quote contract fetched_at is in the future")

    if capability == "market.quote.realtime":
        if quote.quote_type != "realtime":
            raise ProviderUnavailableError("realtime quote contract type mismatch")
        current_phase = calendar.market_phase(current)
        if current_phase not in {
            TradingPhase.MORNING_SESSION,
            TradingPhase.AFTERNOON_SESSION,
        }:
            raise ProviderUnavailableError("realtime quote contract requires active session")
        if observed.date() != current.date():
            raise ProviderUnavailableError("realtime quote contract requires current session")
        if calendar.market_phase(observed) != current_phase:
            raise ProviderUnavailableError(
                "realtime quote contract observed_at is outside current trading window"
            )
        return

    if capability == "market.quote.latest_close":
        if quote.quote_type != "latest_close":
            raise ProviderUnavailableError("latest-close quote contract type mismatch")
        try:
            expected_close = calendar.session_close_at(observed.date())
        except ValueError as exc:
            raise ProviderUnavailableError(
                "latest-close quote contract requires an exchange session"
            ) from exc
        if observed != expected_close:
            raise ProviderUnavailableError(
                "latest-close quote contract requires official session close time"
            )
        if observed.date() > calendar.latest_completed_session(current):
            raise ProviderUnavailableError(
                "latest-close quote contract session is not completed"
            )
        return

    raise ProviderUnavailableError(f"unsupported quote capability contract: {capability}")


def validate_daily_bar_contract(
    bars: Any,
    *,
    capability: str,
    expected_symbol: str,
    evaluated_at: datetime,
    calendar: TradingCalendar,
) -> None:
    expected_adjustment = {
        "market.daily.qfq": "qfq",
        "market.daily.unadjusted": "unadjusted",
    }.get(capability)
    if expected_adjustment is None:
        raise ProviderUnavailableError(
            f"unsupported daily capability contract: {capability}"
        )
    if not isinstance(bars, list) or not bars:
        raise ProviderUnavailableError("daily contract requires a non-empty list")
    current = _aware_shanghai_datetime(evaluated_at, "evaluated_at")
    latest_completed = calendar.latest_completed_session(current)
    dates: list[date] = []
    sources: set[str] = set()
    timezone_semantics: set[tuple[str, str]] = set()
    fetched_times: list[datetime] = []
    for bar in bars:
        if not isinstance(bar, DailyBar):
            raise ProviderUnavailableError("daily contract requires DailyBar rows")
        if not isinstance(bar.trade_date, date) or isinstance(bar.trade_date, datetime):
            raise ProviderUnavailableError("daily contract requires a trade_date")
        if bar.symbol != expected_symbol:
            raise ProviderUnavailableError("daily contract symbol mismatch")
        if bar.adjustment != expected_adjustment:
            raise ProviderUnavailableError("daily contract adjustment mismatch")
        if bar.price_unit != "CNY" or bar.volume_unit != "share":
            raise ProviderUnavailableError("daily contract unit mismatch")
        observed = _aware_shanghai_datetime(bar.observed_at, "observed_at")
        fetched = _aware_shanghai_datetime(bar.fetched_at, "fetched_at")
        timezone_semantics.add(
            (
                _timezone_semantic(bar.observed_at),
                _timezone_semantic(bar.fetched_at),
            )
        )
        try:
            expected_close = calendar.session_close_at(bar.trade_date)
        except ValueError as exc:
            raise ProviderUnavailableError(
                "daily contract trade_date is not an exchange session"
            ) from exc
        if bar.trade_date > latest_completed:
            raise ProviderUnavailableError(
                "daily contract contains an incomplete exchange session"
            )
        if observed != expected_close:
            raise ProviderUnavailableError(
                "daily contract observed_at must equal official session close"
            )
        prices = (bar.open, bar.high, bar.low, bar.close)
        if not all(isinstance(value, Decimal) for value in (*prices, bar.volume)):
            raise ProviderUnavailableError("daily contract requires decimal values")
        if min(prices) <= 0:
            raise ProviderUnavailableError("daily contract requires positive OHLC")
        if bar.high < max(bar.open, bar.close, bar.low):
            raise ProviderUnavailableError("daily contract high is invalid")
        if bar.low > min(bar.open, bar.close, bar.high):
            raise ProviderUnavailableError("daily contract low is invalid")
        if bar.volume < 0:
            raise ProviderUnavailableError("daily contract volume is negative")
        if observed > fetched + _FUTURE_TIME_TOLERANCE:
            raise ProviderUnavailableError("daily contract observed_at is after fetched_at")
        if fetched > current + _FUTURE_TIME_TOLERANCE:
            raise ProviderUnavailableError("daily contract fetched_at is in the future")
        if not bar.source:
            raise ProviderUnavailableError("daily contract requires source lineage")
        dates.append(bar.trade_date)
        sources.add(bar.source)
        fetched_times.append(fetched)
    if len(sources) != 1:
        raise ProviderUnavailableError("daily contract source mismatch")
    if len(timezone_semantics) != 1:
        raise ProviderUnavailableError("daily contract timezone semantics mismatch")
    if max(fetched_times) - min(fetched_times) > _FUTURE_TIME_TOLERANCE:
        raise ProviderUnavailableError("daily contract fetched_at mismatch")
    if dates != sorted(set(dates)):
        raise ProviderUnavailableError(
            "daily contract trade_date must be unique and increasing"
        )
