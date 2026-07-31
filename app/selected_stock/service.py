from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.snapshot import ProductAnalysisSnapshot, SnapshotCapability
from app.composition.data_hub import build_selected_stock_data_hub
from app.composition.strategies import get_strategy_registry
from app.data_hub.market_subjects import (
    index_daily_subject,
    sector_daily_subject,
    stock_daily_subject,
)
from app.data_hub.quality import DataQualityStatus as HubQualityStatus
from app.data_hub.router import DataHubRouter, ProviderResult
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    shanghai_now,
    storage_naive_to_aware,
    time_storage_semantics_for_capability,
    to_shanghai_aware,
    to_utc_storage_naive,
    utc_storage_naive_to_aware,
)
from app.domain.hashing import canonical_hash
from app.domain.quality import DataQualityStatus
from app.domain.quality_subject import SubjectRef
from app.models import (
    Account,
    CompanyProfile,
    DataQualityRecord,
    Holding,
    MarketDailyBar,
    MarketRegimeSnapshot,
    SelectedStockAnalysisRun,
    Trade,
)
from app.selected_stock.contracts import (
    AccountContext,
    ContextStatus,
    DataStatus,
    QualityBinding,
    SelectedStockAnalysisRequest,
    SelectedStockAnalysisResult,
    SourceLineage,
)
from app.selected_stock.indicators import calculate_indicators
from app.selected_stock.industry import SelectedStockIndustryContextProvider
from app.selected_stock.strategy import CycleStructureValidationStrategyV2
from app.services.market_cache import mapping_series_bars, replace_market_series
from app.services.research_cache import persist_company_profile


STRATEGY_ID = "cycle_structure_validation_v2"
STRATEGY_VERSION = "2.0.0"
BENCHMARK_SYMBOL = "CSI000300"
MINIMUM_ROWS = 120
RECOMMENDED_ROWS = 250


class SelectedStockAnalysisError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _SeriesData:
    capability: str
    subject: SubjectRef
    bars: tuple[MarketDailyBar, ...]
    record: DataQualityRecord
    result: ProviderResult | None


