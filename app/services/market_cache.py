from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_hub.effective_quality import resolve_effective_quality
from app.data_hub.market_subjects import stock_quote_subject
from app.domain.quality_subject import EffectiveQualityResult, SubjectRef
from app.models import MarketDailyBar, MarketQuote


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


def _as_datetime(value: datetime | date | None) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    return datetime.min


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
        rows = db.scalars(
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

    groups: dict[int, list[MarketDailyBar]] = defaultdict(list)
    for row in rows:
        if row.quality_record_id is not None:
            groups[row.quality_record_id].append(row)

    candidates = []
    for quality_record_id, group in groups.items():
        dates = [item.trade_date for item in group]
        if (
            len(group) < min_rows
            or len(set(dates)) != len(dates)
            or dates != sorted(dates)
            or len({item.source for item in group}) != 1
            or len({item.adjustment for item in group}) != 1
            or len({item.price_unit for item in group}) != 1
            or len({item.volume_unit for item in group}) != 1
            or {item.quality_record_id for item in group} != {quality_record_id}
        ):
            continue
        observed_at = max(item.observed_at for item in group)
        effective = resolve_effective_quality(
            db,
            capability=capability,
            subject=subject,
            persisted_quality_record_id=quality_record_id,
            observed_at=observed_at,
            evaluated_at=evaluated_at,
        )
        candidates.append(
            (
                observed_at,
                quality_record_id,
                group,
                effective,
            )
        )

    executable = [item for item in candidates if item[3].executable]
    if executable:
        observed_at, quality_record_id, group, effective = max(
            executable,
            key=lambda item: (_as_datetime(item[0]), item[1]),
        )
        return CachedSeriesSelection(
            bars=group,
            subject=subject,
            quality_record_id=quality_record_id,
            effective_quality=effective,
            source=group[0].source,
            observed_at=observed_at,
            executable=True,
            blocking_reason=None,
        )

    if candidates:
        observed_at, quality_record_id, group, effective = max(
            candidates,
            key=lambda item: (_as_datetime(item[0]), item[1]),
        )
        return CachedSeriesSelection(
            bars=[],
            subject=subject,
            quality_record_id=quality_record_id,
            effective_quality=effective,
            source=group[0].source,
            observed_at=observed_at,
            executable=False,
            blocking_reason=effective.blocking_reason,
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
    )


__all__ = [
    "CachedQuoteSelection",
    "CachedSeriesSelection",
    "resolve_cached_quote",
    "resolve_cached_series",
]
