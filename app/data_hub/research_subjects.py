from __future__ import annotations

from datetime import date

from app.domain.quality_subject import SubjectRef


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


__all__ = ["announcement_catalog_subject", "company_profile_subject"]
