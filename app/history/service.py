from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.market_subjects import (
    index_daily_subject,
    stock_daily_subject,
    stock_turnover_subject,
)
from app.data_hub.router import DataHubRouter, ProviderResult
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    shanghai_now,
    to_shanghai_aware,
    to_utc_storage_naive,
)
from app.discovery.contracts import DiscoveryConfig, IndustryDiscoveryInput
from app.discovery.scoring import IndustryDiscoveryScorer
from app.domain.quality import DataQualityStatus, worst_quality
from app.history.contracts import HistoryRequirementPlan
from app.history.planning import HistoryRequirementPlanner
from app.models import (
    DataQualityRecord,
    DataProviderCallLog,
    HistoricalDataBootstrapItem,
    HistoricalDataBootstrapRun,
    IndustryAnalysisSnapshot,
    MarketDailyBar,
    MarketTurnoverSnapshot,
)
from app.services.history_persistence import (
    persist_index_history_window,
    persist_stock_daily_window,
    persist_stock_history_bundle,
    persist_turnover_history_window,
)
from app.services.market_cache import resolve_cached_series
from app.services.product_data import resolve_product_cache


_PURPOSE = "CANDIDATE_DISCOVERY"
_TRUSTED = {"VERIFIED", "SINGLE_SOURCE"}
_HISTORY_PROVIDER_IDENTITY = "freestockdb+baostock-benchmark"


