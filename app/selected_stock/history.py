from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SelectedStockAnalysisRun
from app.selected_stock.contracts import SelectedStockAnalysisResult


class SelectedStockRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: int
    symbol: str
    analysis_date: date
    cycle_state: str
    stock_role: str
    plan_status: str
    strategy_version: str
    data_status: str
    industry_status: str
    created_at: datetime


class SelectedStockRunComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: int
    other_run_id: int
    symbol: str
    before: dict[str, Any]
    after: dict[str, Any]
    changes: dict[str, dict[str, Any]]


def _comparison_snapshot(result: SelectedStockAnalysisResult) -> dict[str, Any]:
    return {
        "market_context": result.market_context_status.value,
        "industry": result.industry_context.model_dump(mode="json"),
        "cycle": result.cycle_state.value,
        "stock_role": result.stock_role.value,
        "score": result.scores.total,
        "gates": {
            item.code: {
                "status": item.status.value,
                "reason_code": item.reason_code,
            }
            for item in result.hard_gates
        },
        "buy_zone": {
            "low": result.price_plan.entry_zone_low,
            "high": result.price_plan.entry_zone_high,
        },
        "hard_stop": result.price_plan.stop_loss,
        "position": result.position_plan.model_dump(mode="json"),
        "survival_rules": {
            item.rule_code: {
                "status": item.status.value,
                "reason_code": item.reason_code,
            }
            for item in result.survival_discipline
        },
        "sources": [
            {
                "capability": item.capability,
                "provider_id": item.provider_id,
                "response_digest": item.response_digest,
            }
            for item in result.source_lineage
        ],
    }


class SelectedStockHistoryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_runs(
        self,
        *,
        symbol: str | None = None,
        analysis_date: date | None = None,
        cycle_state: str | None = None,
        stock_role: str | None = None,
        plan_status: str | None = None,
        strategy_version: str | None = None,
        data_status: str | None = None,
        industry_status: str | None = None,
        limit: int = 100,
    ) -> list[SelectedStockRunSummary]:
        statement = select(SelectedStockAnalysisRun)
        if symbol:
            statement = statement.where(SelectedStockAnalysisRun.symbol == symbol)
        if analysis_date:
            statement = statement.where(
                SelectedStockAnalysisRun.analysis_date == analysis_date
            )
        if plan_status:
            statement = statement.where(SelectedStockAnalysisRun.status == plan_status)
        if strategy_version:
            statement = statement.where(
                SelectedStockAnalysisRun.strategy_version == strategy_version
            )
        rows = self.db.scalars(
            statement.order_by(
                SelectedStockAnalysisRun.analysis_date.desc(),
                SelectedStockAnalysisRun.id.desc(),
            ).limit(limit)
        ).all()
        summaries = []
        for row in rows:
            result = SelectedStockAnalysisResult.model_validate(row.result_snapshot)
            if cycle_state and result.cycle_state.value != cycle_state:
                continue
            if stock_role and result.stock_role.value != stock_role:
                continue
            if data_status and result.data_status.value != data_status:
                continue
            if (
                industry_status
                and result.industry_context_status.value != industry_status
            ):
                continue
            summaries.append(
                SelectedStockRunSummary(
                    run_id=row.id,
                    symbol=row.symbol,
                    analysis_date=row.analysis_date,
                    cycle_state=result.cycle_state.value,
                    stock_role=result.stock_role.value,
                    plan_status=result.plan_status.value,
                    strategy_version=row.strategy_version,
                    data_status=result.data_status.value,
                    industry_status=result.industry_context_status.value,
                    created_at=row.created_at,
                )
            )
        return summaries

    def get_run(self, run_id: int) -> SelectedStockAnalysisResult:
        row = self.db.get(SelectedStockAnalysisRun, run_id)
        if row is None:
            raise LookupError("selected-stock analysis run not found")
        return SelectedStockAnalysisResult.model_validate(row.result_snapshot)

    def compare(self, run_id: int, other_run_id: int) -> SelectedStockRunComparison:
        before_result = self.get_run(run_id)
        after_result = self.get_run(other_run_id)
        if before_result.stock_code != after_result.stock_code:
            raise ValueError("selected-stock runs must use the same symbol")
        before = _comparison_snapshot(before_result)
        after = _comparison_snapshot(after_result)
        changes = {
            key: {"before": before[key], "after": after[key]}
            for key in before
            if before[key] != after[key]
        }
        return SelectedStockRunComparison(
            run_id=run_id,
            other_run_id=other_run_id,
            symbol=before_result.stock_code,
            before=before,
            after=after,
            changes=changes,
        )


__all__ = [
    "SelectedStockHistoryService",
    "SelectedStockRunComparison",
    "SelectedStockRunSummary",
]
