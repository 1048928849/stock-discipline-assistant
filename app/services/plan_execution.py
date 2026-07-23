from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import (
    PlanExecutionEvent,
    PlanExecutionFill,
    TradePlan,
    TradePlanCheck,
)
from app.schemas_workflow import PlanExecutionEvaluate, PlanExecutionFillCreate


EXECUTION_STATUSES = {
    "draft",
    "confirmed",
    "waiting_entry",
    "entry_triggered",
    "partially_executed",
    "holding",
    "add_triggered",
    "reduce_triggered",
    "stop_triggered",
    "take_profit_triggered",
    "invalidated",
    "closed",
}


def _plan(db: Session, plan_id: int) -> TradePlan:
    plan = db.get(TradePlan, plan_id)
    if plan is None:
        raise AppError(404, "TRADE_PLAN_NOT_FOUND", "交易计划不存在")
    return plan


def _event(
    db: Session,
    plan: TradePlan,
    event_type: str,
    to_status: str,
    *,
    details: dict,
    source: str = "manual",
    notes: str | None = None,
    event_time: datetime | None = None,
) -> None:
    if to_status not in EXECUTION_STATUSES:
        raise AppError(422, "INVALID_EXECUTION_STATUS", "计划执行状态不合法")
    previous = plan.execution_status or "draft"
    db.add(
        PlanExecutionEvent(
            trade_plan_id=plan.id,
            event_type=event_type,
            from_status=previous,
            to_status=to_status,
            event_time=event_time or datetime.now(),
            source=source,
            details=details,
            notes=notes,
        )
    )
    plan.execution_status = to_status


def initialize_plan_execution(db: Session, plan: TradePlan, position_mode: str) -> None:
    _event(
        db,
        plan,
        "plan_confirmed",
        "confirmed",
        details={"rule_status": plan.status, "position_mode": position_mode},
        source="system",
    )
    if position_mode == "持仓":
        target = "holding"
    elif plan.status == "READY" and plan.can_execute:
        target = "entry_triggered"
    else:
        target = "waiting_entry"
    _event(
        db,
        plan,
        "initial_condition_assessment",
        target,
        details={"rule_status": plan.status},
        source="system",
    )


def _calculate_summary(db: Session, plan: TradePlan, current_price: Decimal | None = None) -> dict:
    fills = db.scalars(
        select(PlanExecutionFill)
        .where(PlanExecutionFill.trade_plan_id == plan.id)
        .order_by(PlanExecutionFill.executed_at, PlanExecutionFill.id)
    ).all()
    quantity = 0
    average_cost = Decimal("0")
    realized = Decimal("0")
    violations: list[dict] = []
    entry_prices: list[Decimal] = []
    for fill in fills:
        if fill.side == "买入":
            if not plan.can_execute:
                violations.append(
                    {
                        "code": "entry_on_blocked_plan",
                        "message": "计划保存时不可执行，实际买入属于提前买入或绕过硬规则",
                        "fill_id": fill.id,
                    }
                )
            if fill.quantity % 100:
                violations.append(
                    {"code": "invalid_lot", "message": "买入数量不是A股100股整数倍", "fill_id": fill.id}
                )
            if fill.trigger_confirmed is False:
                violations.append(
                    {"code": "entry_without_trigger", "message": "未满足计划条件即买入", "fill_id": fill.id}
                )
            if not (plan.buy_zone_low <= fill.price <= plan.buy_zone_high):
                violations.append(
                    {
                        "code": "entry_outside_zone",
                        "message": "实际买入价格不在冻结的计划买入区",
                        "fill_id": fill.id,
                    }
                )
            if quantity > 0 and fill.price < average_cost:
                violations.append(
                    {
                        "code": "loss_averaging",
                        "message": "持仓价格低于原平均成本时继续买入，属于亏损补仓",
                        "fill_id": fill.id,
                    }
                )
            total_cost = average_cost * quantity + fill.price * fill.quantity + fill.fee
            quantity += fill.quantity
            average_cost = total_cost / quantity
            entry_prices.append(fill.price)
            if quantity > plan.planned_quantity:
                violations.append(
                    {"code": "over_position", "message": "实际持仓超过冻结计划数量", "fill_id": fill.id}
                )
        else:
            if fill.quantity > quantity:
                violations.append(
                    {"code": "sell_exceeds_position", "message": "卖出数量超过计划跟踪持仓", "fill_id": fill.id}
                )
            sold = min(fill.quantity, quantity)
            realized += (fill.price - average_cost) * sold - fill.fee
            quantity -= sold
            if quantity == 0:
                average_cost = Decimal("0")
    if current_price is not None and quantity > 0 and current_price <= plan.initial_stop:
        violations.append(
            {
                "code": "missed_stop",
                "message": "当前价格已触及硬止损但计划仍有持仓",
                "fill_id": None,
            }
        )
    target_entry = (plan.buy_zone_low + plan.buy_zone_high) / 2
    actual_entry = (
        sum(entry_prices, Decimal("0")) / len(entry_prices) if entry_prices else None
    )
    deviation_pct = (
        float((actual_entry / target_entry - 1) * 100)
        if actual_entry is not None and target_entry
        else None
    )
    checks = db.scalars(
        select(TradePlanCheck).where(TradePlanCheck.trade_plan_id == plan.id)
    ).all()
    required = (
        plan.buy_zone_low,
        plan.buy_zone_high,
        plan.initial_stop,
        plan.invalidation_condition,
        plan.add_condition,
        plan.reduce_condition,
        plan.exit_condition,
        plan.no_trade_condition,
        plan.planned_risk_amount,
    )
    completeness = round(sum(value not in (None, "") for value in required) / len(required) * 100, 2)
    unique_violations = {
        (item["code"], item.get("fill_id")): item for item in violations
    }
    compliance_denominator = max(1, len(checks) + len(fills))
    compliance = round(
        max(0, compliance_denominator - len(unique_violations)) / compliance_denominator * 100,
        2,
    )
    risk_amount = Decimal(plan.planned_risk_amount or 0)
    summary = {
        "execution_status": plan.execution_status,
        "net_quantity": quantity,
        "average_cost": round(float(average_cost), 4) if quantity else None,
        "realized_pnl": round(float(realized), 2),
        "realized_r": round(float(realized / risk_amount), 4) if risk_amount > 0 else None,
        "planned_quantity": plan.planned_quantity,
        "over_position": quantity > plan.planned_quantity,
        "entry_price_deviation_pct": round(deviation_pct, 4)
        if deviation_pct is not None
        else None,
        "violations": list(unique_violations.values()),
        "rule_execution_rate": compliance,
        "plan_completeness": completeness,
        "current_price": float(current_price) if current_price is not None else None,
        "updated_at": datetime.now().isoformat(),
    }
    plan.execution_summary = summary
    return summary


