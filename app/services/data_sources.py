from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import DataProviderCallLog
from app.providers.akshare_provider import AKShareProvider
from app.providers.external_http_provider import (
    ConfiguredNewsApiProvider,
    ProfessionalMarketApiProvider,
)
from app.providers.market import ProviderUnavailableError
from app.providers.registry import ProviderRegistry
from app.providers.tushare_provider import TushareProvider
from app.providers.x_social_provider import XSocialClueProvider


@dataclass
class ProviderResult:
    value: Any
    provider_id: str
    capability: str
    fetched_at: datetime
    fallback_used: bool
    cache_used: bool
    errors: list[str]

    def public_meta(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "capability": self.capability,
            "fetched_at": self.fetched_at.isoformat(),
            "fallback_used": self.fallback_used,
            "cache_used": self.cache_used,
            "errors": self.errors,
        }


def build_provider_registry() -> ProviderRegistry:
    settings = get_settings()
    registry = ProviderRegistry()
    registry.register(ProfessionalMarketApiProvider(settings))
    registry.register(TushareProvider(settings))
    registry.register(
        AKShareProvider(
            retries=settings.provider_max_retries,
            timeout=settings.provider_timeout_seconds,
        )
    )
    registry.register(XSocialClueProvider(settings))
    registry.register(ConfiguredNewsApiProvider(settings))
    return registry


