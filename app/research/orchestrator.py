from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from app.domain.models import ResearchResult
from app.domain.package_builder import research_result_from_ai


class ResearchExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=40)
    result: ResearchResult | None
    error: str | None = Field(default=None, max_length=2000)
    _legacy_payload: dict[str, Any] = PrivateAttr(default_factory=dict)

    @classmethod
    def from_legacy_payload(cls, payload: dict[str, Any]) -> ResearchExecution:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > 512_000:
            raise ValueError("Research execution payload exceeds 512 KB")
        instance = cls(
            status=str(payload.get("status") or "failed"),
            result=research_result_from_ai(payload.get("result")),
            error=payload.get("error"),
        )
        instance._legacy_payload = payload
        return instance

    @property
    def legacy_payload(self) -> dict[str, Any]:
        return self._legacy_payload

    @model_validator(mode="after")
    def require_result_for_success(self):
        if self.status == "success" and self.result is None:
            raise ValueError("Successful research execution requires a typed ResearchResult")
        return self


class ResearchOrchestrator(Protocol):
    orchestrator_id: str

    def run(self, request: Any) -> ResearchExecution: ...


class ExistingAIResearchOrchestrator:
    """Single-pass adapter around the existing isolated AI analysis."""

    orchestrator_id = "single_pass_existing_ai"

    def __init__(self, runner: Callable[[Any], dict[str, Any]]):
        self._runner = runner

    def run(self, request: Any) -> ResearchExecution:
        return ResearchExecution.from_legacy_payload(self._runner(request))
