from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.selected_stock.contracts import (
    ContextStatus,
    StockRole,
    SurvivalRuleResult,
    SurvivalRuleStatus,
)


ZERO = Decimal("0")


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _rule(
    code: str,
    name: str,
    status: SurvivalRuleStatus,
    reason: str,
    evidence: dict[str, Any],
    action: str,
) -> SurvivalRuleResult:
    return SurvivalRuleResult(
        rule_code=code,
        rule_name=name,
        status=status,
        severity=(
            "CRITICAL"
            if status == SurvivalRuleStatus.BLOCK
            else "WARNING"
            if status in {SurvivalRuleStatus.WARN, SurvivalRuleStatus.INSUFFICIENT_DATA}
            else "INFO"
        ),
        evidence=evidence,
        action=action,
        applicable=status != SurvivalRuleStatus.NOT_APPLICABLE,
        reason_code=reason,
    )


def evaluate_survival_discipline(
    *,
    indicators: dict[str, Any],
    current_price: Decimal,
    entry_high: Decimal,
    hard_stop: Decimal,
    proposed_position_pct: Decimal,
    has_position: bool,
    market_status: ContextStatus,
    industry_status: ContextStatus,
    stock_role: StockRole,
) -> tuple[SurvivalRuleResult, ...]:
    bullish = bool(indicators.get("bullish_alignment"))
    bearish = bool(indicators.get("bearish_alignment"))
    if market_status != ContextStatus.AVAILABLE or industry_status != ContextStatus.AVAILABLE:
        trend_status = SurvivalRuleStatus.INSUFFICIENT_DATA
        trend_reason = "TREND_CONTEXT_INSUFFICIENT"
    elif bearish and proposed_position_pct > 0:
        trend_status = SurvivalRuleStatus.BLOCK
        trend_reason = "BEARISH_TREND_CANNOT_EXPAND_POSITION"
    elif not bullish and proposed_position_pct > 0:
        trend_status = SurvivalRuleStatus.WARN
        trend_reason = "TREND_NOT_CONFIRMED"
    else:
        trend_status = SurvivalRuleStatus.PASS
        trend_reason = "TREND_POSITION_ALIGNED"
    trend = _rule(
        "TREND_POSITION_COORDINATION",
        "Trend and position coordination",
        trend_status,
        trend_reason,
        {
            "market_status": market_status.value,
            "industry_status": industry_status.value,
            "bullish_alignment": bullish,
            "bearish_alignment": bearish,
            "proposed_position_pct": proposed_position_pct,
        },
        "Do not expand position without aligned market, industry, and stock trend.",
    )

    returns = indicators.get("returns") or {}
    sma20 = _decimal((indicators.get("sma") or {}).get("20"))
    atr_pct = _decimal(indicators.get("atr_pct"))
    return5 = _decimal(returns.get("5"))
    ma_deviation = current_price / sma20 - Decimal("1") if sma20 else None
    entry_deviation = current_price / entry_high - Decimal("1") if entry_high else None
    chase_flags = (
        return5 is not None and return5 >= Decimal("0.10"),
        ma_deviation is not None and ma_deviation >= Decimal("0.08"),
        entry_deviation is not None and entry_deviation >= Decimal("0.08"),
        atr_pct is not None
        and entry_deviation is not None
        and atr_pct > 0
        and entry_deviation / atr_pct >= Decimal("3"),
        bool(indicators.get("momentum_accelerating")),
    )
    chase_count = sum(chase_flags)
    chase_status = (
        SurvivalRuleStatus.BLOCK
        if chase_count >= 3
        else SurvivalRuleStatus.WARN
        if chase_count >= 2
        else SurvivalRuleStatus.PASS
    )
    chase = _rule(
        "CHASE_RISK",
        "Chasing risk",
        chase_status,
        "CHASE_RISK_BLOCKED"
        if chase_status == SurvivalRuleStatus.BLOCK
        else "CHASE_RISK_ELEVATED"
        if chase_status == SurvivalRuleStatus.WARN
        else "CHASE_RISK_ACCEPTABLE",
        {
            "return_5": return5,
            "ma20_deviation": ma_deviation,
            "entry_zone_deviation": entry_deviation,
            "atr_pct": atr_pct,
            "momentum_accelerating": indicators.get("momentum_accelerating"),
            "trigger_count": chase_count,
        },
        "Wait for price to return to a validated entry structure when chasing risk is high.",
    )

    candle = indicators.get("latest_candle") or {}
    percentile = _decimal(candle.get("price_percentile_250"))
    volume_ratio = _decimal(indicators.get("volume_ratio_20"))
    body_pct = _decimal(candle.get("body_pct"))
    upper_wick = _decimal(candle.get("upper_wick_pct"))
    close_position = _decimal(candle.get("close_position"))
    if any(
        value is None
        for value in (percentile, volume_ratio, body_pct, upper_wick, close_position)
    ):
        stall_status = SurvivalRuleStatus.INSUFFICIENT_DATA
        stall_reason = "HIGH_VOLUME_STALL_INPUTS_INSUFFICIENT"
    else:
        stalled = bool(
            percentile >= Decimal("0.80")
            and volume_ratio >= Decimal("1.50")
            and body_pct <= Decimal("0.02")
            and upper_wick >= body_pct
        )
        stall_status = (
            SurvivalRuleStatus.BLOCK
            if stalled and close_position <= Decimal("0.35")
            else SurvivalRuleStatus.WARN
            if stalled
            else SurvivalRuleStatus.PASS
        )
        stall_reason = (
            "HIGH_VOLUME_STALL_BLOCKED"
            if stall_status == SurvivalRuleStatus.BLOCK
            else "HIGH_VOLUME_STALL_WARNING"
            if stall_status == SurvivalRuleStatus.WARN
            else "HIGH_VOLUME_STALL_NOT_PRESENT"
        )
    high_stall = _rule(
        "HIGH_VOLUME_STALL",
        "High-level volume stall",
        stall_status,
        stall_reason,
        {
            "price_percentile_250": percentile,
            "volume_ratio_20": volume_ratio,
            "body_pct": body_pct,
            "upper_wick_pct": upper_wick,
            "close_position": close_position,
        },
        "Do not add when high-level volume cannot produce price progress.",
    )

    support_low = _decimal((indicators.get("support_zone") or [None])[0])
    down_on_volume = bool(indicators.get("down_on_volume"))
    below_ma20 = sma20 is not None and current_price < sma20
    structure_broken = support_low is not None and current_price < support_low
    decline_status = (
        SurvivalRuleStatus.BLOCK
        if down_on_volume and below_ma20 and structure_broken
        else SurvivalRuleStatus.WARN
        if down_on_volume and below_ma20
        else SurvivalRuleStatus.PASS
    )
    decline = _rule(
        "VOLUME_DECLINE_BREAKDOWN",
        "Volume decline and structure breakdown",
        decline_status,
        "VOLUME_DECLINE_STRUCTURE_BROKEN"
        if decline_status == SurvivalRuleStatus.BLOCK
        else "VOLUME_DECLINE_WARNING"
        if decline_status == SurvivalRuleStatus.WARN
        else "DECLINE_BREAKDOWN_NOT_PRESENT",
        {
            "down_on_volume": down_on_volume,
            "below_ma20": below_ma20,
            "support_low": support_low,
            "current_price": current_price,
            "structure_broken": structure_broken,
        },
        "Reduce or exit when volume-backed decline breaks the validated structure.",
    )

    if industry_status != ContextStatus.AVAILABLE or stock_role == StockRole.UNKNOWN:
        role_status = SurvivalRuleStatus.INSUFFICIENT_DATA
        role_reason = "INDUSTRY_OR_ROLE_EVIDENCE_INSUFFICIENT"
    elif stock_role == StockRole.FOLLOWER:
        role_status = SurvivalRuleStatus.BLOCK
        role_reason = "FOLLOWER_CANNOT_RECEIVE_CORE_POSITION"
    elif stock_role in {StockRole.LEADER, StockRole.CAPACITY_CORE, StockRole.TREND_CORE}:
        role_status = SurvivalRuleStatus.PASS
        role_reason = "CORE_ROLE_EVIDENCE_AVAILABLE"
    else:
        role_status = SurvivalRuleStatus.WARN
        role_reason = "NON_CORE_ROLE_REQUIRES_CAUTION"
    role_rule = _rule(
        "INDUSTRY_ROLE_RISK",
        "Mainline and stock role risk",
        role_status,
        role_reason,
        {"industry_status": industry_status.value, "stock_role": stock_role.value},
        "Do not label or size a stock as a leader without complete industry evidence.",
    )

    stop_hit = current_price <= hard_stop
    stop_rule = _rule(
        "HARD_STOP_PRIORITY",
        "Hard stop priority",
        SurvivalRuleStatus.BLOCK if stop_hit else SurvivalRuleStatus.PASS,
        "HARD_STOP_TRIGGERED" if stop_hit else "HARD_STOP_NOT_TRIGGERED",
        {
            "current_price": current_price,
            "hard_stop": hard_stop,
            "override_sources_allowed": [],
            "has_position": has_position,
        },
        "Exit risk has priority; valuation, volume, fear, catalysts, and user opinion cannot override it.",
    )
    return (trend, chase, high_stall, decline, role_rule, stop_rule)


__all__ = ["evaluate_survival_discipline"]
