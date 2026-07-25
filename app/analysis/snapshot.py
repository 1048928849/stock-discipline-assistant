from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.quality import DataQualityStatus
from app.domain.quality_subject import SubjectRef
from app.domain.hashing import canonical_hash


_HASH = re.compile(r"^[0-9a-f]{64}$")


class SnapshotCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str = Field(min_length=1, max_length=80)
    subject: SubjectRef
    required: bool
    quality_status: DataQualityStatus
    executable: bool
    quality_record_id: int | None = Field(default=None, ge=1)
    observed_at: datetime | None
    fetched_at: datetime | None
    normalized_digest: str | None
    rows: tuple[dict[str, Any], ...]
    evidence_refs: tuple[str, ...]

    @field_validator("observed_at", "fetched_at")
    @classmethod
    def aware_time(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("snapshot capability times must be aware")
        return value

    @field_validator("normalized_digest")
    @classmethod
    def digest_format(cls, value):
        if value is not None and not _HASH.fullmatch(value):
            raise ValueError("invalid snapshot capability digest")
        return value

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def stable_refs(cls, value):
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def executable_has_exact_lineage(self):
        if self.executable and (
            self.quality_status.blocks_execution
            or self.quality_record_id is None
            or self.observed_at is None
            or self.fetched_at is None
            or self.normalized_digest is None
        ):
            raise ValueError("executable snapshot capability requires exact trusted lineage")
        return self


class ProductAnalysisSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis_started_at: datetime
    symbol: str = Field(pattern=r"^\d{6}$")
    capabilities: tuple[SnapshotCapability, ...]
    snapshot_hash: str | None = None

    @field_validator("analysis_started_at")
    @classmethod
    def aware_start(cls, value):
        if value.tzinfo is None:
            raise ValueError("analysis_started_at must be aware")
        return value

    @model_validator(mode="after")
    def deterministic_hash(self):
        scopes = [
            (
                item.capability,
                item.subject.subject_type,
                item.subject.subject_id,
                item.subject.semantic_key or "",
            )
            for item in self.capabilities
        ]
        if len(scopes) != len(set(scopes)):
            raise ValueError("snapshot contains duplicate capability scope")
        expected = canonical_hash(
            self.model_dump(mode="python", exclude={"snapshot_hash"})
        )
        if self.snapshot_hash is not None and self.snapshot_hash != expected:
            raise ValueError("snapshot_hash does not match snapshot content")
        object.__setattr__(self, "snapshot_hash", expected)
        return self

    def has_capability(self, capability: str) -> bool:
        return any(
            item.capability == capability and item.executable
            for item in self.capabilities
        )

    def evidence_refs_for(self, capability: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    reference
                    for item in self.capabilities
                    if item.capability == capability
                    for reference in item.evidence_refs
                }
            )
        )


__all__ = ["ProductAnalysisSnapshot", "SnapshotCapability"]
