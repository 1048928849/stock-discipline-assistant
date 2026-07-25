from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from functools import lru_cache
from typing import Protocol
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


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


class XSHGTradingCalendar:
    def __init__(self) -> None:
        self._calendar = xcals.get_calendar("XSHG")

    @staticmethod
    def _shanghai_time(value: datetime | None = None) -> datetime:
        current = value or datetime.now(SHANGHAI_TZ)
        if current.tzinfo is None:
            return current.replace(tzinfo=SHANGHAI_TZ)
        return current.astimezone(SHANGHAI_TZ)

    def _is_session(self, day: date) -> bool:
        return bool(self._calendar.is_session(pd.Timestamp(day)))

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
