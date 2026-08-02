from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PositionState(StrEnum):
    WATCH = "WATCH"
    READY = "READY"
    TRIAL = "TRIAL"
    CONFIRMED = "CONFIRMED"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    CLOSED = "CLOSED"


class PositionEvent(StrEnum):
    SETUP_READY = "SETUP_READY"
    SETUP_LOST = "SETUP_LOST"
    TRIAL_FILLED = "TRIAL_FILLED"
    ADD_CONFIRMED = "ADD_CONFIRMED"
    TARGET_REACHED = "TARGET_REACHED"
    REDUCTION_FILLED = "REDUCTION_FILLED"
    STOP_TRIGGERED = "STOP_TRIGGERED"
    INVALIDATED = "INVALIDATED"
    POSITION_CLOSED = "POSITION_CLOSED"


@dataclass(frozen=True)
class TransitionContext:
    trigger_confirmed: bool = False
    structure_intact: bool = True
    position_profitable: bool | None = None
    remaining_quantity: int = 0


@dataclass(frozen=True)
class StateTransition:
    previous: PositionState
    event: PositionEvent
    current: PositionState
    reason: str


class InvalidStateTransition(ValueError):
    pass


LEGACY_STATE_MAP = {
    "draft": PositionState.WATCH,
    "waiting_entry": PositionState.WATCH,
    "confirmed": PositionState.READY,
    "entry_triggered": PositionState.READY,
    "partially_executed": PositionState.TRIAL,
    "holding": PositionState.HOLD,
    "add_triggered": PositionState.CONFIRMED,
    "reduce_triggered": PositionState.REDUCE,
    "take_profit_triggered": PositionState.REDUCE,
    "stop_triggered": PositionState.EXIT,
    "invalidated": PositionState.EXIT,
    "closed": PositionState.CLOSED,
}


def canonical_position_state(legacy_status: str | None, *, quantity: int = 0) -> PositionState:
    state = LEGACY_STATE_MAP.get(legacy_status or "draft")
    if state is None:
        raise InvalidStateTransition(f"未知旧执行状态：{legacy_status}")
    if quantity > 0 and state in {PositionState.WATCH, PositionState.READY}:
        return PositionState.TRIAL
    if quantity == 0 and state is PositionState.HOLD:
        return PositionState.CLOSED
    return state


def audit_legacy_event_history(events: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    issues: list[str] = []
    previous_to: str | None = None
    for index, event in enumerate(events):
        from_status = event.get("from_status")
        to_status = event.get("to_status")
        try:
            canonical_position_state(str(to_status) if to_status is not None else None)
        except InvalidStateTransition as exc:
            issues.append(f"事件{index + 1}：{exc}")
        if previous_to is not None and from_status != previous_to:
            issues.append(
                f"事件{index + 1}：from_status={from_status} 与上一事件 "
                f"to_status={previous_to} 不一致"
            )
        previous_to = str(to_status) if to_status is not None else None
    return tuple(issues)


_TRANSITIONS = {
    (PositionState.WATCH, PositionEvent.SETUP_READY): PositionState.READY,
    (PositionState.READY, PositionEvent.SETUP_LOST): PositionState.WATCH,
    (PositionState.READY, PositionEvent.TRIAL_FILLED): PositionState.TRIAL,
    (PositionState.TRIAL, PositionEvent.ADD_CONFIRMED): PositionState.CONFIRMED,
    (PositionState.CONFIRMED, PositionEvent.ADD_CONFIRMED): PositionState.HOLD,
    (PositionState.HOLD, PositionEvent.ADD_CONFIRMED): PositionState.HOLD,
    (PositionState.TRIAL, PositionEvent.TARGET_REACHED): PositionState.REDUCE,
    (PositionState.CONFIRMED, PositionEvent.TARGET_REACHED): PositionState.REDUCE,
    (PositionState.HOLD, PositionEvent.TARGET_REACHED): PositionState.REDUCE,
    (PositionState.REDUCE, PositionEvent.REDUCTION_FILLED): PositionState.HOLD,
    (PositionState.REDUCE, PositionEvent.POSITION_CLOSED): PositionState.CLOSED,
    (PositionState.EXIT, PositionEvent.POSITION_CLOSED): PositionState.CLOSED,
}


def transition_position(
    state: PositionState,
    event: PositionEvent,
    context: TransitionContext | None = None,
) -> StateTransition:
    context = context or TransitionContext()
    if state is PositionState.CLOSED:
        raise InvalidStateTransition("已关闭计划不能继续迁移")
    if event in {PositionEvent.STOP_TRIGGERED, PositionEvent.INVALIDATED}:
        if state is PositionState.WATCH:
            raise InvalidStateTransition("观察状态没有持仓，不能触发持仓退出")
        return StateTransition(state, event, PositionState.EXIT, "硬止损或逻辑失效优先退出")
    target = _TRANSITIONS.get((state, event))
    if target is None:
        raise InvalidStateTransition(f"不允许从 {state.value} 通过 {event.value} 迁移")
    if event is PositionEvent.SETUP_READY and not context.trigger_confirmed:
        raise InvalidStateTransition("交易结构尚未确认，不能进入准备状态")
    if event is PositionEvent.TRIAL_FILLED and not context.trigger_confirmed:
        raise InvalidStateTransition("首次试仓必须有已确认触发证据")
    if event is PositionEvent.ADD_CONFIRMED:
        if not context.trigger_confirmed or not context.structure_intact:
            raise InvalidStateTransition("加仓必须同时满足触发确认和结构完整")
        if context.position_profitable is not True:
            raise InvalidStateTransition("禁止在持仓未盈利时增加风险")
    if event is PositionEvent.REDUCTION_FILLED:
        target = PositionState.HOLD if context.remaining_quantity > 0 else PositionState.CLOSED
    return StateTransition(state, event, target, f"{state.value} -> {target.value}")
