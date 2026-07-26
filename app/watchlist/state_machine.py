from __future__ import annotations

from app.watchlist.contracts import (
    IndustryStatusInvalidationRule,
    InvalidationRuleSpec,
    MarketRegimeInvalidationRule,
    MonitoringHealth,
    MonitoringRuleType,
    PriceInvalidationRule,
    PriceStateInput,
    PriceStateOutcome,
    WatchlistStatus,
)
from pydantic import TypeAdapter


_AUTOMATIC_TRANSITIONS = {
    WatchlistStatus.DISCOVERED: {WatchlistStatus.RESEARCHING},
    WatchlistStatus.RESEARCHING: {
        WatchlistStatus.WATCHING,
        WatchlistStatus.NEAR_ENTRY,
        WatchlistStatus.ENTRY_TRIGGERED,
        WatchlistStatus.INVALIDATED,
    },
    WatchlistStatus.WATCHING: {
        WatchlistStatus.NEAR_ENTRY,
        WatchlistStatus.ENTRY_TRIGGERED,
        WatchlistStatus.INVALIDATED,
    },
    WatchlistStatus.NEAR_ENTRY: {
        WatchlistStatus.WATCHING,
        WatchlistStatus.ENTRY_TRIGGERED,
        WatchlistStatus.INVALIDATED,
    },
    WatchlistStatus.ENTRY_TRIGGERED: {
        WatchlistStatus.WATCHING,
        WatchlistStatus.NEAR_ENTRY,
        WatchlistStatus.INVALIDATED,
    },
    WatchlistStatus.INVALIDATED: set(),
    WatchlistStatus.ARCHIVED: set(),
}


MARKET_RISK_ORDER = {
    "EXPANSION": 0,
    "REPAIR": 1,
    "DIVERGENCE": 2,
    "CONTRACTION": 3,
    "PANIC": 4,
}
INDUSTRY_RISK_ORDER = {
    "MAINLINE": 0,
    "SECONDARY": 1,
    "ROTATION": 2,
    "FADING": 3,
    "NONE": 4,
}
_RULE_ADAPTER = TypeAdapter(InvalidationRuleSpec)


def risk_increased(previous: str | None, current: str | None, order: dict[str, int]) -> bool:
    return (
        previous in order
        and current in order
        and order[current] > order[previous]
    )


def matching_invalidation_rule(
    specs: list[dict],
    *,
    current_price,
    market_state: str | None,
    industry_state: str | None,
) -> InvalidationRuleSpec | None:
    for raw in specs:
        rule = _RULE_ADAPTER.validate_python(raw)
        if isinstance(rule, PriceInvalidationRule):
            matched = current_price is not None and current_price <= rule.threshold
        elif isinstance(rule, MarketRegimeInvalidationRule):
            matched = (
                market_state in MARKET_RISK_ORDER
                and MARKET_RISK_ORDER[market_state]
                >= MARKET_RISK_ORDER[rule.threshold]
            )
        elif isinstance(rule, IndustryStatusInvalidationRule):
            matched = (
                industry_state in INDUSTRY_RISK_ORDER
                and INDUSTRY_RISK_ORDER[industry_state]
                >= INDUSTRY_RISK_ORDER[rule.threshold]
            )
        else:  # pragma: no cover - the discriminated union is exhaustive
            matched = False
        if matched:
            return rule
    return None


def validate_transition(
    from_status: WatchlistStatus,
    to_status: WatchlistStatus,
    *,
    automatic: bool,
) -> None:
    if from_status == to_status:
        return
    if to_status == WatchlistStatus.ARCHIVED and not automatic:
        return
    if to_status not in _AUTOMATIC_TRANSITIONS[from_status]:
        raise ValueError(
            f"illegal_watchlist_transition:{from_status.value}->{to_status.value}"
        )


def determine_price_transition(value: PriceStateInput) -> PriceStateOutcome:
    current = value.current_status
    if current in {WatchlistStatus.INVALIDATED, WatchlistStatus.ARCHIVED}:
        return PriceStateOutcome(from_status=current, to_status=current)
    if value.monitoring_health != MonitoringHealth.HEALTHY or value.current_price is None:
        return PriceStateOutcome(
            from_status=current,
            to_status=current,
            rule_type=MonitoringRuleType.DATA_QUALITY_DEGRADED,
            reason_codes=(value.monitoring_health.value,),
            severity="ATTENTION",
        )

    price = value.current_price
    if value.invalidation_triggered:
        target = WatchlistStatus.INVALIDATED
        rule = MonitoringRuleType.PLAN_INVALIDATION_TRIGGERED
        reasons = ("PLAN_INVALIDATION_TRIGGERED",)
        severity = "CRITICAL"
    elif price <= value.hard_stop:
        target = WatchlistStatus.INVALIDATED
        rule = MonitoringRuleType.PRICE_BREAK_HARD_STOP
        reasons = ("PRICE_AT_OR_BELOW_HARD_STOP",)
        severity = "CRITICAL"
    elif value.entry_low <= price <= value.entry_high:
        target = WatchlistStatus.ENTRY_TRIGGERED
        rule = MonitoringRuleType.PRICE_ENTER_ENTRY_ZONE
        reasons = ("PRICE_IN_ENTRY_ZONE",)
        severity = "ATTENTION"
    elif price > value.entry_high:
        near_limit = value.entry_high * (
            1 + value.near_entry_distance_pct / 100
        )
        if price <= near_limit:
            target = WatchlistStatus.NEAR_ENTRY
            rule = MonitoringRuleType.PRICE_NEAR_ENTRY_ZONE
            reasons = ("PRICE_WITHIN_NEAR_ENTRY_DISTANCE",)
            severity = "ATTENTION"
        else:
            target = WatchlistStatus.WATCHING
            rule = None
            reasons = ("PRICE_ABOVE_NEAR_ENTRY_DISTANCE",)
            severity = "INFO"
    else:
        target = WatchlistStatus.WATCHING if value.reassessment else current
        rule = None
        reasons = ("BELOW_ENTRY_REQUIRES_REASSESSMENT",)
        severity = "ATTENTION"

    validate_transition(current, target, automatic=True)
    return PriceStateOutcome(
        from_status=current,
        to_status=target,
        rule_type=rule,
        reason_codes=reasons,
        severity=severity,
        trigger_reanalysis=(
            "BELOW_ENTRY_REQUIRES_REASSESSMENT" in reasons
            or (
                target != current
                and target
                in {
                    WatchlistStatus.NEAR_ENTRY,
                    WatchlistStatus.ENTRY_TRIGGERED,
                    WatchlistStatus.INVALIDATED,
                }
            )
        ),
    )


__all__ = [
    "INDUSTRY_RISK_ORDER",
    "MARKET_RISK_ORDER",
    "PriceStateInput",
    "determine_price_transition",
    "matching_invalidation_rule",
    "risk_increased",
    "validate_transition",
]