def _aware_record_time(record: DataQualityRecord, value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return storage_naive_to_aware(
        value,
        semantics=time_storage_semantics_for_capability(record.capability),
    )


def _quality_binding(series: _SeriesData) -> QualityBinding:
    observed = _aware_record_time(series.record, series.record.observed_at)
    if observed is None or not series.record.normalized_digest:
        raise SelectedStockAnalysisError(
            "INCOMPLETE_QUALITY_LINEAGE",
            f"{series.capability} has incomplete persisted quality lineage",
        )
    return QualityBinding(
        capability=series.capability,
        subject_type=series.subject.subject_type,
        subject_id=series.subject.subject_id,
        semantic_key=series.subject.semantic_key,
        quality_record_id=series.record.id,
        quality_status=series.record.quality_status,
        observed_at=observed,
        normalized_digest=series.record.normalized_digest,
    )


def _lineage(series: _SeriesData) -> SourceLineage:
    observations = series.record.provider_observations or []
    details = observations[-1] if observations else {}
    fetched = _aware_record_time(series.record, series.record.fetched_at)
    observed = _aware_record_time(series.record, series.record.observed_at)
    return SourceLineage(
        capability=series.capability,
        provider_id=series.record.provider_id,
        source=series.bars[0].source if series.bars else series.record.provider_id,
        adjustment=series.record.adjustment,
        price_unit=series.record.price_unit,
        volume_unit=series.record.volume_unit,
        row_count=len(series.bars),
        observed_at=observed,
        fetched_at=fetched,
        request_digest=details.get("request_digest"),
        response_digest=details.get("response_digest")
        or series.record.normalized_digest,
        details={
            "fallback_used": series.record.fallback_used,
            "cache_used": series.record.cache_used,
            "persisted_row_count": series.record.row_count,
            "analysis_row_count": len(series.bars),
            "analysis_through": (
                series.bars[-1].trade_date.isoformat() if series.bars else None
            ),
            "provider_observations": observations,
        },
    )


def _rows(bars: tuple[MarketDailyBar, ...], *, through: date) -> list[dict[str, Any]]:
    return [
        {
            "trade_date": item.trade_date,
            "open": item.open,
            "high": item.high,
            "low": item.low,
            "close": item.close,
            "volume": item.volume,
        }
        for item in bars
        if item.trade_date <= through
    ]


class SelectedStockAnalysisService:
    def __init__(
        self,
        db: Session,
        *,
        router: DataHubRouter | None = None,
        calendar: TradingCalendar | None = None,
        now_fn: Callable[[], datetime] | None = None,
        industry_context_provider: SelectedStockIndustryContextProvider | None = None,
    ) -> None:
        self.db = db
        self.calendar = calendar or get_trading_calendar()
        self.now_fn = now_fn or shanghai_now
        self._router_injected = router is not None
        self.router = router or build_selected_stock_data_hub(
            db,
            calendar=self.calendar,
            now_fn=self.now_fn,
        )
        self.industry_context_provider = (
            industry_context_provider
            or SelectedStockIndustryContextProvider(db, calendar=self.calendar)
        )

    def _now(self) -> datetime:
        return to_shanghai_aware(self.now_fn())

    def _latest_series(
        self,
        *,
        capability: str,
        subject: SubjectRef,
        through: date,
    ) -> _SeriesData | None:
        record = self.db.scalar(
            select(DataQualityRecord)
            .where(
                DataQualityRecord.capability == capability,
                DataQualityRecord.subject_type == subject.subject_type,
                DataQualityRecord.subject_id == subject.subject_id,
                DataQualityRecord.semantic_key == subject.semantic_key,
                DataQualityRecord.persisted.is_(True),
            )
            .order_by(DataQualityRecord.observed_at.desc(), DataQualityRecord.id.desc())
        )
        if record is None:
            return None
        bars = tuple(
            self.db.scalars(
                select(MarketDailyBar)
                .where(
                    MarketDailyBar.quality_record_id == record.id,
                    MarketDailyBar.trade_date <= through,
                )
                .order_by(MarketDailyBar.trade_date, MarketDailyBar.id)
            ).all()
        )
        if not bars or len(bars) != min(record.row_count, len(bars)):
            return None
        return _SeriesData(capability, subject, bars, record, None)

    def _persist_result(
        self,
        *,
        result: ProviderResult,
        subject: SubjectRef,
        through: date,
    ) -> _SeriesData:
        payload = result.require_trusted_value()
        bars = (
            mapping_series_bars(
                payload,
                cache_symbol=subject.subject_id,
                adjustment=subject.semantic_key.split("/", 1)[0],
            )
            if isinstance(payload, dict)
            else list(payload)
        )
        stored = replace_market_series(
            self.db,
            self.router,
            result,
            bars,
            subject=subject,
            min_rows=1,
            maximum_trade_date=through,
        )
        record = self.db.get(DataQualityRecord, result.quality_record_id)
        if record is None or not record.persisted:
            raise SelectedStockAnalysisError(
                "PERSISTENCE_LINEAGE_INCOMPLETE",
                f"{result.capability} was not atomically persisted",
            )
        return _SeriesData(result.capability, subject, tuple(stored), record, result)

    def _stock_series(self, symbol: str, start: date, end: date) -> _SeriesData:
        subject = stock_daily_subject(symbol, "qfq", "CNY", "share")
        cached = self._latest_series(
            capability="market.daily.qfq", subject=subject, through=end
        )
        if cached and len(cached.bars) >= RECOMMENDED_ROWS and cached.bars[-1].trade_date == end:
            return cached
        try:
            result = self.router.get_history(symbol, start, end)
            return self._persist_result(result=result, subject=subject, through=end)
        except Exception as exc:
            if cached is not None:
                return cached
            raise SelectedStockAnalysisError(
                "STOCK_HISTORY_UNAVAILABLE",
                f"stock history is unavailable: {type(exc).__name__}",
            ) from exc

    def _benchmark_series(self, start: date, end: date) -> _SeriesData | None:
        subject = index_daily_subject(BENCHMARK_SYMBOL, "unadjusted", "CNY", "share")
        cached = self._latest_series(
            capability="market.index_daily", subject=subject, through=end
        )
        if cached and len(cached.bars) >= RECOMMENDED_ROWS and cached.bars[-1].trade_date == end:
            return cached
        try:
            result = self.router.get_index_history(BENCHMARK_SYMBOL, start, end)
            return self._persist_result(result=result, subject=subject, through=end)
        except Exception:
            return cached

    def _company_profile(self, symbol: str) -> tuple[str | None, SourceLineage | None]:
        profile = self.db.scalar(select(CompanyProfile).where(CompanyProfile.symbol == symbol))
        try:
            result = self.router.company_profile(symbol)
            profile = persist_company_profile(self.db, self.router, result)
            record = self.db.get(DataQualityRecord, result.quality_record_id)
            lineage = SourceLineage(
                capability="fundamental.profile",
                provider_id=result.provider_id,
                source=result.provider_id,
                row_count=1,
                observed_at=to_shanghai_aware(result.fetched_at),
                fetched_at=to_shanghai_aware(result.fetched_at),
                response_digest=result.normalized_digest,
                details={"quality_record_id": record.id if record else None},
            )
            return profile.industry, lineage
        except Exception:
            return (profile.industry if profile else None), None

    def _industry_series(
        self, industry: str | None, start: date, end: date
    ) -> _SeriesData | None:
        if not industry:
            return None
        subject = sector_daily_subject(industry, "unadjusted", "CNY", "share")
        cached = self._latest_series(
            capability="market.sector_daily", subject=subject, through=end
        )
        if cached and len(cached.bars) >= MINIMUM_ROWS and cached.bars[-1].trade_date == end:
            return cached
        try:
            result = self.router.get_sector_history(industry, start, end)
            return self._persist_result(result=result, subject=subject, through=end)
        except Exception:
            return cached

    def _market_context(self, analysis_date: date) -> tuple[ContextStatus, dict[str, Any]]:
        snapshot = self.db.scalar(
            select(MarketRegimeSnapshot)
            .where(
                MarketRegimeSnapshot.market_id == "CN-A",
                MarketRegimeSnapshot.trade_date <= analysis_date,
            )
            .order_by(MarketRegimeSnapshot.trade_date.desc(), MarketRegimeSnapshot.id.desc())
        )
        if (
            snapshot is None
            or snapshot.trade_date != analysis_date
            or snapshot.quality_status not in {
                HubQualityStatus.VERIFIED.value,
                HubQualityStatus.SINGLE_SOURCE.value,
            }
            or not snapshot.quality_bindings
        ):
            return ContextStatus.BREADTH_UNAVAILABLE, {}
        return ContextStatus.AVAILABLE, {
            "state": snapshot.state,
            "previous_state": snapshot.previous_state,
            "transition": snapshot.transition,
            "snapshot_hash": snapshot.product_snapshot_hash,
        }

    @staticmethod
    def _matches_manual(server: Decimal, supplied: Decimal, tolerance_pct: Decimal) -> bool:
        if server == supplied:
            return True
        denominator = max(abs(server), Decimal("0.01"))
        return abs(server - supplied) / denominator * Decimal("100") <= tolerance_pct

    def _realized_pnl_for_day(
        self,
        account_id: int,
        analysis_date: date,
    ) -> Decimal | None:
        trades = self.db.scalars(
            select(Trade)
            .where(Trade.account_id == account_id)
            .order_by(Trade.traded_at, Trade.id)
        ).all()
        positions: dict[str, tuple[int, Decimal]] = {}
        realized = Decimal("0")
        for trade in trades:
            quantity, average_cost = positions.get(trade.symbol, (0, Decimal("0")))
            side = str(trade.side).upper()
            if side in {"BUY", "买入"}:
                total = average_cost * quantity + trade.price * trade.quantity + trade.fee
                quantity += trade.quantity
                average_cost = total / quantity
            elif side in {"SELL", "卖出"}:
                if trade.quantity > quantity:
                    return None
                pnl = (trade.price - average_cost) * trade.quantity - trade.fee
                if to_shanghai_aware(
                    trade.traded_at,
                    naive_is_shanghai=trade.traded_at.tzinfo is None,
                ).date() == analysis_date:
                    realized += pnl
                quantity -= trade.quantity
                if quantity == 0:
                    average_cost = Decimal("0")
            positions[trade.symbol] = (quantity, average_cost)
        return realized

    def _account_context(
        self,
        request: SelectedStockAnalysisRequest,
        *,
        trusted_price: Decimal,
        analysis_date: date,
        observed_at: datetime,
    ) -> AccountContext | None:
        if request.account_id is None:
            return None
        account = self.db.get(Account, request.account_id)
        if account is None:
            raise SelectedStockAnalysisError(
                "ACCOUNT_NOT_FOUND",
                "selected-stock account_id does not exist",
            )
        holding = self.db.scalar(
            select(Holding).where(
                Holding.account_id == account.id,
                Holding.symbol == request.stock_code,
            )
        )
        quantity = holding.quantity if holding is not None else 0
        average_cost = holding.cost_price if holding is not None and quantity > 0 else None
        position_pct = (
            Decimal(quantity) * trusted_price / account.total_assets * Decimal("100")
            if quantity and account.total_assets
            else Decimal("0")
        )
        tolerance = Decimal("0.5")
        conflicts: list[str] = []
        comparisons = (
            ("account_size", account.total_assets, request.account_size),
            ("available_cash", account.available_cash, request.available_cash),
            ("average_cost", average_cost, request.average_cost),
            ("current_position_pct", position_pct, request.current_position_pct),
        )
        for field, server_value, supplied in comparisons:
            if supplied is None:
                continue
            if server_value is None or not self._matches_manual(
                Decimal(server_value), Decimal(supplied), tolerance
            ):
                conflicts.append(field)
        if (
            request.current_position_quantity is not None
            and request.current_position_quantity != quantity
        ):
            conflicts.append("current_position_quantity")
        if request.daily_pnl_source is not None:
            conflicts.append("daily_pnl")
        realized = self._realized_pnl_for_day(account.id, analysis_date)
        confirmed = any(
            value is not None
            for value in (
                request.account_size,
                request.available_cash,
                request.current_position_quantity,
                request.current_position_pct,
                request.average_cost,
            )
        )
        return AccountContext(
            account_id=account.id,
            source="SERVER_ACCOUNT",
            trust_status=(
                "ACCOUNT_INPUT_CONFLICT"
                if conflicts
                else "USER_CONFIRMED"
                if confirmed
                else "SERVER_LOADED"
            ),
            account_size=account.total_assets,
            available_cash=account.available_cash,
            current_position_quantity=quantity,
            current_position_pct=position_pct,
            average_cost=average_cost,
            daily_realized_pnl=realized,
            daily_unrealized_pnl=None,
            daily_loss_amount=None,
            daily_loss_pct=None,
            observed_at=utc_storage_naive_to_aware(
                max(
                    value
                    for value in (
                        account.updated_at,
                        holding.updated_at if holding is not None else None,
                    )
                    if value is not None
                )
            )
            if account.updated_at is not None
            else observed_at,
            conflict_fields=tuple(sorted(set(conflicts))),
            confidence=Decimal("1") if not conflicts else Decimal("0"),
        )

    def _data_status(
        self,
        stock: _SeriesData,
        benchmark: _SeriesData | None,
        analysis_date: date,
    ) -> DataStatus:
        rows = stock.bars
        if len(rows) < MINIMUM_ROWS:
            return DataStatus.INSUFFICIENT_DATA
        if stock.record.quality_status in {
            HubQualityStatus.CONFLICTED.value,
            HubQualityStatus.MISSING.value,
        }:
            return DataStatus.CONFLICTED_DATA
        if benchmark is None or not benchmark.bars:
            return DataStatus.CONFLICTED_DATA
        if rows[-1].trade_date > analysis_date:
            return DataStatus.CONFLICTED_DATA
        lag = self.calendar.session_lag(rows[-1].trade_date, self.calendar.session_close_at(analysis_date))
        if lag == 0:
            return DataStatus.FRESH
        if lag == 1:
            return DataStatus.STALE_ONE_SESSION
        return DataStatus.STALE

    @staticmethod
    def _snapshot_capability(series: _SeriesData, *, required: bool) -> SnapshotCapability:
        binding = _quality_binding(series)
        return SnapshotCapability(
            capability=series.capability,
            subject=series.subject,
            required=required,
            quality_status=DataQualityStatus(series.record.quality_status),
            executable=series.record.quality_status
            in {HubQualityStatus.VERIFIED.value, HubQualityStatus.SINGLE_SOURCE.value},
            quality_record_id=series.record.id,
            observed_at=binding.observed_at,
            fetched_at=_aware_record_time(series.record, series.record.fetched_at),
            normalized_digest=series.record.normalized_digest,
            rows=tuple(_rows(series.bars, through=series.bars[-1].trade_date)),
            evidence_refs=(f"quality:{series.record.id}",),
        )

    def analyze(self, request: SelectedStockAnalysisRequest) -> SelectedStockAnalysisResult:
        analysis_started_at = self._now()
        latest_completed = self.calendar.latest_completed_session(analysis_started_at)
        analysis_date = request.analysis_date or latest_completed
        if analysis_date > latest_completed or not self.calendar.is_session(analysis_date):
            raise SelectedStockAnalysisError(
                "INVALID_ANALYSIS_DATE",
                "analysis_date must be a completed A-share trading session",
            )
        if analysis_date < latest_completed and not self._router_injected:
            frozen_market_time = self.calendar.session_close_at(analysis_date)
            self.router = build_selected_stock_data_hub(
                self.db,
                calendar=self.calendar,
                now_fn=lambda: frozen_market_time,
            )
        start = analysis_date - timedelta(days=550)

        with self.db.begin_nested():
            stock = self._stock_series(request.stock_code, start, analysis_date)
            benchmark = self._benchmark_series(start, analysis_date)
            industry_name, profile_lineage = self._company_profile(request.stock_code)
            industry = self._industry_series(industry_name, start, analysis_date)

            stock_rows = _rows(stock.bars, through=analysis_date)
            benchmark_rows = (
                _rows(benchmark.bars, through=analysis_date) if benchmark else []
            )
            industry_rows = _rows(industry.bars, through=analysis_date) if industry else None
            industry_context = self.industry_context_provider.resolve(
                symbol=request.stock_code,
                industry_name=industry_name,
                analysis_date=analysis_date,
                industry_rows=industry_rows,
                profile_lineage=profile_lineage,
            )
            indicators = calculate_indicators(
                stock_rows,
                benchmark_rows=benchmark_rows,
                industry_rows=industry_rows,
            )
            market_status, market_evidence = self._market_context(analysis_date)
            indicators["market_context"] = market_evidence
            market_price_observed_at = self.calendar.session_close_at(
                stock_rows[-1]["trade_date"]
            )
            account_context = self._account_context(
                request,
                trusted_price=stock_rows[-1]["close"],
                analysis_date=analysis_date,
                observed_at=analysis_started_at,
            )

            series = tuple(
                item for item in (stock, benchmark, industry) if item is not None
            )
            quality_bindings = tuple(_quality_binding(item) for item in series)
            lineage = tuple(_lineage(item) for item in series)
            if profile_lineage is not None:
                lineage += (profile_lineage,)

            request_snapshot = request.model_dump(mode="json")
            request_hash = canonical_hash(request_snapshot)
            identity_hash = canonical_hash(
                {
                    "request_hash": request_hash,
                    "analysis_date": analysis_date,
                    "strategy_id": STRATEGY_ID,
                    "strategy_version": STRATEGY_VERSION,
                    "quality_bindings": [
                        item.model_dump(mode="json") for item in quality_bindings
                    ],
                    "account_context": account_context.model_dump(mode="json")
                    if account_context is not None
                    else None,
                    "industry_context": industry_context.model_dump(mode="json"),
                }
            )
            existing = self.db.scalar(
                select(SelectedStockAnalysisRun).where(
                    SelectedStockAnalysisRun.analysis_identity_hash == identity_hash
                )
            )
            if existing is not None:
                return SelectedStockAnalysisResult.model_validate(existing.result_snapshot)

            capabilities = [self._snapshot_capability(stock, required=True)]
            if benchmark is not None:
                capabilities.append(self._snapshot_capability(benchmark, required=True))
            if industry is not None:
                capabilities.append(self._snapshot_capability(industry, required=False))
            snapshot = ProductAnalysisSnapshot(
                analysis_started_at=analysis_started_at,
                symbol=request.stock_code,
                capabilities=tuple(capabilities),
            )
            registry = get_strategy_registry()
            strategy = registry.get(STRATEGY_ID, STRATEGY_VERSION)
            if not isinstance(strategy, CycleStructureValidationStrategyV2):
                raise SelectedStockAnalysisError(
                    "STRATEGY_CONTRACT_MISMATCH",
                    "registered CSV_V2 implementation is incompatible",
                )
            signal = strategy.evaluate(snapshot, {})
            indicators["strategy_signal_hash"] = signal.signal_hash
            product_v1 = registry.get("core.discipline", "1.0.0")
            product_v1_manifest = product_v1.manifest()
            product_v1_parameters = product_v1.validate_parameters(
                product_v1_manifest.default_parameters
            )
            product_v1_signal = product_v1.evaluate(snapshot, product_v1_parameters)
            product_v1_status = product_v1_signal.signal_type
            if product_v1_signal.blocked_reasons:
                product_v1_status += ":" + ",".join(product_v1_signal.blocked_reasons)
            indicators["product_v1_shadow_signal_hash"] = product_v1_signal.signal_hash
            result = strategy.build_plan(
                request=request,
                generated_at=analysis_started_at,
                analysis_date=analysis_date,
                data_status=self._data_status(stock, benchmark, analysis_date),
                market_status=market_status,
                industry_status=industry_context.status,
                industry_name=industry_name,
                stock_row_count=len(stock_rows),
                indicators=indicators,
                quality_bindings=quality_bindings,
                source_lineage=lineage,
                snapshot_hash=snapshot.snapshot_hash,
                product_v1_status=product_v1_status,
                market_price_observed_at=market_price_observed_at,
                account_context=account_context,
                industry_context=industry_context,
            )
            stored_result = result.model_dump(mode="json")
            run = SelectedStockAnalysisRun(
                symbol=request.stock_code,
                analysis_date=analysis_date,
                strategy_id=STRATEGY_ID,
                strategy_version=STRATEGY_VERSION,
                strategy_mode=request.strategy_mode.value,
                status=result.plan_status.value,
                request_hash=request_hash,
                snapshot_hash=result.snapshot_hash,
                result_digest=result.result_digest,
                analysis_identity_hash=identity_hash,
                request_snapshot=request_snapshot,
                result_snapshot=stored_result,
                quality_bindings=[item.model_dump(mode="json") for item in quality_bindings],
                source_lineage=[item.model_dump(mode="json") for item in lineage],
                generated_at=to_utc_storage_naive(analysis_started_at),
                created_at=to_utc_storage_naive(analysis_started_at),
            )
            self.db.add(run)
            self.db.flush()
            final = result.model_copy(update={"analysis_run_id": run.id})
            run.result_snapshot = final.model_dump(mode="json")
        self.db.commit()
        return final


__all__ = [
    "SelectedStockAnalysisError",
    "SelectedStockAnalysisService",
]
