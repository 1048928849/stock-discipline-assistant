from __future__ import annotations

from fastapi import APIRouter, Depends
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


__all__ = ["router"]
