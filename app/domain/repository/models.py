from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class AccountRecord:
    id: int
    total_assets: Decimal
    available_cash: Decimal


@dataclass(frozen=True)
class HoldingRecord:
    symbol: str
    quantity: int
    cost_price: Decimal
    current_price: Decimal
    stop_loss_price: Decimal | None
    target_price: Decimal | None
    sector: str | None


@dataclass(frozen=True)
class CompanyProfileRecord:
    symbol: str
    name: str
    industry: str | None
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class MarketBarRecord:
    symbol: str
    trade_date: date
    close: Decimal
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class MarketQuoteRecord:
    symbol: str
    name: str
    price: Decimal


@dataclass(frozen=True)
class RuleVersionRecord:
    version: str
    parameters: Mapping[str, Any]
    rules: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))
