from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from enum import StrEnum
from typing import Any


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


@dataclass(frozen=True)
class QualityObservation:
    provider_id: str
    value: Any
    stale: bool = False


def _canonical(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def assess_quality(observations: list[QualityObservation]) -> DataQualityStatus:
    usable = [item for item in observations if item.value is not None]
    if not usable:
        return DataQualityStatus.MISSING
    if any(item.stale for item in usable):
        return DataQualityStatus.STALE
    if len(usable) == 1:
        return DataQualityStatus.SINGLE_SOURCE
    values = {_canonical(item.value) for item in usable}
    return DataQualityStatus.VERIFIED if len(values) == 1 else DataQualityStatus.CONFLICTED


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
