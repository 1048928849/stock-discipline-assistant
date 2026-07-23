from datetime import date

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import AppError
from app.models import BacktestRun, MarketQuote, TechnicalSnapshot
from app.services.technical import analyze_frame, backtest_signals
from app.services.technical_snapshots import load_qfq_frame, snapshot_all_holdings


router = APIRouter(prefix="/api/v1/technical", tags=["技术研究"])


@router.get("/{symbol}")
def technical_analysis(symbol: str = Path(pattern=r"^\d{6}$"), db: Session = Depends(get_db)):
    try:
        frame = load_qfq_frame(db, symbol)
        quote = db.scalar(select(MarketQuote).where(MarketQuote.symbol == symbol))
        result = analyze_frame(
            frame,
            symbol,
            frame.attrs.get("data_source", "akshare_qfq"),
            float(quote.price) if quote else None,
        )
        result["current_price_source"] = quote.source if quote else result["data_source"]
        result["current_price_updated_at"] = (
            quote.fetched_at.isoformat() if quote else result["analysis_date"]
        )
        return result
    except ValueError as exc:
        raise AppError(422, "TECHNICAL_DATA_REQUIRED", str(exc)) from exc


@router.post("/{symbol}/backtest")
def technical_backtest(
    symbol: str = Path(pattern=r"^\d{6}$"),
    holding_days: int = Query(default=10, ge=1, le=60),
    fees: float = Query(default=0.0003, ge=0, le=0.02),
    slippage: float = Query(default=0.001, ge=0, le=0.05),
    db: Session = Depends(get_db),
):
    try:
        frame = load_qfq_frame(db, symbol)
        metrics = backtest_signals(frame, holding_days, fees, slippage)
    except (ValueError, ImportError) as exc:
        raise AppError(422, "TECHNICAL_BACKTEST_UNAVAILABLE", str(exc)) from exc
    run = BacktestRun(
        symbol=symbol,
        strategy="technical_signals",
        date_from=date.fromisoformat(metrics["data_start"]),
        date_to=date.fromisoformat(metrics["data_end"]),
        parameters={"holding_days": holding_days, "fees": fees, "slippage": slippage},
        metrics=metrics,
        status="success",
        warning=metrics["warning"],
    )
    db.add(run)
    db.commit()
    return metrics


@router.post("/holdings/snapshots")
def create_holding_snapshots(
    refresh_market: bool = Query(default=False), db: Session = Depends(get_db)
):
    return snapshot_all_holdings(db, refresh_market=refresh_market)


@router.get("/holdings/changes")
def holding_technical_changes(
    holding_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = select(TechnicalSnapshot).order_by(TechnicalSnapshot.trade_date.desc()).limit(limit)
    if holding_id is not None:
        query = query.where(TechnicalSnapshot.holding_id == holding_id)
    return db.scalars(query).all()