class UnifiedDataService:
    """业务层唯一外部数据入口，负责路由、失败回退、缓存回退和审计日志。"""

    def __init__(self, db: Session, registry: ProviderRegistry | None = None):
        self.db = db
        self.registry = registry or build_provider_registry()
        self.calls: dict[str, ProviderResult] = {}

    @staticmethod
    def _row_count(value: Any) -> int:
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            if isinstance(value.get("rows"), list):
                return len(value["rows"])
            return sum(len(item) for item in value.values() if isinstance(item, list))
        return 1 if value is not None else 0

    def _log(
        self,
        *,
        provider_id: str,
        capability: str,
        operation: str,
        symbol: str | None,
        status: str,
        started: datetime,
        duration_ms: int,
        error: str | None = None,
        row_count: int = 0,
        fallback_used: bool = False,
        cache_used: bool = False,
    ) -> None:
        self.db.add(
            DataProviderCallLog(
                provider_id=provider_id,
                capability=capability,
                operation=operation,
                symbol=symbol,
                status=status,
                fallback_used=fallback_used,
                cache_used=cache_used,
                row_count=row_count,
                duration_ms=duration_ms,
                error=error,
                requested_at=started,
                completed_at=datetime.now(),
            )
        )
        self.db.flush()

    def invoke(
        self,
        capability: str,
        operation: str,
        *args,
        symbol: str | None = None,
        cache_loader: Callable[[], Any] | None = None,
        validator: Callable[[Any], bool] | None = None,
        **kwargs,
    ) -> ProviderResult:
        errors: list[str] = []
        attempted = 0
        for provider in self.registry.providers_for(capability):
            if not provider.metadata.enabled:
                continue
            if not provider.configured:
                continue
            attempted += 1
            started = datetime.now()
            timer = time.perf_counter()
            try:
                value = getattr(provider, operation)(*args, **kwargs)
                if validator and not validator(value):
                    raise ProviderUnavailableError("返回数据未通过完整性检查")
                result = ProviderResult(
                    value=value,
                    provider_id=provider.provider_id,
                    capability=capability,
                    fetched_at=datetime.now(),
                    fallback_used=attempted > 1,
                    cache_used=False,
                    errors=errors.copy(),
                )
                self._log(
                    provider_id=provider.provider_id,
                    capability=capability,
                    operation=operation,
                    symbol=symbol,
                    status="success",
                    started=started,
                    duration_ms=int((time.perf_counter() - timer) * 1000),
                    row_count=self._row_count(value),
                    fallback_used=attempted > 1,
                )
                self.calls[capability] = result
                return result
            except Exception as exc:
                detail = f"{provider.provider_id}: {type(exc).__name__}: {str(exc)[:300]}"
                errors.append(detail)
                self._log(
                    provider_id=provider.provider_id,
                    capability=capability,
                    operation=operation,
                    symbol=symbol,
                    status="failed",
                    started=started,
                    duration_ms=int((time.perf_counter() - timer) * 1000),
                    error=detail,
                    fallback_used=attempted > 1,
                )
        if cache_loader:
            cached = cache_loader()
            if cached is not None and self._row_count(cached) > 0:
                now = datetime.now()
                result = ProviderResult(
                    value=cached,
                    provider_id="local_cache",
                    capability=capability,
                    fetched_at=now,
                    fallback_used=True,
                    cache_used=True,
                    errors=errors,
                )
                self._log(
                    provider_id="local_cache",
                    capability=capability,
                    operation=operation,
                    symbol=symbol,
                    status="cache_fallback",
                    started=now,
                    duration_ms=0,
                    row_count=self._row_count(cached),
                    fallback_used=True,
                    cache_used=True,
                    error="；".join(errors)[:2000] or None,
                )
                self.calls[capability] = result
                return result
        suffix = "；".join(errors) if errors else "没有已启用且凭据完整的Provider"
        raise ProviderUnavailableError(f"{capability}不可用：{suffix}")

    def get_history(self, symbol: str, start: date, end: date, cache_loader=None):
        return self.invoke(
            "market.daily",
            "get_history",
            symbol,
            start,
            end,
            symbol=symbol,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list) and len(value) >= 1,
        )

    def get_quote(self, symbol: str, cache_loader=None):
        return self.invoke(
            "market.quote",
            "get_quote",
            symbol,
            symbol=symbol,
            cache_loader=cache_loader,
            validator=lambda value: value is not None and getattr(value, "price", None) is not None,
        )

    def get_index_history(self, symbol: str, start: date, end: date, cache_loader=None):
        return self.invoke(
            "market.index_daily",
            "get_index_history",
            symbol,
            start,
            end,
            symbol=symbol,
            cache_loader=cache_loader,
            validator=lambda value: bool(value and len(value.get("rows", [])) >= 20),
        )

    def get_sector_history(self, industry: str, start: date, end: date, cache_loader=None):
        return self.invoke(
            "market.sector_daily",
            "get_sector_history",
            industry,
            start,
            end,
            symbol=None,
            cache_loader=cache_loader,
            validator=lambda value: bool(value and len(value.get("rows", [])) >= 20),
        )

    def company_profile(self, symbol: str, cache_loader=None):
        return self.invoke(
            "fundamental.profile",
            "company_profile",
            symbol,
            symbol=symbol,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, dict) and bool(value),
        )

    def financial_statements(self, symbol: str, cache_loader=None):
        return self.invoke(
            "fundamental.statements",
            "financial_statements",
            symbol,
            symbol=symbol,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, dict)
            and any(value.get(name) for name in ("balance", "income", "cash_flow")),
        )

    def company_announcements(self, symbol: str, start: date, end: date, cache_loader=None):
        return self.invoke(
            "announcement.catalog",
            "company_announcements",
            symbol,
            start,
            end,
            symbol=symbol,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list),
        )

    def valuation_history(self, symbol: str, cache_loader=None):
        return self.invoke(
            "fundamental.valuation",
            "valuation_history",
            symbol,
            symbol=symbol,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, dict) and any(value.values()),
        )

    def valuation_comparison(self, symbol: str, cache_loader=None):
        return self.invoke(
            "fundamental.valuation",
            "valuation_comparison",
            symbol,
            symbol=symbol,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list),
        )

    def provider_status(self) -> list[dict]:
        return self.registry.public_status(probe=False)

    def list_symbols(self):
        return self.invoke("market.symbols", "list_symbols")

    def list_indices(self, family: str):
        return self.invoke("market.indices", "list_indices", family)

    def list_sectors(self):
        return self.invoke("market.sectors", "list_sectors")

    def daily_announcements(self, symbol: str, day: date):
        return self.invoke(
            "announcement.daily", "announcements", symbol, day, symbol=symbol
        )

    def collect_social_queries(self, queries: list[str], limit: int = 20):
        return self.invoke(
            "social.company_clues",
            "search",
            queries,
            limit,
            validator=lambda value: isinstance(value, list),
        )

    def record_cache_fallback(
        self, capability: str, operation: str, symbol: str, row_count: int, errors: str
    ) -> None:
        now = datetime.now()
        self._log(
            provider_id="local_cache",
            capability=capability,
            operation=operation,
            symbol=symbol,
            status="cache_fallback",
            started=now,
            duration_ms=0,
            row_count=row_count,
            fallback_used=True,
            cache_used=True,
            error=errors[:2000],
        )
