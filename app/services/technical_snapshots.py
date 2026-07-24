from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Holding, MarketDailyBar, TechnicalSnapshot
from app.services.data_sources import UnifiedDataService
from app.services.technical import analyze_frame, compare_states


def load_qfq_frame(db: Session, symbol: str) -> pd.DataFrame:
    bars = db.scalars(
        select(MarketDailyBar)
        .where(
            MarketDailyBar.symbol == symbol,
            MarketDailyBar.source.in_(
                (
                    "akshare_qfq",
                    "akshare_eastmoney_qfq",
                    "akshare_tencent_qfq",
                    "akshare_sina_qfq",
                )
            ),
        )
        .order_by(MarketDailyBar.trade_date)
    ).all()
    if not bars:
        raise ValueError("没有前复权历史数据，请先执行 AKShare 行情同步")
    sources = {}
    for item in bars:
        sources.setdefault(item.source, []).append(item)
    selected_source, bars = max(
        sources.items(), key=lambda pair: max(item.fetched_at for item in pair[1])
    )
    frame = pd.DataFrame(
        [
            {
                "Date": item.trade_date,
                "Open": float(item.open),
                "High": float(item.high),
                "Low": float(item.low),
                "Close": float(item.close),
                "Volume": float(item.volume),
            }
            for item in bars
        ]
    )
    frame["Date"] = pd.to_datetime(frame["Date"])
    frame = frame.set_index("Date")
    frame.attrs["data_source"] = selected_source
    return frame


def snapshot_holding(db: Session, holding: Holding) -> TechnicalSnapshot:
    frame = load_qfq_frame(db, holding.symbol)
    analysis = analyze_frame(frame, holding.symbol, frame.attrs.get("data_source", "akshare_qfq"))
    trade_date = date.fromisoformat(analysis["trade_date"])
    previous = db.scalar(
        select(TechnicalSnapshot)
        .where(
            TechnicalSnapshot.holding_id == holding.id,
            TechnicalSnapshot.trade_date < trade_date,
        )
        .order_by(TechnicalSnapshot.trade_date.desc())
    )
    previous_state = {"signals": previous.signals} if previous else None
    changes = compare_states(previous_state, analysis)
    snapshot = db.scalar(
        select(TechnicalSnapshot).where(
            TechnicalSnapshot.holding_id == holding.id,
            TechnicalSnapshot.trade_date == trade_date,
        )
    )
    values = {
        "symbol": holding.symbol,
        "data_source": analysis["data_source"],
        "indicators": analysis["indicators"],
        "signals": analysis["signals"],
        "support_levels": analysis["support_levels"],
        "resistance_levels": analysis["resistance_levels"],
        "conflicts": analysis["conflicts"],
        "conclusion": analysis["conclusion"],
        "risk_notice": analysis["risk_notice"],
        "changes": changes,
    }
    if snapshot:
        for key, value in values.items():
            setattr(snapshot, key, value)
    else:
        snapshot = TechnicalSnapshot(
            holding_id=holding.id,
            trade_date=trade_date,
            **values,
        )
        db.add(snapshot)
    return snapshot


def refresh_qfq_history(db: Session, symbol: str, days: int = 550) -> int:
    bars = UnifiedDataService(db).get_history(
        symbol, date.today() - timedelta(days=days), date.today()
    ).require_value()
    source = bars[0].source if bars else "akshare_qfq"
    existing = {
        item.trade_date: item
        for item in db.scalars(
            select(MarketDailyBar).where(
                MarketDailyBar.symbol == symbol,
                MarketDailyBar.source == source,
            )
        ).all()
    }
    for bar in bars:
        stored = existing.get(bar.trade_date)
        if stored:
            stored.open = bar.open
            stored.high = bar.high
            stored.low = bar.low
            stored.close = bar.close
            stored.volume = bar.volume
            stored.fetched_at = bar.fetched_at
        else:
            db.add(MarketDailyBar(**bar.__dict__))
    return len(bars)


def snapshot_all_holdings(db: Session, refresh_market: bool = False) -> dict:
    created, errors = 0, []
    holdings = db.scalars(select(Holding).order_by(Holding.id)).all()
    refresh_errors: dict[str, str] = {}
    if refresh_market:
        for symbol in sorted({item.symbol for item in holdings}):
            try:
                refresh_qfq_history(db, symbol)
                db.flush()
            except Exception as exc:
                refresh_errors[symbol] = str(exc)[:300]
    for holding in holdings:
        if holding.symbol in refresh_errors:
            errors.append(
                {
                    "holding_id": holding.id,
                    "symbol": holding.symbol,
                    "error": f"行情刷新失败，未生成可能过期的快照：{refresh_errors[holding.symbol]}",
                }
            )
            continue
        try:
            snapshot = snapshot_holding(db, holding)
            if refresh_market:
                holding.current_price = Decimal(str(snapshot.indicators["close"]))
                holding.price_source = "akshare_qfq_close"
                holding.price_updated_at = datetime.now()
            db.flush()
            created += 1
        except Exception as exc:
            errors.append(
                {"holding_id": holding.id, "symbol": holding.symbol, "error": str(exc)[:300]}
            )
    db.commit()
    return {"processed": created, "refreshed": refresh_market, "errors": errors}
