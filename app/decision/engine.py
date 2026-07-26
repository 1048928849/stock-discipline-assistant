from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from app.decision.contracts import (
    BuyPointAssessment,
    ExitPlan,
    PositionConstraints,
    RiskPlan,
    TradeDecisionContext,
    TradeDecisionResult,
)


_MARKET_MULTIPLIER = {
    "EXPANSION": Decimal("1"),
    "REPAIR": Decimal("0.5"),
    "DIVERGENCE": Decimal("0.4"),
    "CONTRACTION": Decimal("0.25"),
    "PANIC": Decimal("0"),
}
_INDUSTRY_MULTIPLIER = {
    "MAINLINE": Decimal("1"),
    "SECONDARY": Decimal("0.8"),
    "ROTATION": Decimal("0.6"),
    "DIVERGENCE": Decimal("0.3"),
    "FADING": Decimal("0"),
    "NONE": Decimal("0.5"),
}
_TREND_MULTIPLIER = {
    "UP": Decimal("1"),
    "RANGE": Decimal("0.6"),
    "DOWN": Decimal("0.2"),
    "INSUFFICIENT": Decimal("0"),
}
_ENTRY_MULTIPLIER = {
    "BREAKOUT": Decimal("1"),
    "PULLBACK_CONFIRMATION": Decimal("0.8"),
    "DIVERGENCE_TO_CONSENSUS": Decimal("0.6"),
    "TREND_CONTINUATION": Decimal("0.7"),
    "OVERSOLD_REPAIR_WATCH": Decimal("0"),
    "NO_VALID_ENTRY": Decimal("0"),
}


def _buy_point(value: TradeDecisionContext) -> BuyPointAssessment:
    technical = value.technical
    market = value.market
    if technical.volume_breakout and not technical.false_breakout:
        kind = "BREAKOUT"
    elif technical.low_volume_pullback and technical.intraday_trend == "UP":
        kind = "PULLBACK_CONFIRMATION"
    elif market.state == "REPAIR" and technical.breakout:
        kind = "DIVERGENCE_TO_CONSENSUS"
    elif technical.daily_intraday_aligned and technical.intraday_trend == "UP":
        kind = "TREND_CONTINUATION"
    elif market.state == "REPAIR" and technical.daily_trend in {"DOWN", "RANGE"}:
        kind = "OVERSOLD_REPAIR_WATCH"
    else:
        kind = "NO_VALID_ENTRY"
    valid = kind not in {"OVERSOLD_REPAIR_WATCH", "NO_VALID_ENTRY"}
    return BuyPointAssessment(
        buy_point_type=kind,
        valid=valid,
        necessary_conditions=(
            "deterministic base rule remains READY",
            "required market data has executable quality",
            "daily and intraday structure is available",
        ),
        confirmation_conditions=(
            value.base_plan.trigger_condition,
            f"{kind} confirmation remains true at execution",
        ),
        invalidation_conditions=(
            value.base_plan.logical_invalidation,
            "hard stop is reached",
        ),
        required_data=(
            "market.daily.qfq",
            "market.intraday.60m",
            "market.turnover.daily",
            "market.breadth.daily",
            "market.industry.daily",
        ),
        quality_requirements=("VERIFIED or SINGLE_SOURCE",),
        prohibited_conditions=(
            "market PANIC",
            "industry FADING",
            "high announcement risk",
            "false breakout",
        ),
    )


def _lot_quantity(quantity: int, multiplier: Decimal) -> int:
    scaled = (Decimal(quantity) * multiplier / Decimal("100")).to_integral_value(
        rounding=ROUND_FLOOR
    )
    return int(scaled) * 100


