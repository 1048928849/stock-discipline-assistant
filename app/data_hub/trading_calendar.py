from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import Enum
from functools import lru_cache
from typing import Protocol
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class TimeStorageSemantics(str, Enum):
    MARKET_SHANGHAI_NAIVE = "market_shanghai_naive"
    UTC_NAIVE = "utc_naive"


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def shanghai_today() -> date:
    return shanghai_now().date()


def to_shanghai_aware(
    value: datetime,
    *,
    naive_is_shanghai: bool = False,
) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("value must be a datetime")
    if value.tzinfo is None:
        if not naive_is_shanghai:
            raise ValueError("naive datetime requires explicit Shanghai wall-time semantics")
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def to_storage_naive(
    value: datetime,
    *,
    naive_is_shanghai: bool = False,
) -> datetime:
    return to_shanghai_aware(
        value,
        naive_is_shanghai=naive_is_shanghai,
    ).replace(tzinfo=None)


def to_market_storage_naive(
    value: datetime,
    *,
    naive_is_shanghai: bool = False,
) -> datetime:
    return to_storage_naive(value, naive_is_shanghai=naive_is_shanghai)


def to_utc_storage_naive(
    value: datetime,
    *,
    naive_is_utc: bool = False,
) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("value must be a datetime")
    if value.tzinfo is None:
        if not naive_is_utc:
            raise ValueError("naive datetime requires explicit UTC storage semantics")
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def storage_naive_to_aware(
    value: datetime,
    *,
    semantics: TimeStorageSemantics,
) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("value must be a datetime")
    if value.tzinfo is not None:
        return value.astimezone(SHANGHAI_TZ)
    if semantics == TimeStorageSemantics.MARKET_SHANGHAI_NAIVE:
        return value.replace(tzinfo=SHANGHAI_TZ)
    if semantics == TimeStorageSemantics.UTC_NAIVE:
        return value.replace(tzinfo=timezone.utc).astimezone(SHANGHAI_TZ)
    raise ValueError(f"unsupported time storage semantics: {semantics}")


def market_storage_naive_to_aware(value: datetime) -> datetime:
    return storage_naive_to_aware(
        value,
        semantics=TimeStorageSemantics.MARKET_SHANGHAI_NAIVE,
    )


def utc_storage_naive_to_aware(value: datetime) -> datetime:
    return storage_naive_to_aware(
        value,
        semantics=TimeStorageSemantics.UTC_NAIVE,
    )


def time_storage_semantics_for_capability(
    capability: str,
) -> TimeStorageSemantics:
    return (
        TimeStorageSemantics.MARKET_SHANGHAI_NAIVE
        if capability.startswith("market.")
        else TimeStorageSemantics.UTC_NAIVE
    )


class TradingPhase(str, Enum):
    PRE_OPEN = "PRE_OPEN"
    MORNING_SESSION = "MORNING_SESSION"
    LUNCH_BREAK = "LUNCH_BREAK"
    AFTERNOON_SESSION = "AFTERNOON_SESSION"
    CLOSED = "CLOSED"
    NON_TRADING_DAY = "NON_TRADING_DAY"


class TradingCalendar(Protocol):
    def market_phase(self, now: datetime | None = None) -> TradingPhase: ...

    def is_realtime_session(self, now: datetime | None = None) -> bool: ...

    def session_close_at(self, trade_date: date) -> datetime: ...

    def latest_completed_session(self, now: datetime | None = None) -> date: ...

    def session_lag(
        self, observed: date, now: datetime | None = None
    ) -> int: ...


def resolve_analysis_trade_date(
    calendar: TradingCalendar,
    evaluated_at: datetime,
) -> date:
    """Resolve the last fully completed market session for a formal analysis."""
    return calendar.latest_completed_session(to_shanghai_aware(evaluated_at))


class XSHGTradingCalendar:
    def __init__(self) -> None:
        self._calendar = xcals.get_calendar("XSHG")

    @staticmethod
    def _shanghai_time(value: datetime | None = None) -> datetime:
        return to_shanghai_aware(
            value or shanghai_now(),
            naive_is_shanghai=value is not None and value.tzinfo is None,
        )

    def _is_session(self, day: date) -> bool:
        return bool(self._calendar.is_session(pd.Timestamp(day)))

    def is_session(self, day: date) -> bool:
        return self._is_session(day)

    def market_phase(self, now: datetime | None = None) -> TradingPhase:
        """Return the A-share phase using half-open continuous sessions."""
        current = self._shanghai_time(now)
        if not self._is_session(current.date()):
            return TradingPhase.NON_TRADING_DAY
        local_time = current.time().replace(tzinfo=None)
        if local_time < time(9, 30):
            return TradingPhase.PRE_OPEN
        if local_time < time(11, 30):
            return TradingPhase.MORNING_SESSION
        if local_time < time(13, 0):
            return TradingPhase.LUNCH_BREAK
        if local_time < time(15, 0):
            return TradingPhase.AFTERNOON_SESSION
        return TradingPhase.CLOSED

    def is_realtime_session(self, now: datetime | None = None) -> bool:
        return self.market_phase(now) in {
            TradingPhase.MORNING_SESSION,
            TradingPhase.AFTERNOON_SESSION,
        }

    def session_close_at(self, trade_date: date) -> datetime:
        if not self._is_session(trade_date):
            raise ValueError(f"{trade_date.isoformat()} is not an exchange session")
        return datetime.combine(trade_date, time(15, 0), tzinfo=SHANGHAI_TZ)

    def latest_completed_session(self, now: datetime | None = None) -> date:
        current = self._shanghai_time(now)
        day = pd.Timestamp(current.date())
        session = self._calendar.date_to_session(day, direction="previous")
        if (
            session.date() == current.date()
            and current.time() < time(15, 0)
        ):
            session = self._calendar.previous_session(session)
        return session.date()

    def session_lag(
        self, observed: date, now: datetime | None = None
    ) -> int:
        expected = self.latest_completed_session(now)
        if observed >= expected:
            return 0
        sessions = self._calendar.sessions_in_range(
            pd.Timestamp(observed), pd.Timestamp(expected)
        )
        return max(0, len(sessions) - 1)


@lru_cache(maxsize=1)
def get_trading_calendar() -> XSHGTradingCalendar:
    return XSHGTradingCalendar()
