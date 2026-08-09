from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_hub.market_subjects import index_daily_subject, stock_daily_subject
from app.data_hub.quality import DataQualityStatus
from app.data_hub.trading_calendar import get_trading_calendar
from app.domain.hashing import canonical_hash
from app.models import DataQualityRecord, MarketDailyBar
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


class BatchReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(pattern=r"^\d{6}$")
    start_date: date
    end_date: date
    sampling_frequency: Literal["DAILY", "WEEKLY"] = "WEEKLY"
    strategy_version: Literal["2.0.0"] = "2.0.0"
    account_size: Decimal | None = Field(default=None, gt=0)

    def model_post_init(self, _context: Any) -> None:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")


class BatchReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    start_date: date
    end_date: date
    sampling_frequency: str
    strategy_version: str
    analysis_sample_count: int
    valid_trigger_count: int
    sample_status: Literal["SUFFICIENT", "SAMPLE_INSUFFICIENT"]
    plan_status_counts: dict[str, int]
    holding_action_counts: dict[str, int]
    horizon_returns: dict[str, dict[str, Decimal | int | None]]
    maximum_favorable_excursion: Decimal | None
    maximum_adverse_excursion: Decimal | None
    stop_trigger_rate: Decimal | None
    first_target_trigger_rate: Decimal | None
    second_target_trigger_rate: Decimal | None
    win_rate: Decimal | None
    average_return: Decimal | None
    median_return: Decimal | None
    maximum_consecutive_failures: int
    maximum_drawdown: Decimal | None
    grouped_by_role: dict[str, dict[str, Decimal | int | None]]
    grouped_by_cycle: dict[str, dict[str, Decimal | int | None]]
    discipline_rule_outcomes: dict[str, dict[str, int]]
    strategy_comparisons: dict[str, dict[str, Any]]
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
        market_price_observed_at=get_trading_calendar().session_close_at(
            visible_stock[-1]["trade_date"]
        ),
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


def _summary(values: list[Decimal]) -> dict[str, Decimal | int | None]:
    ordered = sorted(values)
    return {
        "count": len(values),
        "minimum": ordered[0] if ordered else None,
        "median": ordered[len(ordered) // 2] if ordered else None,
        "average": sum(ordered, Decimal("0")) / Decimal(len(ordered))
        if ordered
        else None,
        "maximum": ordered[-1] if ordered else None,
    }


def _maximum_drawdown(returns: list[Decimal]) -> Decimal | None:
    if not returns:
        return None
    equity = Decimal("1")
    peak = equity
    worst = Decimal("0")
    for value in returns:
        equity *= Decimal("1") + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - Decimal("1"))
    return worst


def _maximum_failures(returns: list[Decimal]) -> int:
    maximum = current = 0
    for value in returns:
        current = current + 1 if value <= 0 else 0
        maximum = max(maximum, current)
    return maximum


