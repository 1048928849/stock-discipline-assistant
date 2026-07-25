"""Compatibility imports for provider adapters outside the Data Hub package."""

from app.data_hub.contracts import (
    AnnouncementProvider,
    DataProvider,
    FundamentalDataProvider,
    IndustryConceptProvider,
    MarketDataProvider,
    NewsProvider,
    ProviderMetadata,
    SocialClueProvider,
)

__all__ = [
    "AnnouncementProvider",
    "DataProvider",
    "FundamentalDataProvider",
    "IndustryConceptProvider",
    "MarketDataProvider",
    "NewsProvider",
    "ProviderMetadata",
    "SocialClueProvider",
]
