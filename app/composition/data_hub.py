from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import TradingCalendar
from app.providers.akshare_provider import AKShareProvider
from app.providers.astock_discovery_provider import AStockDiscoveryProvider
from app.providers.baostock_provider import BaoStockBenchmarkProvider
from app.providers.external_http_provider import (
    ConfiguredNewsApiProvider,
    ProfessionalMarketApiProvider,
)
from app.providers.freestockdb import FreeStockDBProvider
from app.providers.tushare_provider import TushareProvider
from app.providers.x_social_provider import XSocialClueProvider


def build_provider_registry(
    *,
    calendar: TradingCalendar | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> ProviderRegistry:
    settings = get_settings()
    registry = ProviderRegistry()
    registry.register(
        BaoStockBenchmarkProvider(settings, calendar=calendar, now_fn=now_fn)
    )
    registry.register(FreeStockDBProvider(settings, calendar=calendar, now_fn=now_fn))
    registry.register(
        AStockDiscoveryProvider(settings, calendar=calendar, now_fn=now_fn)
    )
    registry.register(
        ProfessionalMarketApiProvider(settings, calendar=calendar, now_fn=now_fn)
    )
    registry.register(TushareProvider(settings, calendar=calendar, now_fn=now_fn))
    registry.register(
        AKShareProvider(
            retries=settings.provider_max_retries,
            timeout=settings.provider_timeout_seconds,
            calendar=calendar,
            now_fn=now_fn,
        )
    )
    registry.register(XSocialClueProvider(settings))
    registry.register(ConfiguredNewsApiProvider(settings))
    return registry


def build_data_hub(
    db: Session,
    registry: ProviderRegistry | None = None,
    *,
    calendar: TradingCalendar | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> DataHubRouter:
    return DataHubRouter(
        db,
        registry=registry
        or build_provider_registry(calendar=calendar, now_fn=now_fn),
        calendar=calendar,
        now_fn=now_fn,
    )


def build_history_data_hub(
    db: Session,
    *,
    calendar: TradingCalendar | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> DataHubRouter:
    settings = get_settings()
    registry = ProviderRegistry()
    registry.register(
        BaoStockBenchmarkProvider(settings, calendar=calendar, now_fn=now_fn)
    )
    registry.register(
        FreeStockDBProvider(settings, calendar=calendar, now_fn=now_fn)
    )
    return DataHubRouter(db, registry=registry, calendar=calendar, now_fn=now_fn)