def build_trade_decision(value: TradeDecisionContext) -> TradeDecisionResult:
    base = value.base_plan
    buy_point = _buy_point(value)
    midpoint = (base.buy_zone_low + base.buy_zone_high) / Decimal("2")
    stop_ratio = base.per_share_risk / midpoint if midpoint > 0 else Decimal("1")
    stop_multiplier = (
        Decimal("1")
        if stop_ratio <= Decimal("0.10")
        else (Decimal("0.5") if stop_ratio <= Decimal("0.15") else Decimal("0"))
    )
    constraints = {
        "account_risk_budget": Decimal("1"),
        "single_stock_limit": Decimal("1"),
        "market_regime": _MARKET_MULTIPLIER[value.market.state],
        "industry_state": _INDUSTRY_MULTIPLIER[
            value.industry.classification if value.industry else "NONE"
        ],
        "stock_trend": _TREND_MULTIPLIER[value.technical.daily_trend],
        "buy_point_quality": _ENTRY_MULTIPLIER[buy_point.buy_point_type],
        "stop_distance": stop_multiplier,
        "data_quality": Decimal("0")
        if value.data_quality.blocks_execution
        else Decimal("1"),
        "announcement_risk": {
            "LOW": Decimal("1"),
            "MEDIUM": Decimal("0.5"),
            "HIGH": Decimal("0"),
            "UNKNOWN": Decimal("0.5"),
        }[value.announcement_risk],
    }
    effective_name, effective_multiplier = min(
        constraints.items(), key=lambda item: item[1]
    )
    target_quantity = _lot_quantity(base.final_quantity, effective_multiplier)
    trial_quantity = base.trial_quantity if target_quantity >= base.trial_quantity else 0
    blocked = []
    if base.rule_status != "READY":
        blocked.append("deterministic rule status is not READY")
    if value.data_quality.blocks_execution:
        blocked.append(f"data quality is {value.data_quality.value}")
    if value.market.state == "PANIC":
        blocked.append("market is in PANIC")
    if value.industry and value.industry.classification == "FADING":
        blocked.append("industry is fading")
    if value.announcement_risk == "HIGH":
        blocked.append("high announcement risk")
    if value.technical.false_breakout:
        blocked.append("intraday structure is a false breakout")
    if not buy_point.valid:
        blocked.append("no valid entry")
    if target_quantity < 100:
        blocked.append("global position constraints allow no board lot")
    blocked = list(dict.fromkeys(blocked))
    allowed = not blocked
    if not allowed:
        target_quantity = 0
        trial_quantity = 0
    maximum_loss = min(
        base.maximum_loss,
        base.per_share_risk * Decimal(trial_quantity),
    )
    return TradeDecisionResult(
        rule_status=base.rule_status,
        executable_status="READY" if allowed else "WAIT",
        buy_allowed=allowed,
        blocked_reasons=tuple(blocked),
        buy_zone=(base.buy_zone_low, base.buy_zone_high),
        trigger_condition=base.trigger_condition,
        buy_point_assessment=buy_point,
        position_constraints=PositionConstraints(
            constraint_multipliers=constraints,
            effective_constraint=effective_name,
            effective_multiplier=effective_multiplier,
            trial_quantity=trial_quantity,
            target_quantity=target_quantity,
            maximum_quantity=target_quantity,
            maximum_position_pct=base.max_position_pct * effective_multiplier,
            rounding_explanation=(
                f"floor({base.final_quantity} * {effective_multiplier} / 100) * 100"
            ),
        ),
        risk_plan=RiskPlan(
            hard_stop=base.hard_stop,
            per_share_risk=base.per_share_risk,
            maximum_plan_loss=maximum_loss,
            logical_invalidation=base.logical_invalidation,
            add_condition=(
                "original plan valid, risk budget available, market and industry not downgraded, "
                "required Evidence unchanged, and deterministic price confirmation satisfied"
            ),
            no_add_condition=(
                "never average down after plan invalidation, market/industry downgrade, "
                "Evidence change, or hard-stop breach"
            ),
        ),
        exit_plan=ExitPlan(
            first_take_profit="reduce risk at the first deterministic 1R objective",
            second_take_profit="take further profit at the next resistance or 2R objective",
            trailing_stop="raise the stop only after a confirmed higher low",
            reduce_condition="reduce on market/industry downgrade or failed continuation",
            exit_condition="exit on hard stop or logical invalidation",
            no_trade_condition="do not trade while any required quality or risk gate blocks",
        ),
        next_session_observations=(
            "market regime and breadth transition",
            "industry classification and participation",
            "entry confirmation and turnover",
            "announcement and required Evidence changes",
        ),
    )


__all__ = ["build_trade_decision"]