class SelectedStockReplayService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.calendar = get_trading_calendar()

    def _series(self, capability: str, subject) -> list[dict[str, Any]]:
        record = self.db.scalar(
            select(DataQualityRecord)
            .where(
                DataQualityRecord.capability == capability,
                DataQualityRecord.subject_type == subject.subject_type,
                DataQualityRecord.subject_id == subject.subject_id,
                DataQualityRecord.semantic_key == subject.semantic_key,
                DataQualityRecord.persisted.is_(True),
                DataQualityRecord.quality_status.in_(
                    {
                        DataQualityStatus.VERIFIED.value,
                        DataQualityStatus.SINGLE_SOURCE.value,
                    }
                ),
            )
            .order_by(DataQualityRecord.observed_at.desc(), DataQualityRecord.id.desc())
        )
        if record is None:
            return []
        rows = self.db.scalars(
            select(MarketDailyBar)
            .where(MarketDailyBar.quality_record_id == record.id)
            .order_by(MarketDailyBar.trade_date)
        ).all()
        return [
            {
                "trade_date": row.trade_date,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
            }
            for row in rows
        ]

    def run(self, request: BatchReplayRequest) -> BatchReplayResult:
        stock_rows = self._series(
            "market.daily.qfq",
            stock_daily_subject(request.symbol, "qfq", "CNY", "share"),
        )
        benchmark_rows = self._series(
            "market.index_daily",
            index_daily_subject("CSI000300", "unadjusted", "CNY", "share"),
        )
        if not stock_rows or not benchmark_rows:
            raise ValueError("persisted stock and CSI300 histories are required")
        candidates = [
            row["trade_date"]
            for index, row in enumerate(stock_rows)
            if index >= 119 and request.start_date <= row["trade_date"] <= request.end_date
        ]
        if request.sampling_frequency == "WEEKLY":
            candidates = candidates[::5]
        samples: list[SelectedStockReplayResult] = []
        for analysis_date in candidates:
            samples.append(
                replay_selected_stock(
                    request=SelectedStockAnalysisRequest(
                        stock_code=request.symbol,
                        analysis_date=analysis_date,
                        account_size=request.account_size,
                    ),
                    generated_at=self.calendar.session_close_at(analysis_date),
                    stock_rows=stock_rows,
                    benchmark_rows=benchmark_rows,
                )
            )
        plan_counts: dict[str, int] = {}
        holding_counts: dict[str, int] = {}
        horizon_values: dict[int, list[Decimal]] = {5: [], 10: [], 20: []}
        mfe: list[Decimal] = []
        mae: list[Decimal] = []
        stop_hits = first_hits = second_hits = 0
        role_values: dict[str, list[Decimal]] = {}
        cycle_values: dict[str, list[Decimal]] = {}
        discipline: dict[str, dict[str, int]] = {}
        product_shadow: dict[str, int] = {}
        for sample in samples:
            status = sample.plan.plan_status.value
            plan_counts[status] = plan_counts.get(status, 0) + 1
            if status in {"HOLD", "REDUCE", "EXIT"}:
                holding_counts[status] = holding_counts.get(status, 0) + 1
            horizon_by_days = {item.sessions: item for item in sample.horizons}
            for sessions, horizon in horizon_by_days.items():
                if horizon.ending_return is not None:
                    horizon_values[sessions].append(horizon.ending_return)
            horizon20 = horizon_by_days[20]
            if horizon20.maximum_favorable_excursion is not None:
                mfe.append(horizon20.maximum_favorable_excursion)
            if horizon20.maximum_adverse_excursion is not None:
                mae.append(horizon20.maximum_adverse_excursion)
            stop_hits += int(horizon20.stop_touched)
            first_hits += int(horizon20.first_target_touched)
            second_hits += int(horizon20.second_target_touched)
            if horizon20.ending_return is not None:
                role_values.setdefault(sample.plan.stock_role.value, []).append(
                    horizon20.ending_return
                )
                cycle_values.setdefault(sample.plan.cycle_state.value, []).append(
                    horizon20.ending_return
                )
            for rule in sample.plan.survival_discipline:
                counts = discipline.setdefault(rule.rule_code, {})
                counts[rule.status.value] = counts.get(rule.status.value, 0) + 1
            product_status = sample.plan.product_v1_comparison.product_v1_status
            product_shadow[product_status] = product_shadow.get(product_status, 0) + 1
        returns20 = horizon_values[20]
        valid_trigger_count = sum(
            count
            for status, count in plan_counts.items()
            if status in {"ENTRY_ALLOWED", "HOLD", "REDUCE", "EXIT"}
        )
        denominator = Decimal(len(samples)) if samples else None
        grouped_roles = {key: _summary(values) for key, values in role_values.items()}
        grouped_cycles = {key: _summary(values) for key, values in cycle_values.items()}
        result_payload = {
            "symbol": request.symbol,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "sampling_frequency": request.sampling_frequency,
            "strategy_version": request.strategy_version,
            "analysis_sample_count": len(samples),
            "valid_trigger_count": valid_trigger_count,
            "sample_status": "SUFFICIENT"
            if valid_trigger_count >= 30
            else "SAMPLE_INSUFFICIENT",
            "plan_status_counts": plan_counts,
            "holding_action_counts": holding_counts,
            "horizon_returns": {
                str(sessions): _summary(values)
                for sessions, values in horizon_values.items()
            },
            "maximum_favorable_excursion": max(mfe) if mfe else None,
            "maximum_adverse_excursion": min(mae) if mae else None,
            "stop_trigger_rate": Decimal(stop_hits) / denominator
            if denominator
            else None,
            "first_target_trigger_rate": Decimal(first_hits) / denominator
            if denominator
            else None,
            "second_target_trigger_rate": Decimal(second_hits) / denominator
            if denominator
            else None,
            "win_rate": Decimal(sum(value > 0 for value in returns20))
            / Decimal(len(returns20))
            if returns20
            else None,
            "average_return": sum(returns20, Decimal("0")) / Decimal(len(returns20))
            if returns20
            else None,
            "median_return": sorted(returns20)[len(returns20) // 2]
            if returns20
            else None,
            "maximum_consecutive_failures": _maximum_failures(returns20),
            "maximum_drawdown": _maximum_drawdown(returns20),
            "grouped_by_role": grouped_roles,
            "grouped_by_cycle": grouped_cycles,
            "discipline_rule_outcomes": discipline,
            "strategy_comparisons": {
                "CSV_V2_BASELINE": {"plan_status_counts": plan_counts},
                "CSV_V2_SURVIVAL": {
                    "blocked_rule_count": sum(
                        counts.get("BLOCK", 0) for counts in discipline.values()
                    ),
                    "rule_outcomes": discipline,
                },
                "PRODUCT_V1_SHADOW": {"signal_status_counts": product_shadow},
            },
        }
        return BatchReplayResult(
            **result_payload,
            replay_digest=canonical_hash(result_payload),
        )


__all__ = [
    "ReplayHorizon",
    "BatchReplayRequest",
    "BatchReplayResult",
    "SelectedStockReplayService",
    "SelectedStockReplayResult",
    "replay_selected_stock",
]
