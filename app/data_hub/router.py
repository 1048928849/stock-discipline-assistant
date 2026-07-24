from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.quality import (
    DataQualityStatus,
    QualityObservation,
    assess_quality,
    canonical_digest,
    observation_is_stale,
    policy_for,
)
from app.data_hub.registry import ProviderRegistry
from app.data_hub.trading_calendar import TradingCalendar, get_trading_calendar
from app.models import DataProviderCallLog, DataQualityRecord


TRUSTED_QUALITY_STATUSES = frozenset(
    {DataQualityStatus.VERIFIED, DataQualityStatus.SINGLE_SOURCE}
)


@dataclass
class ProviderResult:
    value: Any
    provider_id: str
    capability: str
    fetched_at: datetime
    fallback_used: bool
    cache_used: bool
    errors: list[str]
    quality_status: DataQualityStatus
    observed_at: datetime | date | None = None
    provider_observations: list[dict[str, Any]] = field(default_factory=list)
    conflict_fields: list[str] = field(default_factory=list)
    normalized_digest: str | None = None
    adjustment: str | None = None
    price_unit: str | None = None
    volume_unit: str | None = None
    quality_record_id: int | None = None

    def require_value(
        self,
        allowed_quality_statuses: set[DataQualityStatus] | frozenset[DataQualityStatus] | None = None,
    ) -> Any:
        return self.require_trusted_value(allowed_quality_statuses)

    def require_trusted_value(
        self,
        allowed_quality_statuses: set[DataQualityStatus] | frozenset[DataQualityStatus] | None = None,
    ) -> Any:
        if self.value is None:
            detail = "; ".join(self.errors) or f"{self.capability} is missing"
            raise ProviderUnavailableError(detail)
        allowed = allowed_quality_statuses or TRUSTED_QUALITY_STATUSES
        if self.quality_status not in allowed:
            raise ProviderUnavailableError(
                f"{self.capability} quality is {self.quality_status.value}; "
                "trusted business data requires VERIFIED or SINGLE_SOURCE"
            )
        return self.value

    def audit_value(self) -> Any:
        """Return the selected value for diagnostics without asserting trust."""

        return self.value

    def public_meta(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "capability": self.capability,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "fetched_at": self.fetched_at.isoformat(),
            "fallback_used": self.fallback_used,
            "cache_used": self.cache_used,
            "errors": self.errors,
            "quality_status": self.quality_status.value,
            "provider_observations": self.provider_observations,
            "conflict_fields": self.conflict_fields,
            "normalized_digest": self.normalized_digest,
            "adjustment": self.adjustment,
            "price_unit": self.price_unit,
            "volume_unit": self.volume_unit,
            "quality_record_id": self.quality_record_id,
        }