def add_manual_fill(
    db: Session, plan_id: int, payload: PlanExecutionFillCreate
) -> dict:
    plan = _plan(db, plan_id)
    if plan.execution_status in {"draft", None}:
        raise AppError(409, "PLAN_NOT_CONFIRMED", "计划尚未确认，不能录入执行成交")
    if payload.side == "买入" and payload.quantity % 100:
        raise AppError(422, "A_SHARE_LOT_REQUIRED", "A股买入数量必须是100股整数倍")
    fill = PlanExecutionFill(
        trade_plan_id=plan.id,
        **payload.model_dump(),
    )
    db.add(fill)
    db.flush()
    current = _calculate_summary(db, plan)
    net = current["net_quantity"]
    if payload.side == "买入":
        target = "holding" if net >= plan.planned_quantity else "partially_executed"
        event_type = "confirmation_add_fill" if payload.reason == "确认加仓" else "entry_fill"
    else:
        target = "closed" if net == 0 else "holding"
        event_type = {
            "硬止损": "stop_fill",
            "止盈": "take_profit_fill",
            "减仓": "reduce_fill",
            "逻辑退出": "invalidation_fill",
        }.get(payload.reason, "exit_fill")
    _event(
        db,
        plan,
        event_type,
        target,
        details={"fill_id": fill.id, "side": fill.side, "quantity": fill.quantity},
        event_time=fill.executed_at,
        notes=fill.notes,
    )
    _calculate_summary(db, plan)
    db.commit()
    return execution_detail(db, plan.id)


def evaluate_plan_execution(
    db: Session, plan_id: int, payload: PlanExecutionEvaluate
) -> dict:
    plan = _plan(db, plan_id)
    ordered = (
        ("invalidated", payload.invalidated, "invalidated"),
        ("stop_triggered", payload.stop_triggered, "stop_triggered"),
        ("take_profit_triggered", payload.take_profit_triggered, "take_profit_triggered"),
        ("reduce_triggered", payload.reduce_triggered, "reduce_triggered"),
        ("add_triggered", payload.add_triggered, "add_triggered"),
        ("entry_triggered", payload.entry_triggered, "entry_triggered"),
    )
    selected = next((item for item in ordered if item[1]), None)
    if selected:
        event_type, _, status = selected
        _event(
            db,
            plan,
            event_type,
            status,
            details={
                "current_price": float(payload.current_price)
                if payload.current_price is not None
                else None,
                "evidence": payload.evidence,
            },
            source="manual_check",
        )
    _calculate_summary(db, plan, payload.current_price)
    db.commit()
    return execution_detail(db, plan.id, current_price=payload.current_price)


def execution_detail(
    db: Session, plan_id: int, current_price: Decimal | None = None
) -> dict:
    plan = _plan(db, plan_id)
    if current_price is None and plan.execution_summary:
        stored_price = plan.execution_summary.get("current_price")
        current_price = Decimal(str(stored_price)) if stored_price is not None else None
    summary = _calculate_summary(db, plan, current_price)
    events = db.scalars(
        select(PlanExecutionEvent)
        .where(PlanExecutionEvent.trade_plan_id == plan.id)
        .order_by(PlanExecutionEvent.event_time, PlanExecutionEvent.id)
    ).all()
    fills = db.scalars(
        select(PlanExecutionFill)
        .where(PlanExecutionFill.trade_plan_id == plan.id)
        .order_by(PlanExecutionFill.executed_at, PlanExecutionFill.id)
    ).all()
    return {
        "plan_id": plan.id,
        "symbol": plan.symbol,
        "plan_version": plan.plan_version,
        "execution_status": plan.execution_status,
        "summary": summary,
        "fills": [
            {
                "id": item.id,
                "side": item.side,
                "quantity": item.quantity,
                "price": float(item.price),
                "fee": float(item.fee),
                "executed_at": item.executed_at.isoformat(),
                "reason": item.reason,
                "trigger_confirmed": item.trigger_confirmed,
                "is_test": item.is_test,
                "notes": item.notes,
            }
            for item in fills
        ],
        "events": [
            {
                "id": item.id,
                "event_type": item.event_type,
                "from_status": item.from_status,
                "to_status": item.to_status,
                "event_time": item.event_time.isoformat(),
                "source": item.source,
                "details": item.details,
                "notes": item.notes,
            }
            for item in events
        ],
        "disclaimer": "成交由用户手工录入；系统不连接券商、不自动下单。",
    }
