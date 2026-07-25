from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.data_hub.quality import QUALITY_POLICIES
from app.strategies.hashing import canonical_hash, validate_parameter_schema


_STRATEGY_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMPATIBILITY_ALIASES = {"market.quote", "market.daily"}
FORMAL_DATA_CAPABILITIES = frozenset(QUALITY_POLICIES) - _COMPATIBILITY_ALIASES


class StrategyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str
    version: str
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    time_horizon: str = Field(min_length=1, max_length=80)
    required_capabilities: tuple[str, ...]
    parameter_schema: dict[str, Any]
    default_parameters: dict[str, Any]
    applicable_market_regimes: tuple[str, ...]
    source_evidence: tuple[str, ...]
    implementation_hash: str

    @field_validator("strategy_id")
    @classmethod
    def valid_strategy_id(cls, value: str) -> str:
        if not _STRATEGY_ID.fullmatch(value):
            raise ValueError("invalid strategy_id")
        return value

    @field_validator("version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        if not _VERSION.fullmatch(value):
            raise ValueError("invalid strategy version")
        return value

    @field_validator(
        "required_capabilities",
        "applicable_market_regimes",
        "source_evidence",
        mode="before",
    )
    @classmethod
    def stable_tuple(cls, value) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("strategy manifest sequence is invalid")
        normalized = tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
        return normalized

    @field_validator("implementation_hash")
    @classmethod
    def valid_implementation_hash(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("invalid implementation_hash")
        return value

    @model_validator(mode="after")
    def valid_contract(self):
        if not self.required_capabilities:
            raise ValueError("strategy requires at least one DataHub capability")
        unknown = sorted(set(self.required_capabilities) - FORMAL_DATA_CAPABILITIES)
        if unknown:
            raise ValueError(f"unknown formal DataHub capabilities: {unknown}")
        if not self.applicable_market_regimes:
            raise ValueError("applicable_market_regimes must not be empty")
        if not self.source_evidence:
            raise ValueError("source_evidence must not be empty")
        validate_parameter_schema(self.default_parameters, self.parameter_schema)
        return self

    def manifest_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="python"))


class StrategySignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str
    strategy_version: str
    applicable: bool
    signal_type: str = Field(min_length=1, max_length=80)
    signal_strength: Decimal = Field(ge=0, le=1)
    entry_assessment: dict[str, Any]
    invalidation: str | None = Field(default=None, max_length=1000)
    suggested_risk_level: str = Field(min_length=1, max_length=40)
    holding_horizon: str = Field(min_length=1, max_length=80)
    evidence_refs: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    parameter_hash: str
    signal_hash: str | None = None

    @field_validator("strategy_id")
    @classmethod
    def valid_strategy_id(cls, value: str) -> str:
        if not _STRATEGY_ID.fullmatch(value):
            raise ValueError("invalid strategy_id")
        return value

    @field_validator("strategy_version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        if not _VERSION.fullmatch(value):
            raise ValueError("invalid strategy version")
        return value

    @field_validator("evidence_refs", "blocked_reasons", mode="before")
    @classmethod
    def stable_tuple(cls, value) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("strategy signal sequence is invalid")
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))

    @field_validator("parameter_hash")
    @classmethod
    def valid_parameter_hash(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("invalid parameter_hash")
        return value

    @model_validator(mode="after")
    def deterministic_signal_hash(self):
        payload = self.model_dump(mode="python", exclude={"signal_hash"})
        expected = canonical_hash(payload)
        if self.signal_hash is not None and self.signal_hash != expected:
            raise ValueError("signal_hash does not match signal content")
        object.__setattr__(self, "signal_hash", expected)
        return self


__all__ = [
    "FORMAL_DATA_CAPABILITIES",
    "StrategyManifest",
    "StrategySignal",
]
