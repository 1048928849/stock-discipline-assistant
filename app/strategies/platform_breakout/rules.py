from __future__ import annotations

from typing import Any

from app.domain.features import FeatureQuality
from app.domain.strategy import (
    StrategyContext,
    StrategyRuleCategory,
    StrategyRuleEvidence,
    StrategyRuleResult,
    StrategyRuleStatus,
)


def _features_missing(context: StrategyContext) -> bool:
    return any(
        context.feature_snapshot.get(feature_id).quality is FeatureQuality.MISSING
        for feature_id in ("platform_structure", "multi_timeframe")
    )


def _result(
    context: StrategyContext,
    rule_id: str,
    name: str,
    category: StrategyRuleCategory,
    status: StrategyRuleStatus,
    reason: str,
    value: Any = None,
    missing_data: tuple[str, ...] = (),
) -> StrategyRuleResult:
    market_feature = context.feature_snapshot.get("platform_structure")
    source_id = market_feature.result.source_ids[0] if market_feature.result.source_ids else "数据不足"
    return StrategyRuleResult(
        rule_id=rule_id,
        name=name,
        category=category,
        status=status,
        reason=reason,
        evidence=(
            StrategyRuleEvidence(
                source_id=source_id,
                description=reason,
                value=value,
                data_time=market_feature.result.data_time,
            ),
        ),
        missing_data=missing_data,
    )


def platform_data_sufficiency(context: StrategyContext) -> StrategyRuleResult:
    platform = context.feature_snapshot.get("platform_structure")
    timeframes = context.feature_snapshot.get("multi_timeframe")
    missing = tuple(
        str(item)
        for item in context.market_context.get("missing_data", ())
    )
    if _features_missing(context) or platform.value is None or timeframes.value is None:
        return _result(
            context,
            "platform_data_sufficiency",
            "平台策略数据完整性",
            StrategyRuleCategory.REQUIRED,
            StrategyRuleStatus.UNKNOWN,
            "历史行情数据不足。",
            missing_data=missing,
        )
    return _result(
        context,
        "platform_data_sufficiency",
        "平台策略数据完整性",
        StrategyRuleCategory.REQUIRED,
        StrategyRuleStatus.PASS,
        "平台结构与多周期事实可用。",
    )


def large_cycle_direction(context: StrategyContext) -> StrategyRuleResult:
    facts = context.feature_snapshot.value("multi_timeframe")
    if _features_missing(context) or facts is None:
        return _result(
            context,
            "large_cycle_direction",
            "月线、周线大周期方向",
            StrategyRuleCategory.CONTEXT,
            StrategyRuleStatus.UNKNOWN,
            "历史行情数据不足。",
            missing_data=tuple(context.market_context.get("missing_data", ())),
        )
    weekly = facts["weekly_state"]
    monthly = facts["monthly_state"]
    large_state = facts["large_state"]
    status = (
        StrategyRuleStatus.FAIL
        if weekly["state"] == "向下"
        else StrategyRuleStatus.PASS
        if large_state == "向上"
        else StrategyRuleStatus.WAIT
    )
    reason = (
        f"月线 {monthly['state']}（{monthly['evidence']}）；"
        f"周线 {weekly['state']}（{weekly['evidence']}）。"
    )
    return _result(
        context,
        "large_cycle_direction",
        "月线、周线大周期方向",
        StrategyRuleCategory.CONTEXT,
        status,
        reason,
        value=facts,
    )


def platform_structure(context: StrategyContext) -> StrategyRuleResult:
    pattern = context.feature_snapshot.value("platform_structure")
    if _features_missing(context) or pattern is None:
        return _result(
            context,
            "platform_structure",
            "日线平台和趋势结构",
            StrategyRuleCategory.REQUIRED,
            StrategyRuleStatus.UNKNOWN,
            "历史行情数据不足。",
            missing_data=tuple(context.market_context.get("missing_data", ())),
        )
    status = StrategyRuleStatus.PASS if pattern["valid_platform"] else StrategyRuleStatus.FAIL
    reason = (
        f"观察 {pattern['platform_days']} 日，上沿 {pattern['platform_upper']}，"
        f"下沿 {pattern['platform_lower']}，区间振幅 {pattern['platform_range_pct']}%。"
    )
    return _result(
        context,
        "platform_structure",
        "日线平台和趋势结构",
        StrategyRuleCategory.REQUIRED,
        status,
        reason,
        value=pattern,
    )


