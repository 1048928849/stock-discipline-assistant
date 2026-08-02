from typing import Protocol

from app.domain.risk.models import RiskContext, RiskEvaluationResult


class RiskEvaluator(Protocol):
    def __call__(self, context: RiskContext) -> RiskEvaluationResult: ...
