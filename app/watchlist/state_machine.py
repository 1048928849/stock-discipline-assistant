from __future__ import annotations

from app.watchlist.contracts import (
    MonitoringHealth,
    MonitoringRuleType,
    PriceStateInput,
    PriceStateOutcome,
    WatchlistStatus,
)


_AUTOMATIC_TRANSITIONS = {
    WatchlistStatus.DISCOVERED: {WatchlistStatus.RESEARCHING},
    WatchlistStatus.RESEARCHING: {
        WatchlistStatus.WATCHING,
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
        target = current
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
            target != current
            and target
            in {
                WatchlistStatus.NEAR_ENTRY,
                WatchlistStatus.ENTRY_TRIGGERED,
                WatchlistStatus.INVALIDATED,
            }
        ),
    )


__all__ = ["PriceStateInput", "determine_price_transition", "validate_transition"]
