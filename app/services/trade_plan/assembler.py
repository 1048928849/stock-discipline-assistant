from collections.abc import Mapping
from typing import Any

from app.domain.trade_plan import TradePlanPreview


def assemble_preview(
    symbol: str,
    sections: Mapping[str, Any],
    *,
    strategy_result: Any | None = None,
    risk_result: Any | None = None,
    decision_result: Any | None = None,
) -> TradePlanPreview:
    """Combine computed engine outputs into an in-memory preview without recalculation."""
    # Explicit parameters document the assembly boundary. Legacy JSON remains unchanged.
    _ = strategy_result, risk_result, decision_result
    return TradePlanPreview(symbol=symbol, payload=sections)
