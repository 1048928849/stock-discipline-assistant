from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.hashing import canonical_hash
from app.selected_stock.contracts import (
    ContextStatus,
    DataStatus,
    SelectedStockAnalysisRequest,
    SelectedStockAnalysisResult,
    SourceLineage,
)
from app.selected_stock.indicators import calculate_indicators
from app.selected_stock.strategy import CycleStructureValidationStrategyV2


class ReplayHorizon(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sessions: int = Field(ge=1, le=20)
    ending_date: date | None
    ending_return: Decimal | None
    maximum_favorable_excursion: Decimal | None
    maximum_adverse_excursion: Decimal | None
    stop_touched: bool
    first_target_touched: bool
    second_target_touched: bool


class SelectedStockReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis_date: date
    plan: SelectedStockAnalysisResult
    horizons: tuple[ReplayHorizon, ...]
    replay_digest: str
    notice: str = "Historical replay is not a profit promise."


def _through(rows: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    return [row for row in rows if row["trade_date"] <= day]


def _future(rows: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    return [row for row in rows if row["trade_date"] > day]


def replay_selected_stock(
    *,
    request: SelectedStockAnalysisRequest,
    generated_at: datetime,
    stock_rows: list[dict[str, Any]],
    benchmark_rows: list[dict[str, Any]],
    industry_rows: list[dict[str, Any]] | None = None,
    industry_name: str | None = None,
    data_status: DataStatus = DataStatus.FRESH,
    market_status: ContextStatus = ContextStatus.BREADTH_UNAVAILABLE,
    industry_status: ContextStatus = ContextStatus.INDUSTRY_CONTEXT_UNAVAILABLE,
    source_lineage: tuple[SourceLineage, ...] = (),
) -> SelectedStockReplayResult:
    if request.analysis_date is None:
        raise ValueError("historical replay requires a frozen analysis_date")
    analysis_date = request.analysis_date
    visible_stock = _through(stock_rows, analysis_date)
    visible_benchmark = _through(benchmark_rows, analysis_date)
    visible_industry = (
        _through(industry_rows, analysis_date) if industry_rows is not None else None
    )
    if not visible_stock or any(row["trade_date"] > analysis_date for row in visible_stock):
        raise ValueError("historical replay has no valid visible stock history")
    indicators = calculate_indicators(
        visible_stock,
        benchmark_rows=visible_benchmark,
        industry_rows=visible_industry,
    )
    snapshot_hash = canonical_hash(
        {
            "analysis_date": analysis_date,
            "stock_rows": visible_stock,
            "benchmark_rows": visible_benchmark,
            "industry_rows": visible_industry,
            "source_lineage": [item.model_dump(mode="json") for item in source_lineage],
        }
    )
    plan = CycleStructureValidationStrategyV2().build_plan(
        request=request,
        generated_at=generated_at,
        analysis_date=analysis_date,
        data_status=data_status,
        market_status=market_status,
        industry_status=industry_status,
        industry_name=industry_name,
        stock_row_count=len(visible_stock),
        indicators=indicators,
        quality_bindings=(),
        source_lineage=source_lineage,
        snapshot_hash=snapshot_hash,
        product_v1_status="FORMAL_EXECUTION_REMAINS_PRODUCT_V1",
        market_price_observed_at=datetime.combine(
            visible_stock[-1]["trade_date"],
            datetime.min.time(),
            tzinfo=generated_at.tzinfo,
        ).replace(hour=15),
    )
    future = _future(stock_rows, analysis_date)
    base_close = Decimal(str(visible_stock[-1]["close"]))
    stop = plan.price_plan.stop_loss
    first_target = plan.price_plan.first_take_profit
    second_target = plan.price_plan.second_take_profit
    horizons = []
    for sessions in (5, 10, 20):
        sample = future[:sessions]
        closes = [Decimal(str(row["close"])) for row in sample]
        highs = [Decimal(str(row["high"])) for row in sample]
        lows = [Decimal(str(row["low"])) for row in sample]
        horizons.append(
            ReplayHorizon(
                sessions=sessions,
                ending_date=sample[-1]["trade_date"] if sample else None,
                ending_return=(closes[-1] / base_close - Decimal("1")) if closes else None,
                maximum_favorable_excursion=(max(highs) / base_close - Decimal("1"))
                if highs
                else None,
                maximum_adverse_excursion=(min(lows) / base_close - Decimal("1"))
                if lows
                else None,
                stop_touched=bool(stop is not None and lows and min(lows) <= stop),
                first_target_touched=bool(
                    first_target is not None and highs and max(highs) >= first_target
                ),
                second_target_touched=bool(
                    second_target is not None and highs and max(highs) >= second_target
                ),
            )
        )
    replay_payload = {
        "analysis_date": analysis_date,
        "plan_digest": plan.result_digest,
        "horizons": [item.model_dump(mode="python") for item in horizons],
    }
    return SelectedStockReplayResult(
        analysis_date=analysis_date,
        plan=plan,
        horizons=tuple(horizons),
        replay_digest=canonical_hash(replay_payload),
    )


__all__ = [
    "ReplayHorizon",
    "SelectedStockReplayResult",
    "replay_selected_stock",
]
