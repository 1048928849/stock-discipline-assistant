from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.data_hub.contracts import DailyBar, ProviderUnavailableError
from app.data_hub.effective_quality import resolve_effective_quality
from app.data_hub.market_subjects import stock_quote_subject
from app.data_hub.router import DataHubRouter, ProviderResult
from app.domain.quality_subject import (
    EffectiveQualityResult,
    SubjectRef,
    canonical_semantic_key,
)
from app.domain.models import MarketQualityBinding
from app.models import DataQualityRecord, MarketDailyBar, MarketQuote


_QUOTE_TYPES = {
    "market.quote.realtime": "realtime",
    "market.quote.latest_close": "latest_close",
}


@dataclass(frozen=True)
class CachedQuoteSelection:
    value: MarketQuote | None
    subject: SubjectRef
    quality_record_id: int | None
    effective_quality: EffectiveQualityResult
    source: str | None
    observed_at: datetime | None
    executable: bool
    blocking_reason: str | None


@dataclass(frozen=True)
class CachedSeriesSelection:
    bars: list[MarketDailyBar]
    subject: SubjectRef
    quality_record_id: int | None
    effective_quality: EffectiveQualityResult
    source: str | None
    observed_at: datetime | date | None
    executable: bool
    blocking_reason: str | None
    structure_reason: str | None = None


@dataclass(frozen=True)
class BindingValidationResult:
    binding: MarketQualityBinding
    executable: bool
    error_code: str | None
    reason: str | None
    selected_quality_record_id: int | None
    selected_observed_at: datetime | date | None


@dataclass(frozen=True)
class _SeriesCandidate:
    bars: list[MarketDailyBar]
    quality_record_id: int
    effective_quality: EffectiveQualityResult
    observed_at: datetime | date | None
    source: str | None
    structure_reason: str | None


@dataclass(frozen=True)
class CanonicalSeriesRow:
    symbol: str
    trade_date: str
    open: str
    high: str
    low: str
    close: str
    volume: str
    adjustment: str
    price_unit: str
    volume_unit: str
    observed_at: str
    source: str
    fetched_at: str


def effective_quality_metadata(result: EffectiveQualityResult) -> dict[str, Any]:
    return {
        "stored_quality": result.stored_quality.value if result.stored_quality else None,
        "freshness_quality": result.freshness_quality.value,
        "newest_signal_quality": (
            result.newest_signal_quality.value if result.newest_signal_quality else None
        ),
        "effective_quality": result.effective_quality.value,
        "executable": result.executable,
        "requires_refresh": result.requires_refresh,
        "blocking_record_id": result.blocking_record_id,
        "blocking_reason": result.blocking_reason,
        "subject_type": result.subject_type,
        "subject_id": result.subject_id,
        "semantic_key": result.semantic_key,
    }


def persist_market_quote(
    db: Session,
    router: DataHubRouter,
    result: ProviderResult,
) -> MarketQuote:
    quote = result.require_trusted_value()
    if result.subject is None or result.quality_record_id is None:
        raise ProviderUnavailableError("quote result has no complete lineage")
    expected = stock_quote_subject(quote.symbol, quote.quote_type, quote.price_unit)
    if result.subject != expected:
        raise ProviderUnavailableError("quote result subject does not match value")

    stored = db.scalar(select(MarketQuote).where(MarketQuote.symbol == quote.symbol))
    values = {
        "name": quote.name,
        "price": quote.price,
        "quote_type": quote.quote_type,
        "observed_at": quote.observed_at,
        "price_unit": quote.price_unit,
        "quality_status": result.quality_status.value,
        "quality_record_id": result.quality_record_id,
        "source": quote.source,
        "source_api": quote.source_api,
        "fetched_at": quote.fetched_at,
    }
    if stored is None:
        stored = MarketQuote(symbol=quote.symbol, **values)
        db.add(stored)
    else:
        for key, value in values.items():
            setattr(stored, key, value)
    db.flush()
    router.mark_persisted(result)
    return stored


