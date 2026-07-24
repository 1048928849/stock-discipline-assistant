from __future__ import annotations

from enum import StrEnum


class DataQualityStatus(StrEnum):
    VERIFIED = "VERIFIED"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    CONFLICTED = "CONFLICTED"
    STALE = "STALE"
    MISSING = "MISSING"

    @property
    def blocks_execution(self) -> bool:
        return self in {
            DataQualityStatus.CONFLICTED,
            DataQualityStatus.STALE,
            DataQualityStatus.MISSING,
        }


def worst_quality(statuses: list[DataQualityStatus]) -> DataQualityStatus:
    if not statuses:
        return DataQualityStatus.MISSING
    priority = {
        DataQualityStatus.MISSING: 5,
        DataQualityStatus.CONFLICTED: 4,
        DataQualityStatus.STALE: 3,
        DataQualityStatus.SINGLE_SOURCE: 2,
        DataQualityStatus.VERIFIED: 1,
    }
    return max(statuses, key=priority.__getitem__)
