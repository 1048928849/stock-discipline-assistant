from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import AppError
from app.selected_stock.contracts import (
    SelectedStockAnalysisRequest,
    SelectedStockAnalysisResult,
)
from app.selected_stock.service import (
    SelectedStockAnalysisError,
    SelectedStockAnalysisService,
)
from app.selected_stock.history import (
    SelectedStockHistoryService,
    SelectedStockRunComparison,
    SelectedStockRunSummary,
)
from app.selected_stock.replay import (
    BatchReplayRequest,
    BatchReplayResult,
    SelectedStockReplayService,
)


router = APIRouter(prefix="/api/v1", tags=["selected-stock-analysis"])


@router.post(
    "/selected-stock-analysis",
    response_model=SelectedStockAnalysisResult,
)
def selected_stock_analysis(
    payload: SelectedStockAnalysisRequest,
    db: Session = Depends(get_db),
) -> SelectedStockAnalysisResult:
    try:
        return SelectedStockAnalysisService(db).analyze(payload)
    except SelectedStockAnalysisError as exc:
        raise AppError(422, exc.code, str(exc)) from exc


@router.get(
    "/selected-stock-analysis/runs",
    response_model=list[SelectedStockRunSummary],
)
def selected_stock_runs(
    symbol: str | None = None,
    analysis_date: date | None = None,
    cycle_state: str | None = None,
    stock_role: str | None = None,
    plan_status: str | None = None,
    strategy_version: str | None = None,
    data_status: str | None = None,
    industry_status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[SelectedStockRunSummary]:
    return SelectedStockHistoryService(db).list_runs(
        symbol=symbol,
        analysis_date=analysis_date,
        cycle_state=cycle_state,
        stock_role=stock_role,
        plan_status=plan_status,
        strategy_version=strategy_version,
        data_status=data_status,
        industry_status=industry_status,
        limit=limit,
    )


@router.get(
    "/selected-stock-analysis/runs/{run_id}",
    response_model=SelectedStockAnalysisResult,
)
def selected_stock_run(
    run_id: int,
    db: Session = Depends(get_db),
) -> SelectedStockAnalysisResult:
    try:
        return SelectedStockHistoryService(db).get_run(run_id)
    except LookupError as exc:
        raise AppError(404, "SELECTED_STOCK_RUN_NOT_FOUND", str(exc)) from exc


@router.get(
    "/selected-stock-analysis/runs/{run_id}/compare/{other_run_id}",
    response_model=SelectedStockRunComparison,
)
def compare_selected_stock_runs(
    run_id: int,
    other_run_id: int,
    db: Session = Depends(get_db),
) -> SelectedStockRunComparison:
    try:
        return SelectedStockHistoryService(db).compare(run_id, other_run_id)
    except LookupError as exc:
        raise AppError(404, "SELECTED_STOCK_RUN_NOT_FOUND", str(exc)) from exc
    except ValueError as exc:
        raise AppError(422, "SELECTED_STOCK_RUN_SCOPE_MISMATCH", str(exc)) from exc


@router.post(
    "/selected-stock-analysis/replay",
    response_model=BatchReplayResult,
)
def replay_selected_stock_runs(
    payload: BatchReplayRequest,
    db: Session = Depends(get_db),
) -> BatchReplayResult:
    try:
        return SelectedStockReplayService(db).run(payload)
    except ValueError as exc:
        raise AppError(422, "SELECTED_STOCK_REPLAY_UNAVAILABLE", str(exc)) from exc


__all__ = ["router"]
