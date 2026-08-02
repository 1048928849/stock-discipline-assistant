from __future__ import annotations

import pytest

from app.domain.trade_plan import (
    InvalidStateTransition,
    PositionEvent,
    PositionState,
    TransitionContext,
    audit_legacy_event_history,
    canonical_position_state,
    transition_position,
)


def test_complete_entry_holding_and_reduction_path():
    ready = transition_position(
        PositionState.WATCH,
        PositionEvent.SETUP_READY,
        TransitionContext(trigger_confirmed=True),
    )
    trial = transition_position(
        ready.current,
        PositionEvent.TRIAL_FILLED,
        TransitionContext(trigger_confirmed=True),
    )
    confirmed = transition_position(
        trial.current,
        PositionEvent.ADD_CONFIRMED,
        TransitionContext(trigger_confirmed=True, position_profitable=True),
    )
    holding = transition_position(
        confirmed.current,
        PositionEvent.ADD_CONFIRMED,
        TransitionContext(trigger_confirmed=True, position_profitable=True),
    )
    reducing = transition_position(holding.current, PositionEvent.TARGET_REACHED)
    reduced = transition_position(
        reducing.current,
        PositionEvent.REDUCTION_FILLED,
        TransitionContext(remaining_quantity=100),
    )

    assert reduced.current is PositionState.HOLD


def test_reduction_fill_closes_when_no_position_remains():
    result = transition_position(
        PositionState.REDUCE,
        PositionEvent.REDUCTION_FILLED,
        TransitionContext(remaining_quantity=0),
    )

    assert result.current is PositionState.CLOSED


@pytest.mark.parametrize("state", [PositionState.READY, PositionState.TRIAL, PositionState.HOLD])
def test_stop_has_priority_over_normal_path(state):
    result = transition_position(state, PositionEvent.STOP_TRIGGERED)

    assert result.current is PositionState.EXIT


def test_loss_averaging_is_rejected():
    with pytest.raises(InvalidStateTransition, match="禁止在持仓未盈利时增加风险"):
        transition_position(
            PositionState.TRIAL,
            PositionEvent.ADD_CONFIRMED,
            TransitionContext(trigger_confirmed=True, position_profitable=False),
        )


def test_add_is_rejected_when_structure_is_broken():
    with pytest.raises(InvalidStateTransition, match="结构完整"):
        transition_position(
            PositionState.HOLD,
            PositionEvent.ADD_CONFIRMED,
            TransitionContext(
                trigger_confirmed=True,
                structure_intact=False,
                position_profitable=True,
            ),
        )


def test_closed_plan_cannot_reopen():
    with pytest.raises(InvalidStateTransition, match="已关闭计划"):
        transition_position(PositionState.CLOSED, PositionEvent.SETUP_READY)


@pytest.mark.parametrize(
    ("legacy", "quantity", "expected"),
    [
        ("waiting_entry", 0, PositionState.WATCH),
        ("entry_triggered", 0, PositionState.READY),
        ("partially_executed", 100, PositionState.TRIAL),
        ("holding", 100, PositionState.HOLD),
        ("reduce_triggered", 100, PositionState.REDUCE),
        ("stop_triggered", 100, PositionState.EXIT),
        ("closed", 0, PositionState.CLOSED),
    ],
)
def test_legacy_execution_status_mapping(legacy, quantity, expected):
    assert canonical_position_state(legacy, quantity=quantity) is expected


def test_legacy_event_history_detects_broken_chain():
    issues = audit_legacy_event_history(
        [
            {"from_status": "draft", "to_status": "confirmed"},
            {"from_status": "waiting_entry", "to_status": "holding"},
        ]
    )

    assert len(issues) == 1
    assert "与上一事件" in issues[0]


def test_legacy_event_history_accepts_continuous_chain():
    issues = audit_legacy_event_history(
        [
            {"from_status": "draft", "to_status": "confirmed"},
            {"from_status": "confirmed", "to_status": "entry_triggered"},
            {"from_status": "entry_triggered", "to_status": "holding"},
        ]
    )

    assert issues == ()