def breakout_volume_confirmation(context: StrategyContext) -> StrategyRuleResult:
    pattern = context.feature_snapshot.value("platform_structure")
    if _features_missing(context) or pattern is None:
        return _result(
            context,
            "breakout_volume_confirmation",
            "突破成交量",
            StrategyRuleCategory.TRIGGER,
            StrategyRuleStatus.UNKNOWN,
            "历史行情数据不足。",
            missing_data=tuple(context.market_context.get("missing_data", ())),
        )
    breakout = pattern["breakout"]
    if breakout and breakout["volume_confirmed"]:
        status = StrategyRuleStatus.PASS
    elif breakout:
        status = StrategyRuleStatus.WAIT
    else:
        status = StrategyRuleStatus.UNKNOWN
    reason = (
        f"{breakout['date']} 收盘 {breakout['price']} 突破，成交量为平台均量 "
        f"{breakout['volume_ratio']:.2f} 倍。"
        if breakout
        else f"尚未收盘突破平台上沿 {pattern['platform_upper']}。"
    )
    return _result(
        context,
        "breakout_volume_confirmation",
        "突破成交量",
        StrategyRuleCategory.TRIGGER,
        status,
        reason,
        value=breakout,
        missing_data=() if breakout else ("有效突破",),
    )


def pullback_structure(context: StrategyContext) -> StrategyRuleResult:
    pattern = context.feature_snapshot.value("platform_structure")
    if _features_missing(context) or pattern is None:
        return _result(
            context,
            "pullback_structure",
            "回踩缩量及结构",
            StrategyRuleCategory.HARD_REJECT,
            StrategyRuleStatus.UNKNOWN,
            "历史行情数据不足。",
            missing_data=tuple(context.market_context.get("missing_data", ())),
        )
    status = (
        StrategyRuleStatus.FAIL
        if pattern["platform_broken"]
        else StrategyRuleStatus.PASS
        if pattern["pullback_seen"] and pattern["pullback_shrinking"]
        else StrategyRuleStatus.WAIT
        if pattern["pullback_seen"]
        else StrategyRuleStatus.UNKNOWN
    )
    reason = (
        f"回踩区 {pattern['pullback_range'] or '尚未出现'}；"
        f"量能比 {pattern['pullback_volume_ratio']}; "
        f"是否破位：{'是' if pattern['platform_broken'] else '否'}。"
    )
    return _result(
        context,
        "pullback_structure",
        "回踩缩量及结构",
        StrategyRuleCategory.HARD_REJECT,
        status,
        reason,
        value=pattern,
        missing_data=() if pattern["pullback_seen"] else ("突破后的回踩样本",),
    )


def turn_stronger_confirmation(context: StrategyContext) -> StrategyRuleResult:
    pattern = context.feature_snapshot.value("platform_structure")
    if _features_missing(context) or pattern is None:
        return _result(
            context,
            "turn_stronger_confirmation",
            "再次转强条件",
            StrategyRuleCategory.TRIGGER,
            StrategyRuleStatus.UNKNOWN,
            "历史行情数据不足。",
            missing_data=tuple(context.market_context.get("missing_data", ())),
        )
    status = StrategyRuleStatus.PASS if pattern["turned_stronger"] else StrategyRuleStatus.UNKNOWN
    reason = (
        f"触发价 {pattern['turn_trigger_price']}；要求收盘越过触发价、"
        "超过前一日高点且成交量不低于20日均量。"
    )
    return _result(
        context,
        "turn_stronger_confirmation",
        "再次转强条件",
        StrategyRuleCategory.TRIGGER,
        status,
        reason,
        value=pattern["turned_stronger"],
        missing_data=() if pattern["turned_stronger"] else ("再次放量转强",),
    )