def mapping_series_bars(
    payload: dict[str, Any],
    *,
    cache_symbol: str,
    adjustment: str,
) -> list[DailyBar]:
    source = str(payload["source"])
    fetched_at = payload["fetched_at"]
    bars = []
    for row in payload["rows"]:
        close = Decimal(str(row["close"]))
        trade_date = row["date"]
        bars.append(
            DailyBar(
                symbol=cache_symbol,
                trade_date=trade_date,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal(str(row.get("volume") or 0)),
                adjustment=adjustment,
                price_unit="CNY",
                volume_unit="share",
                observed_at=datetime.combine(trade_date, time.min),
                source=source,
                fetched_at=fetched_at,
            )
        )
    return bars


def _canonical_decimal(value: Any) -> str:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("non_finite_series_number")
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _canonical_series_datetime(value: Any) -> str:
    if not isinstance(value, datetime):
        raise ValueError("invalid_series_datetime")
    return _as_datetime(value).isoformat(timespec="microseconds")


def _canonical_series_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_series_text")
    return value


def canonical_series_row(value: Any) -> CanonicalSeriesRow:
    trade_date = getattr(value, "trade_date", None)
    if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
        raise ValueError("invalid_series_trade_date")
    return CanonicalSeriesRow(
        symbol=_canonical_series_text(getattr(value, "symbol", None)),
        trade_date=trade_date.isoformat(),
        open=_canonical_decimal(getattr(value, "open", None)),
        high=_canonical_decimal(getattr(value, "high", None)),
        low=_canonical_decimal(getattr(value, "low", None)),
        close=_canonical_decimal(getattr(value, "close", None)),
        volume=_canonical_decimal(getattr(value, "volume", None)),
        adjustment=_canonical_series_text(getattr(value, "adjustment", None)),
        price_unit=_canonical_series_text(getattr(value, "price_unit", None)),
        volume_unit=_canonical_series_text(getattr(value, "volume_unit", None)),
        observed_at=_canonical_series_datetime(getattr(value, "observed_at", None)),
        source=_canonical_series_text(getattr(value, "source", None)),
        fetched_at=_canonical_series_datetime(getattr(value, "fetched_at", None)),
    )


def _provider_series_rows(
    trusted: Any,
    *,
    subject: SubjectRef,
) -> list[Any]:
    if isinstance(trusted, list):
        return trusted
    if isinstance(trusted, dict) and isinstance(trusted.get("rows"), list):
        adjustment, _, _ = _series_semantics(subject)
        return mapping_series_bars(
            trusted,
            cache_symbol=subject.subject_id,
            adjustment=adjustment,
        )
    raise ProviderUnavailableError("unsupported series result structure")


