from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data_hub.quality import DataQualityStatus


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(DomainModel):
    evidence_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]+$", min_length=3, max_length=160)
    symbol: str
    category: str
    source_name: str
    source_url: str | None = None
    observed_at: str | None = None
    fetched_at: str | None = None
    quality_status: DataQualityStatus
    payload: dict[str, Any]
    is_primary: bool = False
    external_text_is_untrusted: bool = True


class MarketSnapshot(DomainModel):
    symbol: str
    as_of: str | None
    market_state: str
    sector_state: str
    current_price: Decimal | None
    quality_status: DataQualityStatus
    evidence_ids: list[str]


class StrategyDecision(DomainModel):
    rule_status: str
    executable_status: str
    decision_code: str
    label: str
    next_action: str
    rule_version: str
    evidence_ids: list[str]
    authority: Literal["deterministic_rule_engine"] = "deterministic_rule_engine"


class RiskDecision(DomainModel):
    status: str
    final_position_quantity: int
    trial_quantity: int
    hard_stop: Decimal | None
    hard_stop_triggered: bool
    calculations: dict[str, Any]
    evidence_ids: list[str]
    authority: Literal["deterministic_risk_engine"] = "deterministic_risk_engine"


class ResearchDecision(DomainModel):
    status: str
    ai_status: str
    evidence_ids: list[str]
    missing_data: list[str]
    result: dict[str, Any] | None
    orchestrator: str
    may_modify_rule_state: Literal[False] = False
    may_modify_position: Literal[False] = False
    may_modify_hard_stop: Literal[False] = False


class DecisionPackage(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    package_id: str
    created_at: datetime
    market_snapshot: MarketSnapshot
    evidence: list[Evidence]
    strategy_decision: StrategyDecision
    risk_decision: RiskDecision
    research_decision: ResearchDecision
    quality_status: DataQualityStatus
    ready_allowed: bool
    freeze_allowed: bool
    blocked_reasons: list[str]
    legacy_preview_hash: str

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
        unknown = sorted(references - known)
        if unknown:
            raise ValueError(f"DecisionPackage references unknown evidence_id: {unknown}")
        if self.quality_status.blocks_execution:
            if self.ready_allowed or self.freeze_allowed:
                raise ValueError("blocked data quality cannot allow READY or plan freezing")
            if self.strategy_decision.executable_status == "READY":
                raise ValueError("blocked data quality cannot output READY")
        return self
