from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.data_hub.contracts import (
    IntradayBar,
    ProviderUnavailableError,
    validate_daily_bar_contract,
    validate_quote_contract,
)
from app.data_hub.market_subjects import (
    company_concepts_subject,
    company_industry_chain_subject,
    index_daily_subject,
    industry_constituents_subject,
    market_amount_subject,
    market_breadth_subject,
    sector_daily_subject,
    stock_daily_subject,
    stock_intraday_subject,
    stock_quote_subject,
    stock_turnover_subject,
)
from app.data_hub.research_subjects import (
    announcement_catalog_subject,
    company_profile_subject,
)
from app.data_hub.quality import (
    DataQualityStatus,
    QualityObservation,
    assess_quality,
    canonical_digest,
    observation_is_stale,
    policy_for,
)
from app.data_hub.registry import ProviderRegistry
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    shanghai_now,
    storage_naive_to_aware,
    time_storage_semantics_for_capability,
    to_market_storage_naive,
    to_shanghai_aware,
    to_utc_storage_naive,
)
from app.domain.quality_subject import SubjectRef, canonical_semantic_key
from app.models import (
    DataProviderCallLog,
    DataQualityRecord,
    DataQualitySubjectHead,
)


TRUSTED_QUALITY_STATUSES = frozenset(
    {DataQualityStatus.VERIFIED, DataQualityStatus.SINGLE_SOURCE}
)
MARKET_SUBJECT_CAPABILITIES = frozenset(
    {
        "market.quote.realtime",
        "market.quote.latest_close",
        "market.daily.qfq",
        "market.daily.unadjusted",
        "market.index_daily",
        "market.sector_daily",
        "market.intraday.60m",
        "market.turnover.daily",
        "market.breadth.daily",
        "market.amount.daily",
        "market.industry.daily",
        "market.industry.constituents",
    }
)
RESEARCH_SUBJECT_CAPABILITIES = frozenset(
    {
        "fundamental.profile",
        "announcement.catalog",
        "company.concepts",
        "company.industry_chain",
    }
)
CallResultKey = tuple[str, str, str, str, str, str]


def _normalized_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _normalize_request_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return {"$decimal": _normalized_decimal(value)}
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        normalized = normalized.astimezone(timezone.utc)
        return {"$datetime": normalized.isoformat().replace("+00:00", "Z")}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return [_normalize_request_value(item) for item in value]
    if isinstance(value, dict):
        normalized = {}
        for key, child in value.items():
            normalized_key = str(key).strip()
            if normalized_key in normalized:
                raise ValueError("duplicate_normalized_request_key")
            normalized[normalized_key] = _normalize_request_value(child)
        return {key: normalized[key] for key in sorted(normalized)}
    raise TypeError(f"unsupported request identity value: {type(value).__name__}")


def _request_identity(
    *,
    capability: str,
    operation: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    symbol: str | None,
) -> tuple[str, dict[str, Any]]:
    summary = {
        "capability": capability.strip(),
        "operation": operation.strip(),
        "args": _normalize_request_value(args),
        "kwargs": _normalize_request_value(kwargs),
        "symbol": _normalize_request_value(symbol),
    }
    encoded = json.dumps(
        summary,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), summary


def request_fingerprint(
    *,
    capability: str,
    operation: str,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    symbol: str | None = None,
) -> str:
    fingerprint, _ = _request_identity(
        capability=capability,
        operation=operation,
        args=args,
        kwargs=kwargs or {},
        symbol=symbol,
    )
    return fingerprint


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
    subject: SubjectRef | None = None
    operation: str = ""
    request_fingerprint: str = ""
    request_summary: dict[str, Any] = field(default_factory=dict)
    scan_start: datetime | None = None
    scan_end: datetime | None = None
    checked_at: datetime | None = None
    latest_content_at: datetime | None = None

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
            "subject_type": self.subject.subject_type if self.subject else None,
            "subject_id": self.subject.subject_id if self.subject else None,
            "semantic_key": self.subject.semantic_key if self.subject else None,
            "operation": self.operation,
            "request_fingerprint": self.request_fingerprint,
            "scan_start": self.scan_start.isoformat() if self.scan_start else None,
            "scan_end": self.scan_end.isoformat() if self.scan_end else None,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "latest_content_at": (
                self.latest_content_at.isoformat() if self.latest_content_at else None
            ),
        }