class DataHubRouter:
    """Single runtime route for provider selection, quality, fallback, and audit."""

    def __init__(
        self,
        db: Session,
        registry: ProviderRegistry,
        calendar: TradingCalendar | None = None,
    ):
        self.db = db
        self.registry = registry
        self.calendar = calendar or get_trading_calendar()
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

    @staticmethod
    def _parse_time(value: Any) -> datetime | date | None:
        if isinstance(value, (datetime, date)):
            return value
        if value is None:
            return None
        text = str(value).strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None

    @classmethod
    def _observed_at(
        cls, capability: str, value: Any, fetched_at: datetime
    ) -> datetime | date | None:
        if capability.startswith("market.quote"):
            return getattr(value, "observed_at", None) or fetched_at
        if capability == "fundamental.profile":
            return fetched_at
        if capability in {"market.symbols", "market.indices", "market.sectors"}:
            return fetched_at
        if capability.startswith("announcement."):
            return fetched_at
        if isinstance(value, list):
            if value and hasattr(value[-1], "trade_date"):
                return max(item.trade_date for item in value)
            candidates = []
            for row in value:
                if not isinstance(row, dict):
                    continue
                for key in (
                    "published_at",
                    "published_date",
                    "report_date",
                    "trade_date",
                    "date",
                    "observed_at",
                    "报告日",
                    "公告时间",
                    "公告日期",
                ):
                    parsed = cls._parse_time(row.get(key))
                    if parsed is not None:
                        candidates.append(parsed)
                        break
            if candidates:
                return max(candidates)
            return None
        if isinstance(value, dict):
            if isinstance(value.get("rows"), list):
                return cls._observed_at(capability, value["rows"], fetched_at)
            nested = [
                cls._observed_at(capability, child, fetched_at)
                for child in value.values()
                if isinstance(child, list)
            ]
            nested = [item for item in nested if item is not None]
            if nested:
                return max(nested)
            for key in (
                "observed_at",
                "data_time",
                "trade_date",
                "published_at",
                "date",
            ):
                parsed = cls._parse_time(value.get(key))
                if parsed is not None:
                    return parsed
        return None

    @staticmethod
    def _as_datetime(value: datetime | date | None) -> datetime | None:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo else value
        if isinstance(value, date):
            return datetime.combine(value, datetime_time.min)
        return None

    def _record_quality(self, result: ProviderResult, symbol: str | None) -> ProviderResult:
        latest_content_at = None
        if result.capability == "announcement.catalog" and isinstance(result.value, list):
            candidates = []
            for row in result.value:
                if not isinstance(row, dict):
                    continue
                for key in ("published_at", "published_date", "公告时间", "公告日期", "date"):
                    parsed = self._parse_time(row.get(key))
                    if parsed is not None:
                        candidates.append(self._as_datetime(parsed))
                        break
            latest_content_at = max((item for item in candidates if item), default=None)
        record = DataQualityRecord(
            symbol=symbol,
            capability=result.capability,
            quality_status=result.quality_status.value,
            observed_at=self._as_datetime(result.observed_at),
            fetched_at=result.fetched_at.replace(tzinfo=None)
            if result.fetched_at.tzinfo
            else result.fetched_at,
            provider_id=result.provider_id,
            provider_observations=result.provider_observations,
            normalized_digest=result.normalized_digest,
            conflict_fields=result.conflict_fields,
            adjustment=result.adjustment,
            price_unit=result.price_unit,
            volume_unit=result.volume_unit,
            row_count=self._row_count(result.value),
            fallback_used=result.fallback_used,
            cache_used=result.cache_used,
            trusted=result.quality_status in TRUSTED_QUALITY_STATUSES,
            persisted=False,
            scan_start=result.fetched_at if result.capability == "announcement.catalog" else None,
            scan_end=result.fetched_at if result.capability == "announcement.catalog" else None,
            checked_at=result.fetched_at if result.capability == "announcement.catalog" else None,
            latest_content_at=latest_content_at,
        )
        self.db.add(record)
        self.db.flush()
        result.quality_record_id = record.id
        return result

    def mark_persisted(self, result: ProviderResult, cached_at: datetime | None = None) -> None:
        if result.quality_status not in TRUSTED_QUALITY_STATUSES:
            raise ProviderUnavailableError(
                f"Cannot persist untrusted {result.capability} result "
                f"with quality {result.quality_status.value}"
            )
        record = self.db.get(DataQualityRecord, result.quality_record_id)
        if record is None:
            raise ProviderUnavailableError("Provider result has no persisted quality audit")
        record.persisted = True
        record.cached_at = cached_at or datetime.now()
        self.db.flush()

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
        policy = policy_for(capability)
        errors: list[str] = []
        observations: list[QualityObservation] = []
        audit: list[dict[str, Any]] = []
        configured = [
            provider
            for provider in self.registry.providers_for(capability)
            if provider.metadata.enabled and provider.configured
        ]
        providers = configured
        for index, provider in enumerate(providers):
            started = datetime.now()
            timer = time.perf_counter()
            try:
                value = getattr(provider, operation)(*args, **kwargs)
                if validator and not validator(value):
                    raise ProviderUnavailableError(
                        "Provider data failed capability completeness validation"
                    )
                fetched_at = datetime.now()
                observed_at = self._observed_at(capability, value, fetched_at)
                stale = observation_is_stale(
                    observed_at,
                    policy,
                    now=fetched_at,
                    calendar=self.calendar,
                )
                digest = canonical_digest(value, policy)
                duration_ms = int((time.perf_counter() - timer) * 1000)
                observations.append(
                    QualityObservation(
                        provider_id=provider.provider_id,
                        value=value,
                        observed_at=observed_at,
                        fetched_at=fetched_at,
                        stale=stale,
                        normalized_digest=digest,
                    )
                )
                audit.append(
                    {
                        "provider_id": provider.provider_id,
                        "status": "success",
                        "duration_ms": duration_ms,
                        "observed_at": observed_at.isoformat() if observed_at else None,
                        "fetched_at": fetched_at.isoformat(),
                        "cache_used": False,
                        "fallback_used": index > 0,
                        "stale": stale,
                        "normalized_digest": digest,
                        "error": None,
                    }
                )
                self._log(
                    provider_id=provider.provider_id,
                    capability=capability,
                    operation=operation,
                    symbol=symbol,
                    status="success",
                    started=started,
                    duration_ms=duration_ms,
                    row_count=self._row_count(value),
                    fallback_used=index > 0,
                )
                if not policy.verify_multiple_sources:
                    break
            except Exception as exc:
                duration_ms = int((time.perf_counter() - timer) * 1000)
                detail = f"{provider.provider_id}: {type(exc).__name__}: {str(exc)[:300]}"
                errors.append(detail)
                audit.append(
                    {
                        "provider_id": provider.provider_id,
                        "status": "failed",
                        "duration_ms": duration_ms,
                        "observed_at": None,
                        "fetched_at": datetime.now().isoformat(),
                        "cache_used": False,
                        "fallback_used": index > 0,
                        "stale": False,
                        "normalized_digest": None,
                        "error": detail,
                    }
                )
                self._log(
                    provider_id=provider.provider_id,
                    capability=capability,
                    operation=operation,
                    symbol=symbol,
                    status="failed",
                    started=started,
                    duration_ms=duration_ms,
                    error=detail,
                    fallback_used=index > 0,
                )

        if observations:
            quality = assess_quality(observations, policy)
            fresh = [item for item in observations if not item.stale]
            selected = (fresh or observations)[0]
            result = ProviderResult(
                value=selected.value,
                provider_id=selected.provider_id,
                capability=capability,
                observed_at=selected.observed_at,
                fetched_at=selected.fetched_at or datetime.now(),
                fallback_used=bool(errors) or selected.provider_id != observations[0].provider_id,
                cache_used=False,
                errors=errors,
                quality_status=quality,
                provider_observations=audit,
                conflict_fields=(
                    ["canonical_business_value"]
                    if quality == DataQualityStatus.CONFLICTED
                    else []
                ),
                normalized_digest=selected.normalized_digest,
                adjustment=getattr(selected.value[0], "adjustment", None)
                if isinstance(selected.value, list) and selected.value
                else getattr(selected.value, "adjustment", None),
                price_unit=getattr(selected.value[0], "price_unit", None)
                if isinstance(selected.value, list) and selected.value
                else getattr(selected.value, "price_unit", None),
                volume_unit=getattr(selected.value[0], "volume_unit", None)
                if isinstance(selected.value, list) and selected.value
                else getattr(selected.value, "volume_unit", None),
            )
            result = self._record_quality(result, symbol)
            self.calls[capability] = result
            return result

        if cache_loader and policy.allow_cache_fallback:
            cached = cache_loader()
            if cached is not None and self._row_count(cached) > 0:
                now = datetime.now()
                observed_at = self._observed_at(capability, cached, now)
                digest = canonical_digest(cached, policy)
                audit.append(
                    {
                        "provider_id": "local_cache",
                        "status": "cache_fallback",
                        "duration_ms": 0,
                        "observed_at": observed_at.isoformat() if observed_at else None,
                        "fetched_at": now.isoformat(),
                        "cache_used": True,
                        "fallback_used": True,
                        "stale": True,
                        "normalized_digest": digest,
                        "error": "; ".join(errors)[:2000] or None,
                    }
                )
                result = ProviderResult(
                    value=cached,
                    provider_id="local_cache",
                    capability=capability,
                    observed_at=observed_at,
                    fetched_at=now,
                    fallback_used=True,
                    cache_used=True,
                    errors=errors,
                    quality_status=DataQualityStatus.STALE,
                    provider_observations=audit,
                    normalized_digest=digest,
                )
                result = self._record_quality(result, symbol)
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
                    error="; ".join(errors)[:2000] or None,
                )
                self.calls[capability] = result
                return result

        now = datetime.now()
        result = ProviderResult(
            value=None,
            provider_id="none",
            capability=capability,
            observed_at=None,
            fetched_at=now,
            fallback_used=bool(errors),
            cache_used=False,
            errors=errors,
            quality_status=DataQualityStatus.MISSING,
            provider_observations=audit,
        )
        result = self._record_quality(result, symbol)
        self.calls[capability] = result
        return result

    def get_history(self, symbol: str, start: date, end: date, cache_loader=None):
        return self.invoke(
            "market.daily.qfq",
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
            "market.quote.realtime",
            "get_quote",
            symbol,
            symbol=symbol,
            cache_loader=cache_loader,
            validator=lambda value: value is not None
            and getattr(value, "price", None) is not None,
        )

    def get_latest_close(self, symbol: str, cache_loader=None):
        return self.invoke(
            "market.quote.latest_close",
            "get_quote",
            symbol,
            symbol=symbol,
            cache_loader=cache_loader,
            validator=lambda value: value is not None
            and getattr(value, "price", None) is not None
            and getattr(value, "quote_type", None) == "latest_close",
        )

    def get_unadjusted_history(
        self, symbol: str, start: date, end: date, cache_loader=None
    ):
        return self.invoke(
            "market.daily.unadjusted",
            "get_history",
            symbol,
            start,
            end,
            symbol=symbol,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list)
            and bool(value)
            and all(getattr(item, "adjustment", None) == "unadjusted" for item in value),
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
