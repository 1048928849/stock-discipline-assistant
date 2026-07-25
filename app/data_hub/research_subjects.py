from __future__ import annotations

from datetime import date, datetime, timedelta

from app.data_hub.trading_calendar import shanghai_now, to_shanghai_aware
from app.domain.quality_subject import SubjectRef


def announcement_catalog_window(
    *,
    evaluated_at: datetime | None = None,
    lookback_days: int = 3 * 366,
) -> tuple[date, date]:
    if lookback_days < 0:
        raise ValueError("announcement lookback_days must not be negative")
    business_now = to_shanghai_aware(evaluated_at or shanghai_now())
    end = business_now.date()
    return end - timedelta(days=lookback_days), end


def company_profile_subject(symbol: str) -> SubjectRef:
    return SubjectRef(
        subject_type="stock",
        subject_id=symbol,
        semantic_key="profile",
    )


def announcement_catalog_subject(
    symbol: str,
    start: date,
    end: date,
) -> SubjectRef:
    if not isinstance(start, date) or not isinstance(end, date):
        raise ValueError("announcement coverage requires date values")
    if start > end:
        raise ValueError("announcement coverage start must not be after end")
    return SubjectRef(
        subject_type="stock",
        subject_id=symbol,
        semantic_key=f"catalog/{start.isoformat()}/{end.isoformat()}",
    )


__all__ = [
    "announcement_catalog_subject",
    "announcement_catalog_window",
    "company_profile_subject",
]
