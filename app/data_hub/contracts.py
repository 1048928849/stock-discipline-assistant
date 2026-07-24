from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any


class ProviderUnavailableError(RuntimeError):
    """A provider cannot return trustworthy data for the current request."""


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