def replace_market_series(
    db: Session,
    router: DataHubRouter,
    result: ProviderResult,
    bars: Iterable[Any],
    *,
    subject: SubjectRef,
    min_rows: int,
) -> list[MarketDailyBar]:
    trusted = result.require_trusted_value()
    rows = validate_series_for_persistence(bars, subject=subject, min_rows=min_rows)
    if result.subject != subject or result.quality_record_id is None:
        raise ProviderUnavailableError("series result has no matching complete lineage")
    expected_rows = _provider_series_rows(trusted, subject=subject)
    expected_row_count = len(expected_rows)
    if len(rows) != expected_row_count:
        raise ProviderUnavailableError("series persistence row count does not match result")
    try:
        expected_canonical = [canonical_series_row(item) for item in expected_rows]
        actual_canonical = [canonical_series_row(item) for item in rows]
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ProviderUnavailableError(
            "series persistence content does not match provider result"
        ) from exc
    if actual_canonical != expected_canonical:
        raise ProviderUnavailableError(
            "series persistence content does not match provider result"
        )

    current = router.result_for(
        result.capability,
        result.operation,
        result.subject,
        result.request_fingerprint,
    )
    if current is not result:
        raise ProviderUnavailableError(
            "series result is not the current exact Router call lineage"
        )
    record = db.get(DataQualityRecord, result.quality_record_id)
    if record is None:
        raise ProviderUnavailableError("series result has no quality record")
    if record.row_count != expected_row_count:
        raise ProviderUnavailableError(
            "series quality record row count does not match result"
        )
    if record.capability != result.capability or not _scope_matches(
        record,
        capability=result.capability,
        subject=subject,
    ):
        raise ProviderUnavailableError("series quality record scope does not match result")
    if record.quality_status != result.quality_status.value:
        raise ProviderUnavailableError("series quality record quality does not match result")
    if record.normalized_digest != result.normalized_digest:
        raise ProviderUnavailableError("series quality record digest does not match result")
    if _as_datetime(record.observed_at) != _as_datetime(result.observed_at):
        raise ProviderUnavailableError(
            "series quality record observed_at does not match result"
        )
    if record.provider_id != result.provider_id:
        raise ProviderUnavailableError("series quality record provider does not match result")

    first = rows[0]
    db.execute(
        delete(MarketDailyBar).where(
            MarketDailyBar.symbol == subject.subject_id,
            MarketDailyBar.source == first.source,
            MarketDailyBar.adjustment == first.adjustment,
            MarketDailyBar.price_unit == first.price_unit,
            MarketDailyBar.volume_unit == first.volume_unit,
        )
    )
    db.flush()
    stored = [
        MarketDailyBar(
            symbol=row.symbol,
            trade_date=row.trade_date,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            adjustment=row.adjustment,
            price_unit=row.price_unit,
            volume_unit=row.volume_unit,
            observed_at=row.observed_at,
            quality_status=result.quality_status.value,
            quality_record_id=result.quality_record_id,
            source=row.source,
            fetched_at=row.fetched_at,
        )
        for row in rows
    ]
    db.add_all(stored)
    db.flush()
    router.mark_persisted(result)
    return stored


