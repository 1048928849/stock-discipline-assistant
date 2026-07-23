from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo


class ProviderUnavailableError(RuntimeError):
    """外部数据源当前不可用，调用方不得伪造或静默使用过期数据。"""


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def infer_a_share_market_status(at: datetime | None = None) -> str:
    """仅描述交易时段，不猜测节假日；Provider可用更准确的状态覆盖。"""

    current = at or shanghai_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    else:
        current = current.astimezone(SHANGHAI_TZ)
    if current.weekday() >= 5:
        return "closed"
    clock = current.time()
    if clock < time(9, 15):
        return "pre_open"
    if time(9, 15) <= clock < time(9, 30):
        return "auction"
    if time(9, 30) <= clock < time(11, 30) or time(13) <= clock < time(15):
        return "trading"
    if time(11, 30) <= clock < time(13):
        return "break"
    return "closed"


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: Decimal
    source: str
    source_api: str
    fetched_at: datetime
    previous_close: Decimal | None = None
    trading_date: date | None = None
    quote_time: datetime | None = None
    market_status: str = "unknown"
    price_type: str = "intraday_snapshot"
    provider_id: str | None = None
    data_as_of: datetime | None = None


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str
    fetched_at: datetime
    frequency: str = "daily"
    adjustment: str = "qfq"
    price_type: str = "official_close"
    provider_id: str | None = None
    data_as_of: datetime | None = None


class MarketDataProvider(ABC):
    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    @abstractmethod
    def get_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        raise NotImplementedError
