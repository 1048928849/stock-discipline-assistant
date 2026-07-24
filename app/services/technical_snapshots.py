from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_hub.market_subjects import stock_daily_subject
from app.models import Holding, TechnicalSnapshot
from app.services.data_sources import build_data_hub
from app.services.market_cache import replace_market_series, resolve_cached_series
from app.services.technical import analyze_frame, compare_states


def load_qfq_frame(db: Session, symbol: str) -> pd.DataFrame:
    subject = stock_daily_subject(symbol, "qfq", "CNY", "share")
    selection = resolve_cached_series(
        db,
        cache_symbol=subject.subject_id,
        capability="market.daily.qfq",
        subject=subject,
        adjustment="qfq",
        price_unit="CNY",
        volume_unit="share",
        min_rows=250,
    )
    if not selection.executable:
        reason = selection.blocking_reason or selection.structure_reason or "missing"
        quality = selection.effective_quality.effective_quality.value
        raise ValueError(f"qfq market data is not executable: {quality} ({reason})")
    bars = selection.bars
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
    frame.attrs["data_source"] = selection.source
    frame.attrs["quality_record_id"] = selection.quality_record_id
    frame.attrs["effective_quality"] = (
        selection.effective_quality.effective_quality.value
    )
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
    provider = build_data_hub(db)
    result = provider.get_history(
        symbol, date.today() - timedelta(days=days), date.today()
    )
    if result.quality_status.blocks_execution:
        db.commit()
    bars = result.require_trusted_value()
    subject = stock_daily_subject(symbol, "qfq", "CNY", "share")
    stored = replace_market_series(
        db,
        provider,
        result,
        bars,
        subject=subject,
        min_rows=250,
    )
    return len(stored)


def snapshot_all_holdings(db: Session, refresh_market: bool = False) -> dict:
    created, errors = 0, []
    holdings = db.scalars(select(Holding).order_by(Holding.id)).all()
    refresh_errors: dict[str, str] = {}
    if refresh_market:
        for symbol in sorted({item.symbol for item in holdings}):
            try:
                refresh_qfq_history(db, symbol)
                db.commit()
            except Exception as exc:
                db.rollback()
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
            with db.begin_nested():
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