def _as_datetime(value: datetime | date | None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    return datetime.min


def _series_semantics(subject: SubjectRef) -> tuple[str, str, str]:
    parts = canonical_semantic_key(subject.semantic_key).split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError("invalid_series_subject_semantic_key")
    return parts[0], parts[1], parts[2]


def validate_series_for_persistence(
    bars: Iterable[Any],
    *,
    subject: SubjectRef,
    min_rows: int,
) -> list[Any]:
    if min_rows < 1:
        raise ValueError("min_rows_must_be_positive")
    rows = list(bars)
    if not rows:
        raise ValueError("empty_series")
    if len(rows) < min_rows:
        raise ValueError("insufficient_rows")

    dates = [getattr(item, "trade_date", None) for item in rows]
    if any(item is None for item in dates):
        raise ValueError("missing_trade_date")
    if len(set(dates)) != len(dates):
        raise ValueError("duplicate_trade_dates")
    if dates != sorted(dates):
        raise ValueError("non_monotonic_trade_dates")

    if len({getattr(item, "source", None) for item in rows}) != 1:
        raise ValueError("mixed_sources")

    expected_adjustment, expected_price_unit, expected_volume_unit = _series_semantics(
        subject
    )
    dimensions = {
        (
            getattr(item, "adjustment", None),
            getattr(item, "price_unit", None),
            getattr(item, "volume_unit", None),
        )
        for item in rows
    }
    if dimensions != {
        (expected_adjustment, expected_price_unit, expected_volume_unit)
    }:
        raise ValueError("mixed_dimensions")
    if {getattr(item, "symbol", None) for item in rows} != {subject.subject_id}:
        raise ValueError("subject_mismatch")
    if any(getattr(item, "observed_at", None) is None for item in rows):
        raise ValueError("missing_observed_at")

    for item in rows:
        low = getattr(item, "low", None)
        high = getattr(item, "high", None)
        open_price = getattr(item, "open", None)
        close = getattr(item, "close", None)
        if (
            None in {low, high, open_price, close}
            or low > high
            or not low <= open_price <= high
            or not low <= close <= high
        ):
            raise ValueError("invalid_ohlc")
    return rows


def _scope_matches(
    record: DataQualityRecord,
    *,
    capability: str,
    subject: SubjectRef,
) -> bool:
    return (
        record.capability == capability
        and record.subject_type == subject.subject_type
        and record.subject_id == subject.subject_id
        and canonical_semantic_key(record.semantic_key)
        == canonical_semantic_key(subject.semantic_key)
    )


def _series_structure_reason(
    record: DataQualityRecord | None,
    group: list[MarketDailyBar],
    *,
    quality_record_id: int,
    capability: str,
    subject: SubjectRef,
    adjustment: str,
    price_unit: str,
    volume_unit: str,
    min_rows: int,
) -> str | None:
    if record is None:
        return "incomplete_lineage"
    if not record.persisted:
        return "lineage_not_persisted"
    if not _scope_matches(record, capability=capability, subject=subject):
        return "lineage_scope_mismatch"
    if record.row_count < min_rows or len(group) < min_rows:
        return "incomplete_lineage"
    if len(group) != record.row_count:
        return "row_count_mismatch"
    if {item.quality_record_id for item in group} != {quality_record_id}:
        return "incomplete_lineage"

    dates = [item.trade_date for item in group]
    if len(set(dates)) != len(dates):
        return "duplicate_trade_dates"
    if dates != sorted(dates):
        return "non_monotonic_trade_dates"
    if len({item.source for item in group}) != 1:
        return "mixed_sources"

    expected_dimensions = {(adjustment, price_unit, volume_unit)}
    group_dimensions = {
        (item.adjustment, item.price_unit, item.volume_unit) for item in group
    }
    record_dimensions = {
        (record.adjustment, record.price_unit, record.volume_unit)
    }
    if group_dimensions != expected_dimensions or record_dimensions != expected_dimensions:
        return "mixed_dimensions"

    observed_at = max(item.observed_at for item in group)
    if _as_datetime(observed_at) != _as_datetime(record.observed_at):
        return "observed_at_mismatch"
    return None


def resolve_cached_quote(
    db: Session,
    *,
    symbol: str,
    capability: str,
    price_unit: str = "CNY",
    evaluated_at: datetime | None = None,
) -> CachedQuoteSelection:
    quote_type = _QUOTE_TYPES.get(capability)
    if quote_type is None:
        raise ValueError(f"unsupported quote capability: {capability}")
    subject = stock_quote_subject(symbol, quote_type, price_unit)
    with db.no_autoflush:
        quote = db.scalar(
            select(MarketQuote)
            .where(
                MarketQuote.symbol == subject.subject_id,
                MarketQuote.quote_type == quote_type,
                MarketQuote.price_unit == price_unit.upper(),
            )
            .order_by(MarketQuote.observed_at.desc(), MarketQuote.id.desc())
        )
    effective = resolve_effective_quality(
        db,
        capability=capability,
        subject=subject,
        persisted_quality_record_id=quote.quality_record_id if quote else None,
        observed_at=quote.observed_at if quote else None,
        evaluated_at=evaluated_at,
    )
    return CachedQuoteSelection(
        value=quote,
        subject=subject,
        quality_record_id=quote.quality_record_id if quote else None,
        effective_quality=effective,
        source=quote.source if quote else None,
        observed_at=quote.observed_at if quote else None,
        executable=effective.executable,
        blocking_reason=effective.blocking_reason,
    )


def resolve_cached_series(
    db: Session,
    *,
    cache_symbol: str,
    capability: str,
    subject: SubjectRef,
    adjustment: str,
    price_unit: str,
    volume_unit: str,
    min_rows: int,
    evaluated_at: datetime | None = None,
) -> CachedSeriesSelection:
    if min_rows < 1:
        raise ValueError("min_rows must be positive")
    with db.no_autoflush:
        seed_rows = db.scalars(
            select(MarketDailyBar)
            .where(
                MarketDailyBar.symbol == cache_symbol,
                MarketDailyBar.adjustment == adjustment,
                MarketDailyBar.price_unit == price_unit,
                MarketDailyBar.volume_unit == volume_unit,
            )
            .order_by(
                MarketDailyBar.quality_record_id,
                MarketDailyBar.trade_date,
                MarketDailyBar.id,
            )
        ).all()
        quality_record_ids = {
            row.quality_record_id
            for row in seed_rows
            if row.quality_record_id is not None
        }
        rows = (
            db.scalars(
                select(MarketDailyBar)
                .where(
                    MarketDailyBar.symbol == cache_symbol,
                    MarketDailyBar.quality_record_id.in_(quality_record_ids),
                )
                .order_by(
                    MarketDailyBar.quality_record_id,
                    MarketDailyBar.trade_date,
                    MarketDailyBar.id,
                )
            ).all()
            if quality_record_ids
            else []
        )
        records = (
            {
                record.id: record
                for record in db.scalars(
                    select(DataQualityRecord).where(
                        DataQualityRecord.id.in_(quality_record_ids)
                    )
                ).all()
            }
            if quality_record_ids
            else {}
        )

    groups: dict[int, list[MarketDailyBar]] = defaultdict(list)
    for row in rows:
        if row.quality_record_id is not None:
            groups[row.quality_record_id].append(row)

    candidates: list[_SeriesCandidate] = []
    for quality_record_id, group in groups.items():
        observed_at = max(
            (item.observed_at for item in group),
            default=None,
        )
        effective = resolve_effective_quality(
            db,
            capability=capability,
            subject=subject,
            persisted_quality_record_id=quality_record_id,
            observed_at=observed_at,
            evaluated_at=evaluated_at,
        )
        structure_reason = _series_structure_reason(
            records.get(quality_record_id),
            group,
            quality_record_id=quality_record_id,
            capability=capability,
            subject=subject,
            adjustment=adjustment,
            price_unit=price_unit,
            volume_unit=volume_unit,
            min_rows=min_rows,
        )
        candidates.append(
            _SeriesCandidate(
                bars=group,
                quality_record_id=quality_record_id,
                effective_quality=effective,
                observed_at=observed_at,
                source=group[0].source if group else None,
                structure_reason=structure_reason,
            )
        )

    complete = [item for item in candidates if item.structure_reason is None]
    executable = [item for item in complete if item.effective_quality.executable]
    if executable:
        selected = max(
            executable,
            key=lambda item: (
                _as_datetime(item.observed_at),
                item.quality_record_id,
            ),
        )
        return CachedSeriesSelection(
            bars=selected.bars,
            subject=subject,
            quality_record_id=selected.quality_record_id,
            effective_quality=selected.effective_quality,
            source=selected.source,
            observed_at=selected.observed_at,
            executable=True,
            blocking_reason=None,
            structure_reason=None,
        )

    if complete:
        selected = max(
            complete,
            key=lambda item: (
                _as_datetime(item.observed_at),
                item.quality_record_id,
            ),
        )
        return CachedSeriesSelection(
            bars=[],
            subject=subject,
            quality_record_id=selected.quality_record_id,
            effective_quality=selected.effective_quality,
            source=selected.source,
            observed_at=selected.observed_at,
            executable=False,
            blocking_reason=selected.effective_quality.blocking_reason,
            structure_reason=None,
        )

    if candidates:
        selected = max(
            candidates,
            key=lambda item: (
                _as_datetime(item.observed_at),
                item.quality_record_id,
            ),
        )
        return CachedSeriesSelection(
            bars=[],
            subject=subject,
            quality_record_id=selected.quality_record_id,
            effective_quality=selected.effective_quality,
            source=selected.source,
            observed_at=selected.observed_at,
            executable=False,
            blocking_reason=selected.structure_reason,
            structure_reason=selected.structure_reason,
        )

    effective = resolve_effective_quality(
        db,
        capability=capability,
        subject=subject,
        evaluated_at=evaluated_at,
    )
    return CachedSeriesSelection(
        bars=[],
        subject=subject,
        quality_record_id=None,
        effective_quality=effective,
        source=None,
        observed_at=None,
        executable=False,
        blocking_reason=effective.blocking_reason,
        structure_reason="incomplete_lineage" if seed_rows else None,
    )


def resolve_market_quality_binding(
    db: Session,
    binding: MarketQualityBinding,
    *,
    evaluated_at: datetime | None = None,
) -> BindingValidationResult:
    subject = SubjectRef(
        subject_type=binding.subject_type,
        subject_id=binding.subject_id,
        semantic_key=binding.semantic_key,
    )
    record = db.get(DataQualityRecord, binding.quality_record_id)
    if record is None or not record.persisted:
        return BindingValidationResult(
            binding, False, "MARKET_BINDING_NOT_EXECUTABLE",
            "bound quality lineage is missing or was not persisted", None, None,
        )
    if not _scope_matches(
        record, capability=binding.data_capability, subject=subject
    ):
        return BindingValidationResult(
            binding, False, "MARKET_BINDING_SCOPE_MISMATCH",
            "bound quality lineage scope does not match the package", None, None,
        )

    if binding.data_capability == "market.quote.realtime":
        parts = canonical_semantic_key(binding.semantic_key).split("/")
        if len(parts) != 2 or parts[0] != "realtime" or not parts[1]:
            return BindingValidationResult(
                binding, False, "MARKET_BINDING_SCOPE_MISMATCH",
                "realtime quote binding has invalid semantic scope", None, None,
            )
        selection = resolve_cached_quote(
            db,
            symbol=subject.subject_id,
            capability=binding.data_capability,
            price_unit=parts[1],
            evaluated_at=evaluated_at,
        )
        selected_subject = selection.subject
    else:
        policies = {
            "market.daily.qfq": ("stock", 250),
            "market.index_daily": ("index", 60),
            "market.sector_daily": ("sector", 60),
        }
        policy = policies.get(binding.data_capability)
        if policy is None or subject.subject_type != policy[0]:
            return BindingValidationResult(
                binding, False, "MARKET_BINDING_SCOPE_MISMATCH",
                "market series binding has invalid capability or subject type", None, None,
            )
        try:
            adjustment, price_unit, volume_unit = _series_semantics(subject)
        except ValueError:
            return BindingValidationResult(
                binding, False, "MARKET_BINDING_SCOPE_MISMATCH",
                "market series binding has invalid semantic scope", None, None,
            )
        selection = resolve_cached_series(
            db,
            cache_symbol=subject.subject_id,
            capability=binding.data_capability,
            subject=subject,
            adjustment=adjustment,
            price_unit=price_unit,
            volume_unit=volume_unit,
            min_rows=policy[1],
            evaluated_at=evaluated_at,
        )
        selected_subject = selection.subject

    if selected_subject != subject:
        return BindingValidationResult(
            binding, False, "MARKET_BINDING_SCOPE_MISMATCH",
            "current market selection has a different subject scope",
            selection.quality_record_id, selection.observed_at,
        )
    if not selection.executable:
        return BindingValidationResult(
            binding, False, "MARKET_BINDING_NOT_EXECUTABLE",
            selection.blocking_reason or "bound market lineage is not executable",
            selection.quality_record_id, selection.observed_at,
        )
    if selection.quality_record_id != binding.quality_record_id:
        return BindingValidationResult(
            binding, False, "MARKET_BINDING_CHANGED",
            "current market selection uses a different quality lineage",
            selection.quality_record_id, selection.observed_at,
        )
    if _as_datetime(record.observed_at) != _as_datetime(binding.observed_at):
        return BindingValidationResult(
            binding, False, "MARKET_BINDING_CHANGED",
            "bound quality lineage has a different observed time",
            selection.quality_record_id, selection.observed_at,
        )
    if _as_datetime(selection.observed_at) != _as_datetime(binding.observed_at):
        return BindingValidationResult(
            binding, False, "MARKET_BINDING_CHANGED",
            "current market selection has a different observed time",
            selection.quality_record_id, selection.observed_at,
        )
    return BindingValidationResult(
        binding, True, None, None,
        selection.quality_record_id, selection.observed_at,
    )


__all__ = [
    "CanonicalSeriesRow",
    "CachedQuoteSelection",
    "CachedSeriesSelection",
    "BindingValidationResult",
    "canonical_series_row",
    "effective_quality_metadata",
    "mapping_series_bars",
    "persist_market_quote",
    "replace_market_series",
    "resolve_cached_quote",
    "resolve_cached_series",
    "resolve_market_quality_binding",
    "validate_series_for_persistence",
]
