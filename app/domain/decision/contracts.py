from typing import Protocol

from app.domain.decision.models import DecisionContext, DecisionResult


class DecisionEvaluator(Protocol):
    def __call__(self, context: DecisionContext) -> DecisionResult: ...
