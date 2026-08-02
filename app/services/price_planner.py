from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from datetime import date
from typing import Any

import pandas as pd

from app.domain.event_anchor import InstitutionalEvidence
from app.domain.features import FeatureSnapshot
from app.domain.price_plan import PricePlan, PricePlanStatus, PriceZone
from app.domain.strategy import StrategyEvaluationResult
from app.services.capital_anchor_engine import calculate_market_structure


def plan_prices(
    *,
    strategy_result: StrategyEvaluationResult,
    feature_snapshot: FeatureSnapshot,
    market_data: pd.DataFrame | None,
    parameters: Mapping[str, Any],
    minute_sessions: Mapping[date, pd.DataFrame] | None = None,
    institutional_evidence: Sequence[InstitutionalEvidence] = (),
) -> PricePlan:
    _ = strategy_result
    pattern = feature_snapshot.value("platform_structure")
    empty = PricePlan(
        status=PricePlanStatus.INSUFFICIENT_DATA,
        entry_zone=PriceZone(None, None),
        entry_reference=None,
        stop_price=None,
        target_price=None,
        first_target=None,
        second_target=None,
        reward_risk=None,
        invalidation_price=pattern.get("platform_lower") if pattern else None,
        structure_invalidation="数据不足，无法判断",
    )
    if market_data is None or not pattern or not pattern["valid_platform"]:
        return empty

    market_structure = calculate_market_structure(
        market_data,
        atr=pattern["atr14"],
        platform_lower=pattern["platform_lower"],
        platform_upper=pattern["platform_upper"],
        extreme_stop_atr_multiple=float(parameters.get("extreme_stop_atr_multiple", 1.0)),
        minute_sessions=minute_sessions,
        institutional_evidence=institutional_evidence,
    )

    atr_buffer = pattern["atr14"] * float(parameters["atr_buffer_multiple"])
    recent_low = float(market_data["Low"].tail(10).min())
    stop = round(max(pattern["platform_lower"], recent_low) - atr_buffer, 4)
    entry = round(pattern["turn_trigger_price"], 4)
    buy_low = round(entry - pattern["atr14"] * 0.2, 4)
    buy_high = round(entry + pattern["atr14"] * 0.2, 4)
    first_target = second_target = reward_risk = None
    if stop < entry:
        platform_target = pattern["platform_upper"] + (
            pattern["platform_upper"] - pattern["platform_lower"]
        )
        minimum_r_target = entry + float(parameters["minimum_reward_risk"]) * (entry - stop)
        raw_first_target = max(platform_target, minimum_r_target)
        first_target = round(raw_first_target, 4)
        second_target = round(entry + 3 * (entry - stop), 4)
        reward_risk = (raw_first_target - entry) / (entry - stop)
    return PricePlan(
        status=PricePlanStatus.AVAILABLE,
        entry_zone=PriceZone(buy_low, buy_high),
        entry_reference=entry,
        stop_price=stop,
        target_price=first_target,
        first_target=first_target,
        second_target=second_target,
        reward_risk=reward_risk,
        invalidation_price=pattern["platform_lower"],
        structure_invalidation=(f"收盘跌破平台下沿 {pattern['platform_lower']} 或放量跌回平台。"),
        market_structure=market_structure,
    )
