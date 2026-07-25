"""Compatibility imports for the legacy provider module path."""

from app.data_hub.contracts import (
    DailyBar,
    MarketDataProvider,
    ProviderUnavailableError,
    Quote,
)

__all__ = ["DailyBar", "MarketDataProvider", "ProviderUnavailableError", "Quote"]
