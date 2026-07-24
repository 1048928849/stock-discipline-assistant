from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from app.schemas_workflow import TradePlanAIRequest


class ResearchOrchestrator(Protocol):
    orchestrator_id: str

    def run(self, request: TradePlanAIRequest) -> dict[str, Any]: ...


class ExistingAIResearchOrchestrator:
    """Single-pass adapter; later phases can replace it without changing the pipeline."""

    orchestrator_id = "single_pass_existing_ai"

    def __init__(self, runner: Callable[[TradePlanAIRequest], dict[str, Any]]):
        self._runner = runner

    def run(self, request: TradePlanAIRequest) -> dict[str, Any]:
        return self._runner(request)
