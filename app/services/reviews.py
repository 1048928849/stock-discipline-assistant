from datetime import date, timedelta
from collections import Counter
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, DisciplineEvent, Holding, PositionSnapshot, Trade, TradePlan


def review_metrics(db: Session, account_id: int, start: date, end: date) -> dict:
    trades = db.scalars(
        select(Trade).where(
            Trade.account_id == account_id,
            Trade.traded_at >= start,
            Trade.traded_at < end + timedelta(days=1),
        )
    ).all()
    events = db.scalars(
        select(DisciplineEvent).where(
            DisciplineEvent.account_id == account_id,
            DisciplineEvent.occurred_at >= start,
            DisciplineEvent.occurred_at < end + timedelta(days=1),
        )
    ).all()
    planned = sum(1 for t in trades if t.is_planned)
    rules = Counter((event.trigger_data or {}).get("rule", "unknown") for event in events)
    plans = db.scalars(
        select(TradePlan).where(
            TradePlan.account_id == account_id,
            TradePlan.created_at >= start,
            TradePlan.created_at < end + timedelta(days=1),
        )
    ).all()
    snapshots = db.scalars(
        select(PositionSnapshot)
        .join(Holding, PositionSnapshot.holding_id == Holding.id)
        .where(
            Holding.account_id == account_id,
            PositionSnapshot.snapshot_date >= start,
            PositionSnapshot.snapshot_date <= end,
        )
    ).all()
    account = db.get(Account, account_id)
    holdings = db.scalars(select(Holding).where(Holding.account_id == account_id)).all()
    concentrations = (
        [
            Decimal(item.quantity) * item.current_price / account.total_assets * 100
            for item in holdings
        ]
        if account
        else []
    )
    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "trade_count": len(trades),
        "planned_ratio": round(planned / len(trades) * 100, 2) if trades else 0,
        "trade_plan_count": len(plans),
        "ready_plan_count": sum(item.status == "READY" for item in plans),
        "plan_complete_ratio": round(
            sum(item.status == "READY" for item in plans) / len(plans) * 100, 2
        )
        if plans
        else 0,
        "position_snapshot_count": len(snapshots),
        "hard_stop_trigger_count": sum(item.hard_stop_triggered for item in snapshots),
        "violation_count": len(events),
        "buy_count": sum(1 for t in trades if t.side == "BUY"),
        "sell_count": sum(1 for t in trades if t.side == "SELL"),
        "average_holding_concentration_pct": round(
            float(sum(concentrations) / len(concentrations)), 2
        )
        if concentrations
        else 0,
        "most_common_violation": rules.most_common(1)[0][0] if rules else None,
    }
