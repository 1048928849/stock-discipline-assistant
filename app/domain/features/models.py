from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class FeatureQuality(StrEnum):
    GOOD = "GOOD"
    STALE = "STALE"
    FALLBACK = "FALLBACK"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    CONFLICTED = "CONFLICTED"


@dataclass(frozen=True)
class FeatureValue:
    value: Any
    source_ids: tuple[str, ...] = ()
    data_time: str | None = None
    quality: FeatureQuality = FeatureQuality.GOOD


@dataclass(frozen=True)
class Feature:
    feature_id: str
    result: FeatureValue

    @property
    def value(self) -> Any:
        return self.result.value

    @property
    def quality(self) -> FeatureQuality:
        return self.result.quality

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "value": self.value,
            "source_ids": list(self.result.source_ids),
            "data_time": self.result.data_time,
            "quality": self.quality.value,
        }


@dataclass(frozen=True)
class FeatureSnapshot:
    symbol: str
    as_of: str
    features: tuple[Feature, ...]

    def __post_init__(self) -> None:
        feature_ids = [item.feature_id for item in self.features]
        if len(feature_ids) != len(set(feature_ids)):
            raise ValueError("FeatureSnapshot 中的 feature_id 必须唯一")

    def get(self, feature_id: str) -> Feature:
        for feature in self.features:
            if feature.feature_id == feature_id:
                return feature
        raise KeyError(feature_id)

    def value(self, feature_id: str) -> Any:
        return self.get(feature_id).value

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "features": [feature.to_dict() for feature in self.features],
        }