class HistoryPlanningBlocked(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _quality(*statuses: str) -> str:
    return worst_quality([DataQualityStatus(item) for item in statuses]).value


class HistoricalDataBootstrapService:
    def __init__(
        self,
        db: Session,
        *,
        router: DataHubRouter | None = None,
        planning_router: DataHubRouter | None = None,
        settings: Settings | None = None,
        calendar: TradingCalendar | None = None,
        plan_factory: Callable[..., HistoryRequirementPlan] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.calendar = calendar or get_trading_calendar()
        self.now_fn = now_fn or shanghai_now
        if router is None:
            from app.composition.history import build_history_data_hub

            router = build_history_data_hub(
                db, calendar=self.calendar, now_fn=self.now_fn
            )
        self.router = router
        if planning_router is None:
            from app.composition.data_hub import build_data_hub

            planning_router = build_data_hub(
                db, calendar=self.calendar, now_fn=self.now_fn
            )
        self.planning_router = planning_router
        self.plan_factory = plan_factory or self._build_plan

    def _now(self) -> datetime:
        value = self.now_fn()
        return to_shanghai_aware(value, naive_is_shanghai=value.tzinfo is None)

    def _discovery_config(self) -> DiscoveryConfig:
        return DiscoveryConfig(
            max_industries=self.settings.candidate_discovery_max_industries,
            max_candidates_per_industry=(
                self.settings.candidate_discovery_max_per_industry
            ),
            max_candidates=self.settings.candidate_discovery_max_candidates,
            candidate_min_history_coverage_ratio=(
                self.settings.candidate_min_history_coverage_ratio
            ),
        )

    def _history_adapter_identity(self) -> str:
        return (
            f"freestockdb:{self.settings.freestockdb_adapter_version};"
            f"baostock:{self.settings.baostock_adapter_version}"
        )

    def _planning_inputs(
        self, *, trade_date: date, now: datetime
    ) -> tuple[tuple[IndustryDiscoveryInput, ...], dict[str, tuple[str, ...]]]:
        from app.discovery.service import CandidateDiscoveryService
        from app.watchlist.context import latest_market_state

        discovery = CandidateDiscoveryService(
            self.db,
            router=self.planning_router,
            settings=self.settings,
            calendar=self.calendar,
        )
        market = latest_market_state(self.db, evaluated_at=now)
        if not market.executable:
            raise HistoryPlanningBlocked("MARKET_CONTEXT_NOT_EXECUTABLE")
        daily = discovery._refresh_product_universe(
            constituents=False, day=trade_date, now=now
        )
        members = discovery._refresh_product_universe(
            constituents=True, day=trade_date, now=now
        )
        capital = discovery._refresh_discovery_capability(
            "market.industry.capital_flow", trade_date, now
        )
        limit_pool = discovery._refresh_discovery_capability(
            "market.limit_up_pool", trade_date, now
        )
        broken_pool = discovery._refresh_discovery_capability(
            "market.broken_limit_pool", trade_date, now
        )
        selections = (daily, members, capital, limit_pool, broken_pool)
        if not all(item.executable for item in selections):
            raise HistoryPlanningBlocked("INDUSTRY_PLANNING_DATA_NOT_EXECUTABLE")

        daily_by_key: dict[str, list[Any]] = {}
        for row in daily.rows:
            daily_by_key.setdefault(row.industry_key, []).append(row)
        members_by_key: dict[str, list[Any]] = {}
        for row in members.rows:
            members_by_key.setdefault(row.industry_key, []).append(row)
        capital_by_key = {row.industry_key: row for row in capital.rows}
        assessments = {
            row.industry_name: row
            for row in self.db.scalars(
                select(IndustryAnalysisSnapshot).where(
                    IndustryAnalysisSnapshot.trade_date == trade_date
                )
            ).all()
        }
        limit_by_name: dict[str, int] = {}
        broken_by_name: dict[str, int] = {}
        for row in limit_pool.rows:
            if row.industry_name:
                limit_by_name[row.industry_name] = (
                    limit_by_name.get(row.industry_name, 0) + 1
                )
        for row in broken_pool.rows:
            if row.industry_name:
                broken_by_name[row.industry_name] = (
                    broken_by_name.get(row.industry_name, 0) + 1
                )
        inputs = []
        constituents: dict[str, tuple[str, ...]] = {}
        for key in sorted(set(daily_by_key) & set(members_by_key)):
            history = sorted(daily_by_key[key], key=lambda row: row.trade_date)
            latest = history[-1]
            flow = capital_by_key.get(key)
            assessment = assessments.get(latest.industry_name)
            total_events = limit_by_name.get(latest.industry_name, 0) + broken_by_name.get(
                latest.industry_name, 0
            )
            broken_rate = (
                Decimal(broken_by_name.get(latest.industry_name, 0))
                / Decimal(total_events)
                if total_events
                else Decimal("0")
            )
            status = _quality(
                daily.effective_quality.effective_quality.value,
                members.effective_quality.effective_quality.value,
                capital.effective_quality.effective_quality.value,
                limit_pool.effective_quality.effective_quality.value,
                broken_pool.effective_quality.effective_quality.value,
            )
            if flow is None or assessment is None:
                status = "MISSING"
            inputs.append(
                IndustryDiscoveryInput(
                    industry_key=key,
                    industry_name=latest.industry_name,
                    classification=assessment.classification if assessment else "NONE",
                    relative_strength_5d=None,
                    relative_strength_10d=None,
                    relative_strength_20d=None,
                    amount_share=latest.amount_share,
                    advance_ratio=latest.advance_ratio,
                    limit_up_count=latest.limit_up_count,
                    leader_strength=latest.leader_strength,
                    new_high_ratio=latest.new_high_ratio,
                    net_inflow_1d=flow.net_inflow_1d if flow else None,
                    net_inflow_5d=flow.net_inflow_5d if flow else None,
                    net_inflow_10d=flow.net_inflow_10d if flow else None,
                    broken_limit_rate=broken_rate,
                    quality_status=status,
                    evidence_references=(
                        f"quality:{daily.quality_record_id}",
                        f"quality:{members.quality_record_id}",
                        f"quality:{capital.quality_record_id}",
                        f"quality:{limit_pool.quality_record_id}",
                        f"quality:{broken_pool.quality_record_id}",
                    ),
                    total_constituents=0,
                    historical_data_ready=0,
                    historical_data_missing=0,
                    coverage_ratio=Decimal("0"),
                    constituents=(),
                )
            )
            constituents[key] = tuple(
                sorted({row.symbol for row in members_by_key[key]})
            )
        if not inputs:
            raise HistoryPlanningBlocked("NO_EXECUTABLE_INDUSTRY_DATA")
        return tuple(inputs), constituents

    def _build_plan(self, *, trade_date: date, now: datetime) -> HistoryRequirementPlan:
        config = self._discovery_config()
        industries, constituents = self._planning_inputs(
            trade_date=trade_date, now=now
        )
        return HistoryRequirementPlanner(
            scorer=IndustryDiscoveryScorer(config),
            minimum_rows=config.minimum_history_rows,
            lookback_sessions=self.settings.freestockdb_history_lookback_sessions,
            rewrite_sessions=self.settings.freestockdb_refresh_rewrite_sessions,
            adapter_version=self._history_adapter_identity(),
        ).build(
            trade_date=trade_date,
            industries=industries,
            constituents=constituents,
            calendar=self.calendar,
        )

    def _get_or_create_run(
        self, plan: HistoryRequirementPlan, *, now: datetime
    ) -> HistoricalDataBootstrapRun:
        run = self.db.scalar(
            select(HistoricalDataBootstrapRun).where(
                HistoricalDataBootstrapRun.purpose == _PURPOSE,
                HistoricalDataBootstrapRun.market == "CN-A",
                HistoricalDataBootstrapRun.trade_date == plan.trade_date,
                HistoricalDataBootstrapRun.plan_hash == plan.plan_hash,
            )
        )
        if run is not None:
            return run
        stored = to_utc_storage_naive(now)
        run = HistoricalDataBootstrapRun(
            purpose=_PURPOSE,
            market="CN-A",
            trade_date=plan.trade_date,
            status="PENDING",
            provider_id=_HISTORY_PROVIDER_IDENTITY,
            adapter_version=self._history_adapter_identity(),
            config_hash=plan.config_hash,
            plan_hash=plan.plan_hash,
            required_symbols=list(plan.benchmark_symbols + plan.required_stock_symbols),
            ready_symbols=0,
            failed_symbols=0,
            benchmark_ready=False,
            total_rows_written=0,
            coverage_ratio=Decimal("0"),
            started_at=stored,
            completed_at=None,
            blocked_reasons=[],
            created_at=stored,
        )
        try:
            with self.db.begin_nested():
                self.db.add(run)
                self.db.flush()
        except IntegrityError:
            run = self.db.scalar(
                select(HistoricalDataBootstrapRun).where(
                    HistoricalDataBootstrapRun.purpose == _PURPOSE,
                    HistoricalDataBootstrapRun.market == "CN-A",
                    HistoricalDataBootstrapRun.trade_date == plan.trade_date,
                    HistoricalDataBootstrapRun.plan_hash == plan.plan_hash,
                )
            )
            if run is None:
                raise
        return run

    def _item(
        self,
        run: HistoricalDataBootstrapRun,
        *,
        symbol: str,
        capability: str,
        adjustment: str,
        plan: HistoryRequirementPlan,
    ) -> HistoricalDataBootstrapItem:
        item = self.db.scalar(
            select(HistoricalDataBootstrapItem).where(
                HistoricalDataBootstrapItem.run_id == run.id,
                HistoricalDataBootstrapItem.symbol == symbol,
                HistoricalDataBootstrapItem.capability == capability,
            )
        )
        if item is None:
            item = HistoricalDataBootstrapItem(
                run_id=run.id,
                symbol=symbol,
                capability=capability,
                adjustment=adjustment,
                status="PENDING",
                requested_start=plan.start_date,
                requested_end=plan.end_date,
                rows_received=0,
                rows_written=0,
                first_trade_date=None,
                last_trade_date=None,
                quality_record_id=None,
                normalized_digest=None,
                error_code=None,
                error_message=None,
                started_at=None,
                completed_at=None,
            )
            self.db.add(item)
            self.db.flush()
        return item

    def _daily_cache(self, symbol: str, plan: HistoryRequirementPlan, now: datetime):
        adjustment = "unadjusted" if symbol == "CSI000300" else "qfq"
        capability = "market.index_daily" if symbol == "CSI000300" else "market.daily.qfq"
        subject = (
            index_daily_subject(symbol, adjustment, "CNY", "share")
            if symbol == "CSI000300"
            else stock_daily_subject(symbol, adjustment, "CNY", "share")
        )
        selected = resolve_cached_series(
            self.db,
            cache_symbol=symbol,
            capability=capability,
            subject=subject,
            adjustment=adjustment,
            price_unit="CNY",
            volume_unit="share",
            min_rows=plan.minimum_rows,
            evaluated_at=now,
        )
        ready = (
            selected.executable
            and bool(selected.bars)
            and selected.bars[-1].trade_date == plan.end_date
        )
        return selected, ready

    def _turnover_cache(self, symbol: str, plan: HistoryRequirementPlan, now: datetime):
        selected = resolve_product_cache(
            self.db,
            capability="market.turnover.daily",
            subject=stock_turnover_subject(symbol),
            evaluated_at=now,
        )
        ready = (
            selected.executable
            and len(selected.rows) >= plan.minimum_rows
            and selected.rows[-1].trade_date == plan.end_date
        )
        return selected, ready

    @staticmethod
    def _result_dates(result: ProviderResult) -> tuple[date | None, date | None, int]:
        value = result.value
        rows = value.get("rows", []) if isinstance(value, dict) else value or []
        dates = [getattr(row, "trade_date", None) or row.get("date") for row in rows]
        return (dates[0], dates[-1], len(rows)) if dates else (None, None, 0)

    def _complete_item(
        self,
        item: HistoricalDataBootstrapItem,
        result: ProviderResult,
        *,
        rows_written: int,
        now: datetime,
    ) -> None:
        first, last, received = self._result_dates(result)
        item.status = "SUCCEEDED"
        item.rows_received = received
        item.rows_written = rows_written
        item.first_trade_date = first
        item.last_trade_date = last
        item.quality_record_id = result.quality_record_id
        item.normalized_digest = result.normalized_digest
        item.error_code = None
        item.error_message = None
        item.completed_at = to_utc_storage_naive(now)

    def _skip_item(
        self,
        item: HistoricalDataBootstrapItem,
        selection,
        *,
        now: datetime,
    ) -> None:
        rows = selection.bars if hasattr(selection, "bars") else selection.rows
        item.status = "SKIPPED_FRESH"
        item.rows_received = len(rows)
        item.rows_written = 0
        item.first_trade_date = rows[0].trade_date if rows else None
        item.last_trade_date = rows[-1].trade_date if rows else None
        item.quality_record_id = selection.quality_record_id
        record = self.db.get(DataQualityRecord, selection.quality_record_id)
        item.normalized_digest = record.normalized_digest if record else None
        item.error_code = None
        item.error_message = None
        item.completed_at = to_utc_storage_naive(now)

    def _fail_item(
        self, item: HistoricalDataBootstrapItem, exc: Exception, *, now: datetime
    ) -> None:
        item.status = "FAILED"
        item.error_code = (
            "DATA_NOT_EXECUTABLE"
            if isinstance(exc, ProviderUnavailableError)
            else type(exc).__name__.upper()[:100]
        )
        item.error_message = str(exc).replace("\n", " ")[:500]
        item.completed_at = to_utc_storage_naive(now)

    def _prepare_benchmark(
        self,
        run: HistoricalDataBootstrapRun,
        plan: HistoryRequirementPlan,
        *,
        force_refresh: bool,
        now: datetime,
    ) -> bool:
        item = self._item(
            run,
            symbol="CSI000300",
            capability="market.index_daily",
            adjustment="unadjusted",
            plan=plan,
        )
        cache, ready = self._daily_cache("CSI000300", plan, now)
        if ready and not force_refresh:
            self._skip_item(item, cache, now=now)
            return True
        item.status = "RUNNING"
        item.started_at = to_utc_storage_naive(now)
        try:
            result = self.router.get_index_history(
                "CSI000300", plan.start_date, plan.end_date
            )
            written = persist_index_history_window(
                self.db,
                self.router,
                result=result,
                subject=index_daily_subject(
                    "CSI000300", "unadjusted", "CNY", "share"
                ),
                requested_start=plan.start_date,
                requested_end=plan.end_date,
                minimum_rows=plan.minimum_rows,
            )
            self._complete_item(item, result, rows_written=written, now=now)
            run.total_rows_written += written
            return True
        except Exception as exc:
            self._fail_item(item, exc, now=now)
            return False

    def _prepare_stock(
        self,
        run: HistoricalDataBootstrapRun,
        plan: HistoryRequirementPlan,
        symbol: str,
        *,
        force_refresh: bool,
        now: datetime,
    ) -> bool:
        daily_item = self._item(
            run,
            symbol=symbol,
            capability="market.daily.qfq",
            adjustment="qfq",
            plan=plan,
        )
        turnover_item = self._item(
            run,
            symbol=symbol,
            capability="market.turnover.daily",
            adjustment="qfq",
            plan=plan,
        )
        daily_cache, daily_ready = self._daily_cache(symbol, plan, now)
        turnover_cache, turnover_ready = self._turnover_cache(symbol, plan, now)
        refresh_daily = force_refresh or not daily_ready
        refresh_turnover = force_refresh or not turnover_ready
        if not refresh_daily:
            self._skip_item(daily_item, daily_cache, now=now)
        if not refresh_turnover:
            self._skip_item(turnover_item, turnover_cache, now=now)
        daily_result = turnover_result = None
        try:
            if refresh_daily:
                daily_item.status = "RUNNING"
                daily_item.started_at = to_utc_storage_naive(now)
                daily_result = self.router.get_history(
                    symbol, plan.start_date, plan.end_date
                )
                daily_result.require_trusted_value()
            if refresh_turnover:
                turnover_item.status = "RUNNING"
                turnover_item.started_at = to_utc_storage_naive(now)
                turnover_result = self.router.get_turnover_daily(
                    symbol, plan.start_date, plan.end_date
                )
                turnover_result.require_trusted_value()
            if refresh_daily and refresh_turnover:
                written = persist_stock_history_bundle(
                    self.db,
                    self.router,
                    daily_result=daily_result,
                    turnover_result=turnover_result,
                    requested_start=plan.start_date,
                    requested_end=plan.end_date,
                    minimum_rows=plan.minimum_rows,
                )
                daily_written = len(daily_result.value)
                turnover_written = len(turnover_result.value)
            elif refresh_daily:
                expected = {row.trade_date for row in turnover_cache.rows}
                daily_written = persist_stock_daily_window(
                    self.db,
                    self.router,
                    result=daily_result,
                    expected_trade_dates=expected,
                    requested_start=plan.start_date,
                    requested_end=plan.end_date,
                    minimum_rows=plan.minimum_rows,
                )
                turnover_written = 0
                written = daily_written
            elif refresh_turnover:
                expected = {row.trade_date for row in daily_cache.bars}
                turnover_written = persist_turnover_history_window(
                    self.db,
                    self.router,
                    result=turnover_result,
                    expected_trade_dates=expected,
                    requested_start=plan.start_date,
                    requested_end=plan.end_date,
                    minimum_rows=plan.minimum_rows,
                )
                daily_written = 0
                written = turnover_written
            else:
                return True
            if daily_result is not None:
                self._complete_item(
                    daily_item, daily_result, rows_written=daily_written, now=now
                )
            if turnover_result is not None:
                self._complete_item(
                    turnover_item,
                    turnover_result,
                    rows_written=turnover_written,
                    now=now,
                )
            run.total_rows_written += written
            return True
        except Exception as exc:
            if refresh_daily:
                self._fail_item(daily_item, exc, now=now)
            if refresh_turnover:
                self._fail_item(turnover_item, exc, now=now)
            return False

    def _blocked_without_plan(
        self, *, trade_date: date, code: str, now: datetime
    ) -> HistoricalDataBootstrapRun:
        plan = HistoryRequirementPlan.create(
            trade_date=trade_date,
            benchmark_symbols=(),
            selected_industries=(),
            required_stock_symbols=(),
            required_capabilities=(),
            start_date=trade_date,
            end_date=trade_date,
            minimum_rows=1,
            config={
                "planning_error": code,
                "adapter_version": self._history_adapter_identity(),
            },
        )
        run = self._get_or_create_run(plan, now=now)
        run.status = "BLOCKED"
        run.blocked_reasons = [code]
        run.completed_at = to_utc_storage_naive(now)
        self.db.commit()
        self.db.refresh(run)
        return run

    def run(
        self,
        *,
        trade_date: date | None = None,
        force_refresh: bool = False,
        now: datetime | None = None,
    ) -> HistoricalDataBootstrapRun:
        current = to_shanghai_aware(
            now or self._now(), naive_is_shanghai=now is not None and now.tzinfo is None
        )
        day = trade_date or self.calendar.latest_completed_session(current)
        try:
            plan = self.plan_factory(trade_date=day, now=current)
        except HistoryPlanningBlocked as exc:
            return self._blocked_without_plan(trade_date=day, code=exc.code, now=current)
        run = self._get_or_create_run(plan, now=current)
        if run.status == "SUCCEEDED" and not force_refresh:
            return run
        required_count = len(plan.required_stock_symbols)
        session_rows = max(
            plan.minimum_rows,
            self.settings.freestockdb_history_lookback_sessions,
            self.settings.freestockdb_refresh_rewrite_sessions,
        )
        predicted_rows = session_rows * (
            len(plan.benchmark_symbols) + 2 * required_count
        )
        if required_count > self.settings.history_bootstrap_max_symbols:
            run.status = "BLOCKED"
            run.blocked_reasons = ["HISTORY_BOOTSTRAP_SYMBOL_BUDGET_EXCEEDED"]
            run.completed_at = to_utc_storage_naive(current)
            self.db.commit()
            return run
        if predicted_rows > self.settings.history_bootstrap_max_rows:
            run.status = "BLOCKED"
            run.blocked_reasons = ["HISTORY_BOOTSTRAP_ROW_BUDGET_EXCEEDED"]
            run.completed_at = to_utc_storage_naive(current)
            self.db.commit()
            return run
        run.status = "RUNNING"
        run.started_at = to_utc_storage_naive(current)
        run.completed_at = None
        run.blocked_reasons = []
        run.total_rows_written = 0
        self.db.commit()
        started = time.monotonic()
        benchmark_ready = self._prepare_benchmark(
            run, plan, force_refresh=force_refresh, now=current
        )
        self.db.commit()
        ready = 0
        failed = 0
        duration_exceeded = False
        for symbol in plan.required_stock_symbols:
            if (
                time.monotonic() - started
                > self.settings.history_bootstrap_max_duration_seconds
            ):
                duration_exceeded = True
                break
            if self._prepare_stock(
                run,
                plan,
                symbol,
                force_refresh=force_refresh,
                now=current,
            ):
                ready += 1
            else:
                failed += 1
            self.db.commit()
        unprocessed = required_count - ready - failed
        failed += unprocessed
        coverage = (
            (Decimal(ready) / Decimal(required_count)).quantize(Decimal("0.000001"))
            if required_count
            else Decimal("0")
        )
        reasons = []
        if duration_exceeded:
            reasons.append("HISTORY_BOOTSTRAP_DURATION_BUDGET_EXCEEDED")
        if not benchmark_ready:
            reasons.append("BENCHMARK_HISTORY_MISSING")
        if coverage < self.settings.candidate_min_history_coverage_ratio:
            reasons.append("HISTORICAL_UNIVERSE_COVERAGE_INSUFFICIENT")
        run.benchmark_ready = benchmark_ready
        run.ready_symbols = ready
        run.failed_symbols = failed
        run.coverage_ratio = coverage
        run.blocked_reasons = reasons
        run.status = "SUCCEEDED" if not reasons else "BLOCKED"
        run.completed_at = to_utc_storage_naive(current)
        self.db.commit()
        self.db.refresh(run)
        return run


def discovery_history_status(db: Session, *, router: DataHubRouter | None = None) -> dict:
    latest = db.scalar(
        select(HistoricalDataBootstrapRun)
        .where(HistoricalDataBootstrapRun.purpose == _PURPOSE)
        .order_by(HistoricalDataBootstrapRun.created_at.desc())
    )
    provider_health = None
    if router is not None:
        providers = router.registry.providers_for("market.daily.qfq")
        provider = next((item for item in providers if item.provider_id == "freestockdb"), None)
        provider_health = provider.health_check(probe=True) if provider else None
        if provider_health is not None:
            latest_success = db.scalar(
                select(DataProviderCallLog)
                .where(
                    DataProviderCallLog.provider_id == "freestockdb",
                    DataProviderCallLog.status == "success",
                )
                .order_by(DataProviderCallLog.completed_at.desc())
            )
            latest_failure = db.scalar(
                select(DataProviderCallLog)
                .where(
                    DataProviderCallLog.provider_id == "freestockdb",
                    DataProviderCallLog.status == "failed",
                )
                .order_by(DataProviderCallLog.completed_at.desc())
            )
            provider_health["last_success_at"] = (
                latest_success.completed_at if latest_success else None
            )
            provider_health["last_error"] = (
                latest_failure.error if latest_failure else None
            )
    return {
        "latest_run": latest,
        "provider_health": provider_health,
        "latest_index_trade_date": db.scalar(
            select(func.max(MarketDailyBar.trade_date)).where(
                MarketDailyBar.symbol == "CSI000300"
            )
        ),
        "latest_stock_trade_date": db.scalar(
            select(func.max(MarketDailyBar.trade_date)).where(
                MarketDailyBar.symbol != "CSI000300"
            )
        ),
        "latest_turnover_trade_date": db.scalar(
            select(func.max(MarketTurnoverSnapshot.trade_date))
        ),
    }


__all__ = [
    "HistoricalDataBootstrapService",
    "HistoryPlanningBlocked",
    "discovery_history_status",
]
