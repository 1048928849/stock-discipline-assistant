from __future__ import annotations

from app.decision.contracts import TradeDecisionContext, TradeDecisionResult
from app.decision.engine import build_trade_decision
from app.strategies.contracts import StrategyDecisionCandidate, StrategySignal


def candidates_from_signals(
    signals: tuple[StrategySignal, ...],
) -> tuple[StrategyDecisionCandidate, ...]:
    return tuple(
        StrategyDecisionCandidate.from_signal(signal)
        for signal in sorted(
            signals,
            key=lambda item: (item.strategy_id, item.strategy_version),
        )
    )


def combine_strategy_candidates(
    candidates: tuple[StrategyDecisionCandidate, ...],
    *,
    context: TradeDecisionContext,
) -> TradeDecisionResult:
    # Signals are advisory candidates. The deterministic global risk engine remains
    # the sole authority for position, quantity, hard stop, and executable status.
    result = build_trade_decision(context)
    if any(candidate.applicable for candidate in candidates):
        return result
    blocked_reasons = tuple(
        dict.fromkeys((*result.blocked_reasons, "no enabled strategy is applicable"))
    )
    return result.model_copy(
        update={
            "executable_status": "WAIT",
            "buy_allowed": False,
            "blocked_reasons": blocked_reasons,
            "position_constraints": result.position_constraints.model_copy(
                update={
                    "trial_quantity": 0,
                    "target_quantity": 0,
                    "maximum_quantity": 0,
                }
            ),
            "risk_plan": result.risk_plan.model_copy(
                update={"maximum_plan_loss": 0}
            ),
        }
    )


__all__ = ["candidates_from_signals", "combine_strategy_candidates"]
