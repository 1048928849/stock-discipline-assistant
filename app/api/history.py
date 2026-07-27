from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import AppError
from app.history.service import HistoricalDataBootstrapService, discovery_history_status
from app.models import HistoricalDataBootstrapItem, HistoricalDataBootstrapRun


router = APIRouter(prefix="/api/history", tags=["history-bootstrap"])


class HistoryBootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trade_date: date | None = None
    force_refresh: bool = False


def _run(db: Session, run_id: int) -> HistoricalDataBootstrapRun:
    row = db.get(HistoricalDataBootstrapRun, run_id)
    if row is None:
        raise AppError(404, "HISTORY_BOOTSTRAP_RUN_NOT_FOUND", "历史数据准备记录不存在")
    return row


@router.post("/bootstrap/discovery", status_code=201)
def bootstrap_discovery(payload: HistoryBootstrapRequest, db: Session = Depends(get_db)):
    return HistoricalDataBootstrapService(db).run(
        trade_date=payload.trade_date,
        force_refresh=payload.force_refresh,
    )


@router.get("/bootstrap/runs")
def list_bootstrap_runs(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(HistoricalDataBootstrapRun)
        .order_by(HistoricalDataBootstrapRun.created_at.desc())
        .limit(limit)
    ).all()


@router.get("/bootstrap/runs/{run_id}")
def get_bootstrap_run(run_id: int, db: Session = Depends(get_db)):
    return _run(db, run_id)


@router.get("/bootstrap/runs/{run_id}/items")
def list_bootstrap_items(run_id: int, db: Session = Depends(get_db)):
    _run(db, run_id)
    return db.scalars(
        select(HistoricalDataBootstrapItem)
        .where(HistoricalDataBootstrapItem.run_id == run_id)
        .order_by(
            HistoricalDataBootstrapItem.symbol,
            HistoricalDataBootstrapItem.capability,
        )
    ).all()


@router.get("/status/discovery")
def get_discovery_history_status(db: Session = Depends(get_db)):
    from app.composition.history import build_history_data_hub

    return discovery_history_status(db, router=build_history_data_hub(db))


__all__ = ["router"]