class DataHubRouter:
    """Single runtime route for provider selection, quality, fallback, and audit."""

    def __init__(
        self,
        db: Session,
        registry: ProviderRegistry,
        calendar: TradingCalendar | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.db = db
        self.registry = registry
        self.calendar = calendar or get_trading_calendar()
        self.now_fn = now_fn or shanghai_now
        self.calls: dict[str, ProviderResult] = {}
        self.call_results: dict[CallResultKey, ProviderResult] = {}

    def _now(self) -> datetime:
        value = self.now_fn()
        return to_shanghai_aware(
            value,
            naive_is_shanghai=value.tzinfo is None,
        )

    @staticmethod
    def _call_result_key(
        capability: str,
        operation: str,
        subject: SubjectRef | None,
        request_fingerprint: str,
    ) -> CallResultKey:
        return (
            capability,
            operation,
            subject.subject_type if subject else "",
            subject.subject_id if subject else "",
            canonical_semantic_key(subject.semantic_key) if subject else "",
            request_fingerprint,
        )

    def result_for(
        self,
        capability: str,
        operation: str,
        subject: SubjectRef | None,
        request_fingerprint: str,
    ) -> ProviderResult | None:
        return self.call_results.get(
            self._call_result_key(
                capability,
                operation,
                subject,
                request_fingerprint,
            )
        )

    def _remember_result(self, result: ProviderResult) -> ProviderResult:
        self.calls[result.capability] = result
        self.call_results[
            self._call_result_key(
                result.capability,
                result.operation,
                result.subject,
                result.request_fingerprint,
            )
        ] = result
        return result

    @staticmethod
    def _row_count(value: Any) -> int:
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            if isinstance(value.get("rows"), list):
                return len(value["rows"])
            return sum(len(item) for item in value.values() if isinstance(item, list))
        return 1 if value is not None else 0

    @classmethod
    def payload_row_count(cls, capability: str, value: Any) -> int:
        """Return the business row count used by quality audit and persistence."""
        if capability == "fundamental.profile":
            return 1 if isinstance(value, dict) and bool(value) else 0
        if capability == "announcement.catalog":
            return len(value) if isinstance(value, list) else cls._row_count(value)
        return cls._row_count(value)

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
            return getattr(value, "observed_at", None)
        if capability == "fundamental.profile":
            return fetched_at
        if capability in {"market.symbols", "market.indices", "market.sectors"}:
            return fetched_at
        if capability.startswith("announcement."):
            return fetched_at
        if isinstance(value, list):
            structured_times = [
                item.observed_at
                for item in value
                if isinstance(getattr(item, "observed_at", None), (datetime, date))
            ]
            if structured_times:
                return max(structured_times)
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
    def _as_datetime(
        value: datetime | date | None,
        *,
        market_time: bool = False,
    ) -> datetime | None:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value
            if market_time:
                return to_market_storage_naive(value)
            return to_utc_storage_naive(value)
        if isinstance(value, date):
            return datetime.combine(value, datetime_time.min)
        return None

    @staticmethod
    def _result_dimensions(
        value: Any,
        subject: SubjectRef | None,
    ) -> tuple[str | None, str | None, str | None]:
        sample = value[0] if isinstance(value, list) and value else value
        adjustment = getattr(sample, "adjustment", None)
        price_unit = getattr(sample, "price_unit", None)
        volume_unit = getattr(sample, "volume_unit", None)
        semantic_parts = (
            canonical_semantic_key(subject.semantic_key).split("/")
            if subject is not None
            else []
        )
        if len(semantic_parts) == 2 and semantic_parts[0] in {
            "realtime",
            "latest_close",
        }:
            price_unit = price_unit or semantic_parts[1]
        elif len(semantic_parts) == 3 and semantic_parts[0] in {
            "qfq",
            "hfq",
            "unadjusted",
        }:
            adjustment = adjustment or semantic_parts[0]
            price_unit = price_unit or semantic_parts[1]
            volume_unit = volume_unit or semantic_parts[2]
        elif len(semantic_parts) == 4 and semantic_parts[0] == "60m":
            adjustment = adjustment or semantic_parts[1]
            price_unit = price_unit or semantic_parts[2]
            volume_unit = volume_unit or semantic_parts[3]
        return adjustment, price_unit, volume_unit

    @staticmethod
    def _scope_matches_result(
        record: DataQualityRecord,
        result: ProviderResult,
    ) -> bool:
        if result.subject is None:
            return (
                record.subject_type is None
                and record.subject_id is None
                and canonical_semantic_key(record.semantic_key) == ""
            )
        return (
            record.subject_type == result.subject.subject_type
            and record.subject_id == result.subject.subject_id
            and canonical_semantic_key(record.semantic_key)
            == canonical_semantic_key(result.subject.semantic_key)
        )

    def _superseded_conflict_id(
        self,
        result: ProviderResult,
        *,
        evaluated_at: datetime,
    ) -> int | None:
        subject = result.subject
        policy = policy_for(result.capability)
        if (
            subject is None
            or result.quality_status != DataQualityStatus.VERIFIED
            or not policy.verify_multiple_sources
            or result.cache_used
            or result.normalized_digest is None
        ):
            return None
        market_time = result.capability.startswith("market.")
        storage_semantics = time_storage_semantics_for_capability(
            result.capability
        )
        stored_observed_at = self._as_datetime(
            result.observed_at,
            market_time=market_time,
        )
        observed_at = (
            to_shanghai_aware(
                stored_observed_at.replace(tzinfo=timezone.utc)
                if not market_time
                else stored_observed_at,
                naive_is_shanghai=market_time,
            )
            if stored_observed_at is not None
            else None
        )
        evaluated = to_shanghai_aware(
            evaluated_at,
            naive_is_shanghai=evaluated_at.tzinfo is None,
        )
        if observed_at is None or observed_at > evaluated:
            return None

        records = self.db.scalars(
            select(DataQualityRecord)
            .where(
                DataQualityRecord.capability == result.capability,
                DataQualityRecord.subject_type == subject.subject_type,
                DataQualityRecord.subject_id == subject.subject_id,
            )
            .order_by(DataQualityRecord.id.desc())
        ).all()
        semantic_key = canonical_semantic_key(subject.semantic_key)
        scoped_records = [
            record
            for record in records
            if canonical_semantic_key(record.semantic_key) == semantic_key
        ]
        conflicts_by_id = {
            record.id: record
            for record in scoped_records
            if record.quality_status == DataQualityStatus.CONFLICTED.value
        }
        superseded_ids = set()
        for candidate in scoped_records:
            conflict = conflicts_by_id.get(candidate.supersedes_record_id)
            candidate_observed_at = (
                storage_naive_to_aware(
                    candidate.observed_at,
                    semantics=storage_semantics,
                )
                if candidate.observed_at is not None
                else None
            )
            conflict_observed_at = (
                storage_naive_to_aware(
                    conflict.observed_at,
                    semantics=storage_semantics,
                )
                if conflict is not None and conflict.observed_at is not None
                else None
            )
            if (
                conflict is not None
                and candidate.id > conflict.id
                and candidate.quality_status == DataQualityStatus.VERIFIED.value
                and candidate.trusted
                and candidate_observed_at is not None
                and conflict_observed_at is not None
                and conflict_observed_at <= candidate_observed_at <= evaluated
                and not observation_is_stale(
                    candidate.observed_at,
                    policy,
                    now=evaluated,
                    calendar=self.calendar,
                )
            ):
                superseded_ids.add(conflict.id)
        conflict = next(
            (
                record
                for record in scoped_records
                if record.quality_status == DataQualityStatus.CONFLICTED.value
                and record.id not in superseded_ids
            ),
            None,
        )
        if (
            conflict is None
            or conflict.observed_at is None
            or observed_at
            < storage_naive_to_aware(
                conflict.observed_at,
                semantics=storage_semantics,
            )
        ):
            return None
        return conflict.id

    def _advance_subject_head(
        self,
        result: ProviderResult,
        record: DataQualityRecord,
        *,
        updated_at: datetime,
    ) -> None:
        subject = result.subject
        if subject is None:
            return
        semantic_key = canonical_semantic_key(subject.semantic_key)
        stored_updated_at = self._as_datetime(
            updated_at,
            market_time=result.capability.startswith("market."),
        )
        if self.db.get_bind().dialect.name == "mysql":
            statement = mysql_insert(DataQualitySubjectHead).values(
                capability=result.capability,
                subject_type=subject.subject_type,
                subject_id=subject.subject_id,
                semantic_key=semantic_key,
                current_record_id=record.id,
                generation=1,
                updated_at=stored_updated_at,
            )
            self.db.execute(
                statement.on_duplicate_key_update(
                    current_record_id=record.id,
                    generation=DataQualitySubjectHead.generation + 1,
                    updated_at=stored_updated_at,
                )
            )
            self.db.flush()
            return
        head = self.db.scalar(
            select(DataQualitySubjectHead)
            .where(
                DataQualitySubjectHead.capability == result.capability,
                DataQualitySubjectHead.subject_type == subject.subject_type,
                DataQualitySubjectHead.subject_id == subject.subject_id,
                DataQualitySubjectHead.semantic_key == semantic_key,
            )
            .with_for_update()
        )
        if head is None:
            self.db.add(
                DataQualitySubjectHead(
                    capability=result.capability,
                    subject_type=subject.subject_type,
                    subject_id=subject.subject_id,
                    semantic_key=semantic_key,
                    current_record_id=record.id,
                    generation=1,
                    updated_at=stored_updated_at,
                )
            )
        else:
            head.current_record_id = record.id
            head.generation += 1
            head.updated_at = stored_updated_at
        self.db.flush()

    def _record_quality(self, result: ProviderResult, symbol: str | None) -> ProviderResult:
        now = self._now()
        latest_content_at = result.latest_content_at
        if (
            result.capability == "announcement.catalog"
            and isinstance(result.value, list)
            and latest_content_at is None
        ):
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
        subject = result.subject
        record = DataQualityRecord(
            symbol=symbol,
            capability=result.capability,
            subject_type=subject.subject_type if subject else None,
            subject_id=subject.subject_id if subject else None,
            semantic_key=canonical_semantic_key(subject.semantic_key)
            if subject
            else "",
            supersedes_record_id=self._superseded_conflict_id(
                result,
                evaluated_at=now,
            ),
            quality_status=result.quality_status.value,
            observed_at=self._as_datetime(
                result.observed_at,
                market_time=result.capability.startswith("market."),
            ),
            fetched_at=self._as_datetime(
                result.fetched_at,
                market_time=result.capability.startswith("market."),
            ),
            provider_id=result.provider_id,
            provider_observations=result.provider_observations,
            normalized_digest=result.normalized_digest,
            conflict_fields=result.conflict_fields,
            adjustment=result.adjustment,
            price_unit=result.price_unit,
            volume_unit=result.volume_unit,
            row_count=self.payload_row_count(result.capability, result.value),
            fallback_used=result.fallback_used,
            cache_used=result.cache_used,
            trusted=result.quality_status in TRUSTED_QUALITY_STATUSES,
            persisted=False,
            scan_start=self._as_datetime(result.scan_start),
            scan_end=self._as_datetime(result.scan_end),
            checked_at=self._as_datetime(result.checked_at),
            latest_content_at=latest_content_at,
        )
        self.db.add(record)
        self.db.flush()
        self._advance_subject_head(result, record, updated_at=now)
        result.quality_record_id = record.id
        return result

    def mark_persisted(self, result: ProviderResult, cached_at: datetime | None = None) -> None:
        record = self.validate_persistence_result(result)
        record.persisted = True
        cache_time = cached_at or self._now()
        record.cached_at = self._as_datetime(
            cache_time,
            market_time=result.capability.startswith("market."),
        )
        self.db.flush()

    def validate_persistence_result(self, result: ProviderResult) -> DataQualityRecord:
        """Validate exact Router lineage without mutating its persistence state."""
        if result.quality_record_id is None:
            raise ProviderUnavailableError("Provider result has no quality audit lineage")
        current = self.call_results.get(
            self._call_result_key(
                result.capability,
                result.operation,
                result.subject,
                result.request_fingerprint,
            )
        )
        if current is not result:
            raise ProviderUnavailableError(
                "Provider result is not the current exact Router call lineage"
            )
        if result.quality_status not in TRUSTED_QUALITY_STATUSES:
            raise ProviderUnavailableError(
                f"Cannot persist untrusted {result.capability} result "
                f"with quality {result.quality_status.value}"
            )
        record = self.db.get(DataQualityRecord, result.quality_record_id)
        if record is None:
            raise ProviderUnavailableError("Provider result has no persisted quality audit")
        if record.capability != result.capability:
            raise ProviderUnavailableError("Provider result capability does not match lineage")
        if not self._scope_matches_result(record, result):
            raise ProviderUnavailableError("Provider result subject does not match lineage")
        if record.quality_status != result.quality_status.value:
            raise ProviderUnavailableError("Provider result quality does not match lineage")
        if record.normalized_digest != result.normalized_digest:
            raise ProviderUnavailableError("Provider result digest does not match lineage")
        if self._as_datetime(record.observed_at) != self._as_datetime(
            result.observed_at,
            market_time=result.capability.startswith("market."),
        ):
            raise ProviderUnavailableError(
                "Provider result observed_at does not match lineage"
            )
        if record.provider_id != result.provider_id:
            raise ProviderUnavailableError("Provider result provider does not match lineage")
        return record

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
                requested_at=to_market_storage_naive(started),
                completed_at=to_market_storage_naive(self._now()),
            )
        )
        self.db.flush()

    def invoke(
        self,
        capability: str,
        operation: str,
        *args,
        symbol: str | None = None,
        subject: SubjectRef | None = None,
        scan_start: datetime | None = None,
        scan_end: datetime | None = None,
        cache_loader: Callable[[], Any] | None = None,
        validator: Callable[[Any], bool] | None = None,
        **kwargs,
    ) -> ProviderResult:
        if capability in MARKET_SUBJECT_CAPABILITIES and subject is None:
            raise ProviderUnavailableError(
                f"{capability} requires an explicit market subject"
            )
        if capability in RESEARCH_SUBJECT_CAPABILITIES and subject is None:
            raise ProviderUnavailableError(
                f"{capability} requires an explicit research subject"
            )
        fingerprint, request_summary = _request_identity(
            capability=capability,
            operation=operation,
            args=args,
            kwargs=kwargs,
            symbol=symbol,
        )
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
            started = self._now()
            timer = time.perf_counter()
            try:
                value = getattr(provider, operation)(*args, **kwargs)
                fetched_at = self._now()
                if capability in {
                    "market.quote.realtime",
                    "market.quote.latest_close",
                }:
                    validate_quote_contract(
                        value,
                        capability=capability,
                        expected_symbol=symbol or "",
                        evaluated_at=fetched_at,
                        calendar=self.calendar,
                    )
                elif capability in {"market.daily.qfq", "market.daily.unadjusted"}:
                    validate_daily_bar_contract(
                        value,
                        capability=capability,
                        expected_symbol=symbol or "",
                        evaluated_at=fetched_at,
                        calendar=self.calendar,
                    )
                if validator and not validator(value):
                    raise ProviderUnavailableError(
                        "Provider data failed capability completeness validation"
                    )
                observed_at = self._observed_at(capability, value, fetched_at)
                if (
                    isinstance(observed_at, date)
                    and not isinstance(observed_at, datetime)
                    and capability.startswith("market.")
                    and "daily" in capability
                ):
                    observed_at = self.calendar.session_close_at(observed_at)
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
                    row_count=self.payload_row_count(capability, value),
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
                        "fetched_at": self._now().isoformat(),
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
            scan_completed_at = (
                max(
                    item.fetched_at
                    for item in observations
                    if item.fetched_at is not None
                )
                if capability == "announcement.catalog"
                else None
            )
            adjustment, price_unit, volume_unit = self._result_dimensions(
                selected.value,
                subject,
            )
            result = ProviderResult(
                value=selected.value,
                provider_id=selected.provider_id,
                capability=capability,
                subject=subject,
                operation=operation,
                request_fingerprint=fingerprint,
                request_summary=request_summary,
                observed_at=scan_completed_at or selected.observed_at,
                fetched_at=selected.fetched_at or self._now(),
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
                adjustment=adjustment,
                price_unit=price_unit,
                volume_unit=volume_unit,
                scan_start=scan_start,
                scan_end=scan_end,
                checked_at=scan_completed_at,
            )
            result = self._record_quality(result, symbol)
            return self._remember_result(result)

        if cache_loader and policy.allow_cache_fallback:
            cached = cache_loader()
            if cached is not None and self.payload_row_count(capability, cached) > 0:
                now = self._now()
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
                    subject=subject,
                    operation=operation,
                    request_fingerprint=fingerprint,
                    request_summary=request_summary,
                    observed_at=observed_at,
                    fetched_at=now,
                    fallback_used=True,
                    cache_used=True,
                    errors=errors,
                    quality_status=DataQualityStatus.STALE,
                    provider_observations=audit,
                    normalized_digest=digest,
                    scan_start=scan_start,
                    scan_end=scan_end,
                )
                (
                    result.adjustment,
                    result.price_unit,
                    result.volume_unit,
                ) = self._result_dimensions(cached, subject)
                result = self._record_quality(result, symbol)
                self._log(
                    provider_id="local_cache",
                    capability=capability,
                    operation=operation,
                    symbol=symbol,
                    status="cache_fallback",
                    started=now,
                    duration_ms=0,
                    row_count=self.payload_row_count(capability, cached),
                    fallback_used=True,
                    cache_used=True,
                    error="; ".join(errors)[:2000] or None,
                )
                return self._remember_result(result)

        now = self._now()
        result = ProviderResult(
            value=None,
            provider_id="none",
            capability=capability,
            subject=subject,
            operation=operation,
            request_fingerprint=fingerprint,
            request_summary=request_summary,
            observed_at=None,
            fetched_at=now,
            fallback_used=bool(errors),
            cache_used=False,
            errors=errors,
            quality_status=DataQualityStatus.MISSING,
            provider_observations=audit,
            scan_start=scan_start,
            scan_end=scan_end,
        )
        (
            result.adjustment,
            result.price_unit,
            result.volume_unit,
        ) = self._result_dimensions(None, subject)
        result = self._record_quality(result, symbol)
        return self._remember_result(result)

    def get_history(self, symbol: str, start: date, end: date, cache_loader=None):
        subject = stock_daily_subject(symbol, "qfq", "CNY", "share")
        return self.invoke(
            "market.daily.qfq",
            "get_history",
            symbol,
            start,
            end,
            symbol=symbol,
            subject=subject,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list) and len(value) >= 1,
        )

    def get_quote(self, symbol: str, cache_loader=None):
        subject = stock_quote_subject(symbol, "realtime", "CNY")
        return self.invoke(
            "market.quote.realtime",
            "get_quote",
            symbol,
            symbol=symbol,
            subject=subject,
            cache_loader=cache_loader,
            validator=lambda value: value is not None
            and getattr(value, "price", None) is not None,
        )

    def get_latest_close(self, symbol: str, cache_loader=None):
        subject = stock_quote_subject(symbol, "latest_close", "CNY")
        return self.invoke(
            "market.quote.latest_close",
            "get_quote",
            symbol,
            symbol=symbol,
            subject=subject,
            cache_loader=cache_loader,
            validator=lambda value: value is not None
            and getattr(value, "price", None) is not None
            and getattr(value, "quote_type", None) == "latest_close",
        )

    def get_unadjusted_history(
        self, symbol: str, start: date, end: date, cache_loader=None
    ):
        subject = stock_daily_subject(
            symbol,
            "unadjusted",
            "CNY",
            "share",
        )
        return self.invoke(
            "market.daily.unadjusted",
            "get_history",
            symbol,
            start,
            end,
            symbol=symbol,
            subject=subject,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list)
            and bool(value)
            and all(getattr(item, "adjustment", None) == "unadjusted" for item in value),
        )

    def get_index_history(self, symbol: str, start: date, end: date, cache_loader=None):
        subject = index_daily_subject(
            symbol,
            "unadjusted",
            "CNY",
            "share",
        )
        return self.invoke(
            "market.index_daily",
            "get_index_history",
            symbol,
            start,
            end,
            symbol=subject.subject_id,
            subject=subject,
            cache_loader=cache_loader,
            validator=lambda value: bool(value and len(value.get("rows", [])) >= 20),
        )

    def get_sector_history(self, industry: str, start: date, end: date, cache_loader=None):
        subject = sector_daily_subject(
            industry,
            "unadjusted",
            "CNY",
            "share",
        )
        return self.invoke(
            "market.sector_daily",
            "get_sector_history",
            industry,
            start,
            end,
            symbol=subject.subject_id,
            subject=subject,
            cache_loader=cache_loader,
            validator=lambda value: bool(value and len(value.get("rows", [])) >= 20),
        )

    def get_intraday_60m(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        cache_loader=None,
    ):
        subject = stock_intraday_subject(symbol)
        return self.invoke(
            "market.intraday.60m",
            "get_intraday_60m",
            symbol,
            start,
            end,
            symbol=symbol,
            subject=subject,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list)
            and bool(value)
            and all(
                isinstance(item, IntradayBar)
                and item.completed
                and item.bar_start.tzinfo is not None
                and item.bar_end.tzinfo is not None
                and item.bar_start < item.bar_end <= end
                for item in value
            ),
        )

    def get_turnover_daily(
        self, symbol: str, start: date, end: date, cache_loader=None
    ):
        return self.invoke(
            "market.turnover.daily",
            "get_turnover_daily",
            symbol,
            start,
            end,
            symbol=symbol,
            subject=stock_turnover_subject(symbol),
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list) and bool(value),
        )

    def get_market_breadth(self, day: date, cache_loader=None):
        return self.invoke(
            "market.breadth.daily",
            "get_market_breadth",
            day,
            symbol="CN-A",
            subject=market_breadth_subject(),
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list) and len(value) == 1,
        )

    def get_market_amount(self, day: date, cache_loader=None):
        return self.invoke(
            "market.amount.daily",
            "get_market_amount",
            day,
            symbol="CN-A",
            subject=market_amount_subject(),
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list) and len(value) == 1,
        )

    def get_market_amount_history(
        self, start: date, end: date, cache_loader=None
    ):
        return self.invoke(
            "market.amount.daily",
            "get_market_amount_history",
            start,
            end,
            symbol="CN-A",
            subject=market_amount_subject(),
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list) and bool(value),
        )

    def get_industry_daily(
        self, industry: str, start: date, end: date, cache_loader=None
    ):
        subject = sector_daily_subject(industry, "unadjusted", "CNY", "share")
        return self.invoke(
            "market.industry.daily",
            "get_industry_daily",
            industry,
            start,
            end,
            symbol=subject.subject_id,
            subject=subject,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list) and bool(value),
        )

    def get_industry_constituents(self, industry: str, cache_loader=None):
        subject = industry_constituents_subject(industry)
        return self.invoke(
            "market.industry.constituents",
            "get_industry_constituents",
            industry,
            symbol=subject.subject_id,
            subject=subject,
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list) and bool(value),
        )

    def company_concepts(self, symbol: str, cache_loader=None):
        return self.invoke(
            "company.concepts",
            "company_concepts",
            symbol,
            symbol=symbol,
            subject=company_concepts_subject(symbol),
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list) and bool(value),
        )

    def company_industry_chain(self, symbol: str, cache_loader=None):
        return self.invoke(
            "company.industry_chain",
            "company_industry_chain",
            symbol,
            symbol=symbol,
            subject=company_industry_chain_subject(symbol),
            cache_loader=cache_loader,
            validator=lambda value: isinstance(value, list) and bool(value),
        )

    def company_profile(self, symbol: str, cache_loader=None):
        return self.invoke(
            "fundamental.profile",
            "company_profile",
            symbol,
            symbol=symbol,
            subject=company_profile_subject(symbol),
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
        subject = announcement_catalog_subject(symbol, start, end)
        return self.invoke(
            "announcement.catalog",
            "company_announcements",
            symbol,
            start,
            end,
            symbol=symbol,
            subject=subject,
            scan_start=datetime.combine(start, datetime_time.min),
            scan_end=datetime.combine(end, datetime_time.max),
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
        now = self._now()
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
