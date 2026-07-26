from __future__ import annotations

from decimal import Decimal

from app.analysis.contracts import MarketRegime, MarketRegimeInput


_CAPS = {
    "PANIC": Decimal("0"),
    "REPAIR": Decimal("0.3"),
    "EXPANSION": Decimal("1"),
    "DIVERGENCE": Decimal("0.5"),
    "CONTRACTION": Decimal("0.2"),
}


def _average(values) -> Decimal | None:
    rows = list(values)
    return sum(rows, Decimal("0")) / len(rows) if rows else None


def infer_market_state_without_history(
    current,
    *,
    amount_history,
    index_changes,
) -> str:
    total = current.advancing + current.declining + current.unchanged
    advance_ratio = Decimal(current.advancing) / Decimal(total) if total else None
    current_amount = amount_history[-1] if amount_history else None
    average_20 = _average(amount_history[-21:-1])
    amount_ratio_20 = current_amount / average_20 if current_amount and average_20 else None
    index_average = _average(index_changes.values()) or Decimal("0")
    if (
        advance_ratio is not None
        and advance_ratio <= Decimal("0.2")
        and current.limit_down >= 30
        and (current.median_change_pct or Decimal("0")) < Decimal("-1")
    ):
        return "PANIC"
    if advance_ratio is not None and (
        (index_average > 0 and advance_ratio < Decimal("0.45"))
        or (current.limit_up >= 40 and advance_ratio < Decimal("0.45"))
    ):
        return "DIVERGENCE"
    if (
        advance_ratio is not None
        and advance_ratio >= Decimal("0.65")
        and current.limit_up >= 50
        and amount_ratio_20 is not None
        and amount_ratio_20 >= Decimal("1.05")
        and (
            current.above_ma20_ratio is None
            or current.above_ma20_ratio >= Decimal("0.55")
        )
    ):
        return "EXPANSION"
    return "CONTRACTION"


def analyze_market_regime(value: MarketRegimeInput) -> MarketRegime:
    current = value.current
    total = current.advancing + current.declining + current.unchanged
    advance_ratio = Decimal(current.advancing) / Decimal(total) if total else None
    current_amount = value.amount_history[-1] if value.amount_history else None
    average_5 = _average(value.amount_history[-6:-1])
    average_20 = _average(value.amount_history[-21:-1])
    amount_ratio_5 = current_amount / average_5 if current_amount and average_5 else None
    amount_ratio_20 = current_amount / average_20 if current_amount and average_20 else None
    supporting = []
    conflicting = []

    base_state = infer_market_state_without_history(
        current,
        amount_history=value.amount_history,
        index_changes=value.index_changes,
    )
    panic = base_state == "PANIC"
    divergence = base_state == "DIVERGENCE"
    expansion = base_state == "EXPANSION"
    repair = bool(
        advance_ratio is not None
        and advance_ratio >= Decimal("0.55")
        and value.previous_state in {"PANIC", "CONTRACTION"}
    )
    if panic:
        state = "PANIC"
        supporting.extend(("decliners dominate", "limit-down pressure", "negative median"))
    elif divergence:
        state = "DIVERGENCE"
        supporting.extend(("index/breadth divergence", "narrow participation"))
    elif expansion:
        state = "EXPANSION"
        supporting.extend(("broad advance", "limit-up expansion", "amount expansion"))
    elif repair:
        state = "REPAIR"
        supporting.extend(("breadth repair", f"recovery from {value.previous_state}"))
        if amount_ratio_20 is not None and amount_ratio_20 < Decimal("0.9"):
            conflicting.append("repair volume is contracting")
    else:
        state = "CONTRACTION"
        supporting.append("conditions do not support active expansion")
        if advance_ratio is not None and advance_ratio > Decimal("0.5"):
            conflicting.append("breadth remains positive")
    fields = (
        advance_ratio,
        current.median_change_pct,
        current.above_ma20_ratio,
        current.above_ma50_ratio,
        amount_ratio_20,
    )
    completeness = Decimal(sum(item is not None for item in fields)) / Decimal(len(fields))
    confidence = "HIGH" if completeness >= Decimal("0.8") else (
        "MEDIUM" if completeness >= Decimal("0.6") else "LOW"
    )
    if confidence == "LOW":
        conflicting.append("market inputs are incomplete")
    return MarketRegime(
        state=state,
        previous_state=value.previous_state,
        transition=f"{value.previous_state}->{state}",
        data_completeness=completeness,
        supporting_indicators=tuple(supporting),
        conflicting_indicators=tuple(conflicting),
        confidence=confidence,
        offensive_allowed=state == "EXPANSION" and confidence != "LOW",
        market_position_cap=_CAPS[state],
        advance_ratio=advance_ratio,
        amount_ratio_5d=amount_ratio_5,
        amount_ratio_20d=amount_ratio_20,
    )


__all__ = ["analyze_market_regime", "infer_market_state_without_history"]
