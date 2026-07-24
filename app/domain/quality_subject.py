from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.quality import DataQualityStatus


SubjectType = Literal["stock", "index", "sector"]
QualityKey = tuple[str, SubjectType, str, str, int | None]


def canonical_semantic_key(value: str | None) -> str:
    return "" if value is None else value.strip()


class SubjectRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_type: SubjectType
    subject_id: str = Field(min_length=1, max_length=160)
    semantic_key: str | None = Field(default=None, max_length=200)

    @field_validator("subject_id", mode="before")
    @classmethod
    def normalize_subject_id(cls, value):
        if not isinstance(value, str):
            return value
        return value.strip()

    @field_validator("semantic_key", mode="before")
    @classmethod
    def normalize_semantic_key(cls, value):
        if value is None or not isinstance(value, str):
            return value
        normalized = canonical_semantic_key(value)
        return normalized or None

    @model_validator(mode="after")
    def validate_subject(self):
        if self.subject_type == "stock":
            if len(self.subject_id) != 6 or not self.subject_id.isdigit():
                raise ValueError("stock subject_id must be a six-digit symbol")
        elif self.subject_type == "index":
            normalized = self.subject_id.upper()
            if len(normalized) < 3 or not normalized.replace("_", "").replace("-", "").isalnum():
                raise ValueError("index subject_id must be a stable alphanumeric identifier")
            object.__setattr__(self, "subject_id", normalized)
        elif not self.subject_id.strip():
            raise ValueError("sector subject_id must be a stable non-empty identifier")
        return self

    @property
    def stable_key(self) -> tuple[SubjectType, str, str | None]:
        return (self.subject_type, self.subject_id, self.semantic_key)


class EffectiveQualityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str = Field(min_length=1, max_length=80)
    subject: SubjectRef
    persisted_quality_record_id: int | None = Field(default=None, ge=1)
    observed_at: datetime | date | None = None
    cached_at: datetime | None = None

    @property
    def key(self) -> QualityKey:
        return (
            self.capability,
            self.subject.subject_type,
            self.subject.subject_id,
            canonical_semantic_key(self.subject.semantic_key),
            self.persisted_quality_record_id,
        )


class EffectiveQualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str
    subject_type: SubjectType
    subject_id: str
    semantic_key: str | None
    stored_quality: DataQualityStatus | None
    freshness_quality: DataQualityStatus
    newest_signal_quality: DataQualityStatus | None
    effective_quality: DataQualityStatus
    executable: bool
    observed_at: datetime | date | None
    evaluated_at: datetime
    blocking_record_id: int | None
    blocking_reason: str | None
    source_quality_record_ids: list[int]
    requires_refresh: bool


__all__ = [
    "EffectiveQualityRequest",
    "EffectiveQualityResult",
    "QualityKey",
    "SubjectRef",
    "SubjectType",
    "canonical_semantic_key",
]
