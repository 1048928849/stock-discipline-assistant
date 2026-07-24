from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.domain.quality import DataQualityStatus, worst_quality


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(DomainModel):
    evidence_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]+$", min_length=3, max_length=160)
    symbol: str = Field(min_length=1, max_length=20)
    capability: str = Field(min_length=1, max_length=80)
    required: bool
    category: str = Field(min_length=1, max_length=80)
    source_name: str = Field(min_length=1, max_length=200)
    source_url: str | None = Field(default=None, max_length=2000)
    observed_at: str | None = Field(default=None, max_length=80)
    fetched_at: str | None = Field(default=None, max_length=80)
    cached_at: str | None = Field(default=None, max_length=80)
    quality_status: DataQualityStatus
    payload: dict[str, Any]
    is_primary: bool = False
    external_text_is_untrusted: bool = True

    @model_validator(mode="after")
    def bound_payload(self):
        encoded = json.dumps(self.payload, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > 128_000:
            raise ValueError("Evidence payload exceeds 128 KB")
        return self


class MarketSnapshot(DomainModel):
    symbol: str
    as_of: str | None
    market_state: str
    sector_state: str
    current_price: Decimal | None
    quality_status: DataQualityStatus
    evidence_ids: list[str] = Field(max_length=50)


class StrategyDecision(DomainModel):
    rule_status: str
    executable_status: str
    decision_code: str
    label: str
    next_action: str
    rule_version: str
    evidence_ids: list[str] = Field(max_length=100)
    authority: Literal["deterministic_rule_engine"] = "deterministic_rule_engine"


class RiskDecision(DomainModel):
    status: str
    final_position_quantity: int
    trial_quantity: int
    hard_stop: Decimal | None
    hard_stop_triggered: bool
    calculations: dict[str, Any]
    evidence_ids: list[str] = Field(max_length=50)
    authority: Literal["deterministic_risk_engine"] = "deterministic_risk_engine"


class ResearchClaim(DomainModel):
    text: str = Field(min_length=1, max_length=3000)
    evidence_ids: list[str] = Field(
        min_length=1,
        max_length=20,
        validation_alias=AliasChoices("evidence_ids", "source_ids"),
    )
    confidence: Literal["high", "medium", "low"]
    claim_type: Literal["fact", "inference", "risk", "conflict"]


class ResearchUncertainty(DomainModel):
    text: str = Field(min_length=1, max_length=1000)
    uncertainty_type: Literal["uncertainty", "missing_information", "process_note"]


class ResearchResult(DomainModel):
    claims: list[ResearchClaim] = Field(default_factory=list, max_length=160)
    uncertainties: list[ResearchUncertainty] = Field(default_factory=list, max_length=80)

    @property
    def evidence_ids(self) -> set[str]:
        return {
            evidence_id
            for claim in self.claims
            for evidence_id in claim.evidence_ids
        }

    @model_validator(mode="after")
    def bound_result(self):
        encoded = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 256_000:
            raise ValueError("ResearchResult exceeds 256 KB")
        return self


class ResearchDecision(DomainModel):
    status: str
    ai_status: str
    evidence_ids: list[str] = Field(max_length=200)
    missing_data: list[str] = Field(max_length=100)
    result: ResearchResult | None
    orchestrator: str
    research_completeness: int = Field(ge=0, le=100)
    missing_optional_evidence: list[str] = Field(default_factory=list, max_length=100)
    may_modify_rule_state: Literal[False] = False
    may_modify_position: Literal[False] = False
    may_modify_hard_stop: Literal[False] = False


class QualitySnapshotItem(DomainModel):
    capability: str
    required: bool
    quality_status: DataQualityStatus
    evidence_ids: list[str] = Field(max_length=100)
    observed_at: list[str] = Field(default_factory=list, max_length=100)
    providers: list[str] = Field(default_factory=list, max_length=100)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DecisionPackage(DomainModel):
    schema_version: Literal["1.1"] = "1.1"
    package_id: str
    created_at: datetime
    generated_at: datetime
    expires_at: datetime
    market_snapshot: MarketSnapshot
    evidence: list[Evidence] = Field(max_length=300)
    strategy_decision: StrategyDecision
    risk_decision: RiskDecision
    research_decision: ResearchDecision
    quality_status: DataQualityStatus
    ready_allowed: bool
    freeze_allowed: bool
    blocked_reasons: list[str] = Field(max_length=100)
    required_capabilities: list[str] = Field(min_length=1, max_length=100)
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    quality_snapshot: dict[str, QualitySnapshotItem]
    rule_snapshot: dict[str, Any]
    account_snapshot: dict[str, Any]
    legacy_preview_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    package_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    def evidence_digest_value(self) -> str:
        rows = [
            {
                "evidence_id": item.evidence_id,
                "capability": item.capability,
                "required": item.required,
                "quality_status": item.quality_status.value,
                "source_name": item.source_name,
                "observed_at": item.observed_at,
                "fetched_at": item.fetched_at,
                "cached_at": item.cached_at,
                "payload": item.payload,
            }
            for item in sorted(self.evidence, key=lambda row: row.evidence_id)
            if item.required
        ]
        return _canonical_hash(rows)

    def package_hash_value(self) -> str:
        payload = self.model_dump(mode="json", exclude={"package_hash"})
        return _canonical_hash(payload)

    @model_validator(mode="after")
    def validate_evidence_and_execution_gate(self):
        evidence_ids = [item.evidence_id for item in self.evidence]
        known = set(evidence_ids)
        if len(known) != len(evidence_ids):
            raise ValueError("DecisionPackage evidence_id values must be unique")
        references = {
            *self.market_snapshot.evidence_ids,
            *self.strategy_decision.evidence_ids,
            *self.risk_decision.evidence_ids,
            *self.research_decision.evidence_ids,
        }
        if self.research_decision.result:
            references.update(self.research_decision.result.evidence_ids)
        unknown = sorted(references - known)
        if unknown:
            raise ValueError(f"DecisionPackage references unknown evidence_id: {unknown}")

        required = set(self.required_capabilities)
        represented = {item.capability for item in self.evidence if item.required}
        missing_capabilities = sorted(required - represented)
        if missing_capabilities:
            raise ValueError(
                f"DecisionPackage missing required capabilities: {missing_capabilities}"
            )
        required_statuses = [
            item.quality_status
            for item in self.evidence
            if item.required and item.capability in required
        ]
        computed_quality = worst_quality(required_statuses)
        if computed_quality != self.quality_status:
            raise ValueError("DecisionPackage quality_status does not match required Evidence")
        if self.market_snapshot.quality_status != computed_quality:
            raise ValueError("MarketSnapshot quality_status does not match DecisionPackage")
        expected_snapshot = {}
        for capability in {item.capability for item in self.evidence}:
            rows = [item for item in self.evidence if item.capability == capability]
            expected_snapshot[capability] = {
                "required": any(item.required for item in rows),
                "quality_status": worst_quality(
                    [item.quality_status for item in rows]
                ),
                "evidence_ids": sorted(item.evidence_id for item in rows),
                "providers": sorted({item.source_name for item in rows}),
                "observed_at": sorted(
                    item.observed_at
                    for item in rows
                    if item.observed_at is not None
                ),
            }
        if set(expected_snapshot) != set(self.quality_snapshot):
            raise ValueError("DecisionPackage quality_snapshot capability mismatch")
        for capability, expected in expected_snapshot.items():
            actual = self.quality_snapshot[capability]
            if (
                actual.required != expected["required"]
                or actual.quality_status != expected["quality_status"]
                or sorted(actual.evidence_ids) != expected["evidence_ids"]
                or sorted(actual.providers) != expected["providers"]
                or sorted(actual.observed_at) != expected["observed_at"]
            ):
                raise ValueError(
                    f"DecisionPackage quality_snapshot mismatch for {capability}"
                )
        if self.evidence_digest != "0" * 64 and self.evidence_digest_value() != self.evidence_digest:
            raise ValueError("DecisionPackage evidence_digest mismatch")
        if self.package_hash != "0" * 64 and self.package_hash_value() != self.package_hash:
            raise ValueError("DecisionPackage package_hash mismatch")
        if computed_quality.blocks_execution:
            if self.ready_allowed or self.freeze_allowed:
                raise ValueError("blocked data quality cannot allow READY or plan freezing")
            if self.strategy_decision.executable_status == "READY":
                raise ValueError("blocked data quality cannot output READY")
        return self
