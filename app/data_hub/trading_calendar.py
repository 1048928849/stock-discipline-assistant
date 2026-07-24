from __future__ import annotations

from datetime import date, datetime, time
from functools import lru_cache
from typing import Protocol
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class TradingCalendar(Protocol):
    def latest_completed_session(self, now: datetime | None = None) -> date: ...

    def session_lag(
        self, observed: date, now: datetime | None = None
    ) -> int: ...


class XSHGTradingCalendar:
    def __init__(self) -> None:
        self._calendar = xcals.get_calendar("XSHG")

    def latest_completed_session(self, now: datetime | None = None) -> date:
        current = now or datetime.now(SHANGHAI_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=SHANGHAI_TZ)
        else:
            current = current.astimezone(SHANGHAI_TZ)
        day = pd.Timestamp(current.date())
        session = self._calendar.date_to_session(day, direction="previous")
        if (
            session.date() == current.date()
            and current.time() < time(15, 30)
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
