from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


class ProviderUnavailableError(RuntimeError):
    """外部数据源当前不可用，调用方不得伪造或静默使用过期数据。"""


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: Decimal
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
    source: str
    fetched_at: datetime


class MarketDataProvider(ABC):
    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    @abstractmethod
    def get_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        raise NotImplementedError
