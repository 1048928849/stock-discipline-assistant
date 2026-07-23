from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, DisciplineEvent, DisciplineRule, Holding, Trade


DEFAULTS = {
    "single_position_pct": Decimal("20"),
    "sector_pct": Decimal("35"),
    "total_pct": Decimal("80"),
    "daily_trades": 5,
}


def check_discipline(db: Session, account_id: int, persist: bool = True) -> list[dict]:
    account = db.get(Account, account_id)
    if not account:
        return []
    holdings = db.scalars(select(Holding).where(Holding.account_id == account_id)).all()
    alerts: list[dict] = []
    sector_values: dict[str, Decimal] = defaultdict(Decimal)
    total_value = Decimal(0)
    limits = DEFAULTS.copy()
    enabled = set(DEFAULTS)
    for configured in db.scalars(select(DisciplineRule)).all():
        if configured.code in limits:
            if configured.enabled:
                limits[configured.code] = Decimal(
                    str(configured.config.get("value", limits[configured.code]))
                )
            else:
                enabled.discard(configured.code)

    def add(rule: str, severity: str, message: str, data: dict):
        alert = {"rule": rule, "severity": severity, "message": message, "trigger_data": data}
        alerts.append(alert)
        if persist:
            db.add(
                DisciplineEvent(
                    account_id=account_id,
                    severity=severity,
                    message=message,
                    trigger_data={"rule": rule, **data},
                )
            )

    for holding in holdings:
        value = Decimal(holding.quantity) * holding.current_price
        total_value += value
        sector_values[holding.sector or "未分类"] += value
        pct = value / account.total_assets * 100
        limit = holding.max_position_pct or limits["single_position_pct"]
        if "single_position_pct" in enabled and pct > limit:
            add(
                "single_position",
                "CRITICAL",
                f"{holding.symbol} 单股仓位 {pct:.2f}% 超过 {limit}%",
                {"symbol": holding.symbol, "position_pct": float(pct), "limit_pct": float(limit)},
            )
        if not holding.buy_reason:
            add(
                "missing_reason",
                "WARNING",
                f"{holding.symbol} 未填写买入理由",
                {"symbol": holding.symbol},
            )
        if not holding.invalidation_condition or holding.stop_loss_price is None:
            add(
                "missing_exit",
                "WARNING",
                f"{holding.symbol} 未完整设置失效条件或止损价",
                {"symbol": holding.symbol},
            )
        if holding.stop_loss_price is not None and holding.current_price <= holding.stop_loss_price:
            add(
                "stop_loss",
                "CRITICAL",
                f"{holding.symbol} 当前价已触及止损价",
                {
                    "symbol": holding.symbol,
                    "current_price": str(holding.current_price),
                    "stop_loss_price": str(holding.stop_loss_price),
                },
            )
    for sector, value in sector_values.items():
        pct = value / account.total_assets * 100
        if "sector_pct" in enabled and pct > limits["sector_pct"]:
            add(
                "sector_concentration",
                "WARNING",
                f"{sector} 板块仓位 {pct:.2f}% 过高",
                {
                    "sector": sector,
                    "position_pct": float(pct),
                    "limit_pct": float(limits["sector_pct"]),
                },
            )
    total_pct = total_value / account.total_assets * 100
    if "total_pct" in enabled and total_pct > limits["total_pct"]:
        add(
            "total_position",
            "CRITICAL",
            f"总仓位 {total_pct:.2f}% 超过上限",
            {"position_pct": float(total_pct), "limit_pct": float(limits["total_pct"])},
        )

    today_trades = db.scalars(
        select(Trade).where(Trade.account_id == account_id, Trade.traded_at >= date.today())
    ).all()
    if "daily_trades" in enabled and len(today_trades) > limits["daily_trades"]:
        add(
            "overtrading",
            "WARNING",
            f"今日交易 {len(today_trades)} 次，存在过度交易风险",
            {"count": len(today_trades), "limit": int(limits["daily_trades"])},
        )
    for trade in today_trades:
        if trade.is_planned and not trade.reason:
            add(
                "planned_without_reason",
                "WARNING",
                f"{trade.symbol} 缺少交易理由，不能视为完整计划内交易",
                {"trade_id": trade.id, "symbol": trade.symbol},
            )
        if not trade.is_planned:
            add(
                "unplanned_trade",
                "WARNING",
                f"{trade.symbol} 为计划外交易",
                {"trade_id": trade.id, "symbol": trade.symbol},
            )
        if trade.emotion and trade.emotion in {"冲动", "焦虑", "报复", "恐慌"}:
            add(
                "emotional_trade",
                "WARNING",
                f"{trade.symbol} 记录了情绪化交易：{trade.emotion}",
                {"trade_id": trade.id, "emotion": trade.emotion},
            )
        if trade.side == "BUY":
            earlier_buys = [
                item
                for item in today_trades
                if item.symbol == trade.symbol
                and item.side == "BUY"
                and item.traded_at < trade.traded_at
            ]
            if earlier_buys and trade.price < earlier_buys[-1].price:
                add(
                    "revenge_averaging",
                    "WARNING",
                    f"{trade.symbol} 下跌后短时间再次买入，存在报复性/摊薄式交易风险",
                    {
                        "trade_id": trade.id,
                        "previous_price": str(earlier_buys[-1].price),
                        "current_price": str(trade.price),
                    },
                )
    if persist:
        db.commit()
    return alerts
