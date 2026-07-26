from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.data_hub.market_subjects import (
    industry_capital_flow_subject,
    industry_universe_subject,
    index_daily_subject,
    market_event_pool_subject,
    stock_daily_subject,
    stock_turnover_subject,
)
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    market_storage_naive_to_aware,
    shanghai_now,
    to_shanghai_aware,
    to_utc_storage_naive,
)
from app.discovery.algorithm import discover_candidates
from app.discovery.contracts import (
    CandidateStockInput,
    DiscoveryConfig,
    DiscoverySnapshot,
    IndustryDiscoveryInput,
    MarketDiscoveryInput,
    PriceHistoryPoint,
)
from app.discovery.data import (
    persist_discovery_result,
    resolve_discovery_cache,
)
from app.errors import AppError
from app.domain.quality import DataQualityStatus, worst_quality
from app.models import (
    CandidateDiscoveryRun,
    CandidateIndustryAssessment,
    DiscoveryCandidate,
    IndustryAnalysisSnapshot,
    IndustryConstituentSnapshot,
    IndustryMarketSnapshot,
    MarketDailyBar,
    MarketTurnoverSnapshot,
    WatchlistItem,
)
from app.services.product_data import persist_product_result, resolve_product_cache
from app.watchlist.contracts import WatchlistCreateRequest, WatchlistSourceType
from app.watchlist.context import latest_industry_state, latest_market_state
from app.watchlist.events import create_reanalysis_request_once
from app.watchlist.reanalysis import execute_reanalysis
from app.watchlist.service import create_watchlist_item, serialize_item


_TRUSTED = {"VERIFIED", "SINGLE_SOURCE"}


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(child) for child in value]
    return value


def _worst_quality(*statuses: str) -> str:
    return worst_quality([DataQualityStatus(item) for item in statuses]).value


class CandidateDiscoveryService:
    def __init__(
        self,
        db: Session,
        *,
        router: DataHubRouter | None = None,
        settings: Settings | None = None,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.calendar = calendar or get_trading_calendar()
        if router is None:
            from app.composition.data_hub import build_data_hub

            router = build_data_hub(db, calendar=self.calendar)
        self.router = router

    def config(self) -> DiscoveryConfig:
        return DiscoveryConfig(
            max_industries=self.settings.candidate_discovery_max_industries,
            max_candidates_per_industry=(
                self.settings.candidate_discovery_max_per_industry
            ),
            max_candidates=self.settings.candidate_discovery_max_candidates,
        )

    def _refresh_discovery_capability(self, capability: str, day, now):
        subject = {
            "market.industry.capital_flow": industry_capital_flow_subject(),
            "market.limit_up_pool": market_event_pool_subject("limit-up"),
            "market.broken_limit_pool": market_event_pool_subject("broken-limit"),
        }[capability]
        selected = resolve_discovery_cache(
            self.db,
            capability=capability,
            subject=subject,
            evaluated_at=now,
        )
        if selected.executable:
            return selected
        operation = {
            "market.industry.capital_flow": self.router.get_industry_capital_flow,
            "market.limit_up_pool": self.router.get_limit_up_pool,
            "market.broken_limit_pool": self.router.get_broken_limit_pool,
        }[capability]
        result = operation(day)
        if result.quality_status.value in _TRUSTED:
            persist_discovery_result(self.db, self.router, result)
        return resolve_discovery_cache(
            self.db,
            capability=capability,
            subject=subject,
            evaluated_at=now,
        )

    def _refresh_product_universe(self, *, constituents: bool, day, now):
        capability = (
            "market.industry.constituents" if constituents else "market.industry.daily"
        )
        subject = industry_universe_subject(constituents=constituents)
        selected = resolve_product_cache(
            self.db,
            capability=capability,
            subject=subject,
            evaluated_at=now,
        )
        if selected.executable:
            return selected
        result = (
            self.router.get_industry_constituents_universe()
            if constituents
            else self.router.get_industry_universe(day - timedelta(days=120), day)
        )
        if result.quality_status.value in _TRUSTED:
            persist_product_result(self.db, self.router, result)
        return resolve_product_cache(
            self.db,
            capability=capability,
            subject=subject,
            evaluated_at=now,
        )

    def _stock_input(
        self,
        member: IndustryConstituentSnapshot,
        *,
        limit_up_symbols: set[str],
        now: datetime,
    ) -> CandidateStockInput | None:
        daily_subject = stock_daily_subject(member.symbol, "qfq", "CNY", "share")
        daily_records = self.db.scalars(
            select(MarketDailyBar)
            .where(
                MarketDailyBar.symbol == member.symbol,
                MarketDailyBar.adjustment == "qfq",
                MarketDailyBar.price_unit == "CNY",
                MarketDailyBar.volume_unit == "share",
            )
            .order_by(MarketDailyBar.trade_date.desc())
            .limit(80)
        ).all()
        turnover = self.db.scalars(
            select(MarketTurnoverSnapshot)
            .where(MarketTurnoverSnapshot.symbol == member.symbol)
            .order_by(MarketTurnoverSnapshot.trade_date.desc())
            .limit(80)
        ).all()
        if len(daily_records) < 50 or len(turnover) < 50:
            return None
        daily_quality_ids = {row.quality_record_id for row in daily_records}
        turnover_quality_ids = {row.quality_record_id for row in turnover}
        if len(daily_quality_ids) != 1 or len(turnover_quality_ids) != 1:
            return None
        from app.data_hub.effective_quality import resolve_effective_quality

        daily_observed = market_storage_naive_to_aware(
            max(row.observed_at for row in daily_records)
        )
        turnover_observed = market_storage_naive_to_aware(
            max(row.observed_at for row in turnover)
        )
        daily_quality = resolve_effective_quality(
            self.db,
            capability="market.daily.qfq",
            subject=daily_subject,
            persisted_quality_record_id=next(iter(daily_quality_ids)),
            observed_at=daily_observed,
            evaluated_at=now,
        )
        turnover_quality = resolve_effective_quality(
            self.db,
            capability="market.turnover.daily",
            subject=stock_turnover_subject(member.symbol),
            persisted_quality_record_id=next(iter(turnover_quality_ids)),
            observed_at=turnover_observed,
            evaluated_at=now,
        )
        if not daily_quality.executable or not turnover_quality.executable:
            return None
        turnover_by_date = {row.trade_date: row for row in turnover}
        points = []
        for row in sorted(daily_records, key=lambda item: item.trade_date):
            turnover_row = turnover_by_date.get(row.trade_date)
            if turnover_row is None or turnover_row.amount is None:
                continue
            points.append(
                PriceHistoryPoint(
                    trade_date=row.trade_date,
                    close=row.close,
                    amount=turnover_row.amount,
                    turnover_rate=turnover_row.turnover_rate,
                )
            )
        if len(points) < 50:
            return None
        quality_status = _worst_quality(
            daily_quality.effective_quality.value,
            turnover_quality.effective_quality.value,
        )
        upper_name = member.name.upper().replace(" ", "")
        return CandidateStockInput(
            symbol=member.symbol,
            name=member.name,
            industry_key=member.industry_key,
            industry_name=member.industry_name,
            prices=tuple(points),
            is_st=upper_name.startswith("ST") or upper_name.startswith("*ST"),
            suspended=False,
            limit_up=member.symbol in limit_up_symbols,
            delisting="退" in member.name,
            quality_status=quality_status,
            evidence_references=(
                f"quality:{next(iter(daily_quality_ids))}",
                f"quality:{next(iter(turnover_quality_ids))}",
            ),
        )

    def build_snapshot(self, *, now: datetime) -> DiscoverySnapshot:
        current = to_shanghai_aware(now, naive_is_shanghai=now.tzinfo is None)
        day = self.calendar.latest_completed_session(current)
        monitored_market = latest_market_state(self.db, evaluated_at=current)
        market_quality = monitored_market.quality_status.value
        market = MarketDiscoveryInput(
            trade_date=day,
            state=monitored_market.value or "CONTRACTION",
            quality_status=market_quality,
            evidence_references=tuple(
                sorted(
                    set(monitored_market.evidence_references)
                    | (
                        {f"product-snapshot:{monitored_market.snapshot_hash}"}
                        if monitored_market.snapshot_hash
                        else set()
                    )
                )
            ),
        )
        if market_quality not in _TRUSTED:
            return DiscoverySnapshot(market=market, industries=(), observed_at=current)

        daily = self._refresh_product_universe(constituents=False, day=day, now=current)
        constituents = self._refresh_product_universe(
            constituents=True, day=day, now=current
        )
        capital = self._refresh_discovery_capability(
            "market.industry.capital_flow", day, current
        )
        limit_pool = self._refresh_discovery_capability(
            "market.limit_up_pool", day, current
        )
        broken_pool = self._refresh_discovery_capability(
            "market.broken_limit_pool", day, current
        )
        if not all(
            selection.executable
            for selection in (daily, constituents, capital, limit_pool, broken_pool)
        ):
            blocked_qualities = [
                selection.effective_quality.effective_quality.value
                for selection in (daily, constituents, capital, limit_pool, broken_pool)
                if not selection.executable
            ]
            blocking_quality = (
                _worst_quality(
                    *(item for item in blocked_qualities if item not in _TRUSTED)
                )
                if any(item not in _TRUSTED for item in blocked_qualities)
                else "MISSING"
            )
            return DiscoverySnapshot(
                market=market.model_copy(update={"quality_status": blocking_quality}),
                industries=(),
                observed_at=current,
            )

        benchmark_rows = self.db.scalars(
            select(MarketDailyBar)
            .where(
                MarketDailyBar.symbol == "CSI000300",
                MarketDailyBar.adjustment == "unadjusted",
                MarketDailyBar.price_unit == "CNY",
                MarketDailyBar.volume_unit == "share",
            )
            .order_by(MarketDailyBar.trade_date.desc())
            .limit(80)
        ).all()
        benchmark_quality_ids = {row.quality_record_id for row in benchmark_rows}
        if len(benchmark_rows) < 21 or len(benchmark_quality_ids) != 1:
            return DiscoverySnapshot(
                market=market.model_copy(update={"quality_status": "MISSING"}),
                industries=(),
                observed_at=current,
            )
        from app.data_hub.effective_quality import resolve_effective_quality

        benchmark_quality = resolve_effective_quality(
            self.db,
            capability="market.index_daily",
            subject=index_daily_subject("CSI000300", "unadjusted", "CNY", "share"),
            persisted_quality_record_id=next(iter(benchmark_quality_ids)),
            observed_at=market_storage_naive_to_aware(
                max(row.observed_at for row in benchmark_rows)
            ),
            evaluated_at=current,
        )
        if not benchmark_quality.executable:
            return DiscoverySnapshot(
                market=market.model_copy(
                    update={"quality_status": benchmark_quality.effective_quality.value}
                ),
                industries=(),
                observed_at=current,
            )
        benchmark_closes = [
            row.close
            for row in sorted(benchmark_rows, key=lambda item: item.trade_date)
        ]
        benchmark_changes = [
            (current_close / previous_close - Decimal("1")) * Decimal("100")
            for previous_close, current_close in zip(
                benchmark_closes, benchmark_closes[1:]
            )
            if previous_close != 0
        ]

        capital_by_name = {row.industry_name: row for row in capital.rows}
        limit_symbols = {row.symbol for row in limit_pool.rows}
        broken_by_industry: dict[str, int] = {}
        limit_by_industry: dict[str, int] = {}
        for row in broken_pool.rows:
            if row.industry_name:
                broken_by_industry[row.industry_name] = (
                    broken_by_industry.get(row.industry_name, 0) + 1
                )
        for row in limit_pool.rows:
            if row.industry_name:
                limit_by_industry[row.industry_name] = (
                    limit_by_industry.get(row.industry_name, 0) + 1
                )
        latest_assessments = self.db.scalars(
            select(IndustryAnalysisSnapshot)
            .where(IndustryAnalysisSnapshot.trade_date == day)
            .order_by(IndustryAnalysisSnapshot.industry_name)
        ).all()
        assessment_by_name = {row.industry_name: row for row in latest_assessments}
        daily_by_name: dict[str, list[IndustryMarketSnapshot]] = {}
        for row in daily.rows:
            daily_by_name.setdefault(row.industry_name, []).append(row)
        members_by_name: dict[str, list[IndustryConstituentSnapshot]] = {}
        for row in constituents.rows:
            members_by_name.setdefault(row.industry_name, []).append(row)
        industries = []
        for name in sorted(set(daily_by_name) & set(members_by_name)):
            flow = capital_by_name.get(name)
            assessment = assessment_by_name.get(name)
            monitored_industry = latest_industry_state(
                self.db,
                industry_name=name,
                evaluated_at=current,
            )
            history = sorted(daily_by_name[name], key=lambda row: row.trade_date)
            latest = history[-1]
            if flow is None or assessment is None:
                quality = "MISSING"
            elif not monitored_industry.executable:
                quality = monitored_industry.quality_status.value
            else:
                quality = _worst_quality(
                    monitored_industry.quality_status.value,
                    daily.effective_quality.effective_quality.value,
                    constituents.effective_quality.effective_quality.value,
                    capital.effective_quality.effective_quality.value,
                    limit_pool.effective_quality.effective_quality.value,
                    broken_pool.effective_quality.effective_quality.value,
                    benchmark_quality.effective_quality.value,
                )
            selected_members = sorted(
                members_by_name[name],
                key=lambda row: (
                    -(row.change_pct or Decimal("-999")),
                    row.symbol,
                ),
            )[: self.settings.candidate_discovery_max_per_industry * 3]
            stock_inputs = tuple(
                stock
                for member in selected_members
                if (
                    stock := self._stock_input(
                        member,
                        limit_up_symbols=limit_symbols,
                        now=current,
                    )
                )
                is not None
            )
            def relative_strength(count: int) -> Decimal:
                industry_return = sum(
                    (row.change_pct or Decimal("0") for row in history[-count:]),
                    Decimal("0"),
                )
                benchmark_return = sum(
                    benchmark_changes[-count:],
                    Decimal("0"),
                )
                return industry_return - benchmark_return
            total_events = limit_by_industry.get(name, 0) + broken_by_industry.get(name, 0)
            broken_rate = (
                Decimal(broken_by_industry.get(name, 0)) / Decimal(total_events)
                if total_events
                else Decimal("0")
            )
            industries.append(
                IndustryDiscoveryInput(
                    industry_key=latest.industry_key,
                    industry_name=name,
                    classification=(monitored_industry.value or "NONE"),
                    relative_strength_5d=relative_strength(5),
                    relative_strength_10d=relative_strength(10),
                    relative_strength_20d=relative_strength(20),
                    amount_share=latest.amount_share,
                    advance_ratio=latest.advance_ratio,
                    limit_up_count=latest.limit_up_count,
                    leader_strength=latest.leader_strength,
                    new_high_ratio=latest.new_high_ratio,
                    net_inflow_1d=flow.net_inflow_1d if flow else None,
                    net_inflow_5d=flow.net_inflow_5d if flow else None,
                    net_inflow_10d=flow.net_inflow_10d if flow else None,
                    broken_limit_rate=broken_rate,
                    quality_status=(quality if quality in _TRUSTED else "MISSING"),
                    evidence_references=tuple(
                        sorted(
                            {
                                f"quality:{daily.quality_record_id}",
                                f"quality:{constituents.quality_record_id}",
                                f"quality:{capital.quality_record_id}",
                                f"quality:{limit_pool.quality_record_id}",
                                f"quality:{broken_pool.quality_record_id}",
                                f"quality:{next(iter(benchmark_quality_ids))}",
                                f"industry-analysis:{assessment.id}"
                                if assessment
                                else "industry-analysis:missing",
                                *monitored_industry.evidence_references,
                            }
                        )
                    ),
                    constituents=stock_inputs,
                )
            )
        return DiscoverySnapshot(
            market=market,
            industries=tuple(industries),
            observed_at=current,
        )

    def run(
        self,
        *,
        now: datetime | None = None,
        snapshot: DiscoverySnapshot | None = None,
    ) -> CandidateDiscoveryRun:
        current = to_shanghai_aware(
            now or shanghai_now(),
            naive_is_shanghai=(now is not None and now.tzinfo is None),
        )
        config = self.config()
        day = (
            snapshot.market.trade_date
            if snapshot is not None
            else self.calendar.latest_completed_session(current)
        )
        existing = self.db.scalar(
            select(CandidateDiscoveryRun).where(
                CandidateDiscoveryRun.market == "CN-A",
                CandidateDiscoveryRun.trade_date == day,
                CandidateDiscoveryRun.algorithm_version == config.algorithm_version,
                CandidateDiscoveryRun.config_hash == config.config_hash(),
            )
        )
        if existing:
            return existing
        stored_now = to_utc_storage_naive(current)
        run = CandidateDiscoveryRun(
            market="CN-A",
            trade_date=day,
            status="RUNNING",
            algorithm_id=config.algorithm_id,
            algorithm_version=config.algorithm_version,
            config_hash=config.config_hash(),
            input_snapshot_hash=None,
            market_state=None,
            quality_status="MISSING",
            blocked_reasons=[],
            quality_bindings=[],
            industries_evaluated=0,
            candidates_generated=0,
            started_at=stored_now,
            completed_at=None,
            created_at=stored_now,
        )
        try:
            with self.db.begin_nested():
                self.db.add(run)
                self.db.flush()
        except IntegrityError:
            existing = self.db.scalar(
                select(CandidateDiscoveryRun).where(
                    CandidateDiscoveryRun.market == "CN-A",
                    CandidateDiscoveryRun.trade_date == day,
                    CandidateDiscoveryRun.algorithm_version == config.algorithm_version,
                    CandidateDiscoveryRun.config_hash == config.config_hash(),
                )
            )
            if existing is None:
                raise
            return existing
        try:
            effective_snapshot = snapshot or self.build_snapshot(now=current)
            result = discover_candidates(effective_snapshot, config)
            run.status = result.status
            run.input_snapshot_hash = result.input_snapshot_hash
            run.market_state = result.market_state
            run.quality_status = result.quality_status
            run.blocked_reasons = list(result.blocked_reasons)
            run.quality_bindings = sorted(
                {
                    *effective_snapshot.market.evidence_references,
                    *(
                        reference
                        for industry in effective_snapshot.industries
                        for reference in industry.evidence_references
                    ),
                    *(
                        reference
                        for industry in effective_snapshot.industries
                        for stock in industry.constituents
                        for reference in stock.evidence_references
                    ),
                }
            )
            run.industries_evaluated = len(result.industries)
            run.candidates_generated = len(result.candidates)
            run.completed_at = to_utc_storage_naive(current)
            for item in result.industries:
                self.db.add(
                    CandidateIndustryAssessment(
                        discovery_run_id=run.id,
                        industry_key=item.industry_key,
                        industry_name=item.industry_name,
                        classification=item.classification,
                        score=item.score,
                        rank=item.rank,
                        metrics=_json(item.metrics),
                        reason_codes=list(item.reason_codes),
                        evidence_references=list(item.evidence_references),
                        quality_status=item.quality_status,
                    )
                )
            expires_at = to_utc_storage_naive(
                current + timedelta(seconds=self.settings.candidate_max_age_seconds)
            )
            for item in result.candidates:
                self.db.add(
                    DiscoveryCandidate(
                        discovery_run_id=run.id,
                        symbol=item.symbol,
                        name=item.name,
                        industry_key=item.industry_key,
                        industry_name=item.industry_name,
                        candidate_type=item.candidate_type,
                        score=item.score,
                        rank=item.rank,
                        status=item.status,
                        current_price=item.current_price,
                        technical_metrics=_json(item.technical_metrics),
                        reason_codes=list(item.reason_codes),
                        risk_flags=list(item.risk_flags),
                        evidence_references=list(item.evidence_references),
                        quality_status=item.quality_status,
                        snapshot_hash=item.snapshot_hash,
                        expires_at=expires_at,
                        promoted_watchlist_item_id=None,
                        created_at=stored_now,
                        updated_at=stored_now,
                    )
                )
            self.db.commit()
            self.db.refresh(run)
            return run
        except Exception as exc:
            self.db.rollback()
            failed = self.db.get(CandidateDiscoveryRun, run.id)
            if failed is None:
                raise
            failed.status = "BLOCKED"
            failed.quality_status = "MISSING"
            failed.blocked_reasons = [f"DISCOVERY_INPUT_FAILED:{type(exc).__name__}"]
            failed.completed_at = to_utc_storage_naive(current)
            self.db.commit()
            return failed


def expire_candidate(candidate: DiscoveryCandidate, *, now: datetime) -> bool:
    current = to_utc_storage_naive(now.astimezone(timezone.utc))
    if candidate.status in {"NEW", "REVIEWED"} and candidate.expires_at < current:
        candidate.status = "EXPIRED"
        candidate.updated_at = current
        return True
    return candidate.status == "EXPIRED"


def promote_candidate(
    db: Session,
    candidate_id: int,
    *,
    thesis: str,
    analysis_capital: Decimal,
    waiting_conditions: list[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("promotion time must be timezone-aware")
    candidate = db.scalar(
        select(DiscoveryCandidate)
        .where(DiscoveryCandidate.id == candidate_id)
        .with_for_update()
    )
    if candidate is None:
        raise AppError(404, "DISCOVERY_CANDIDATE_NOT_FOUND", "候选不存在")
    if expire_candidate(candidate, now=current):
        db.commit()
        raise AppError(409, "DISCOVERY_CANDIDATE_EXPIRED", "候选已过期，请重新发现")
    if candidate.status in {"REJECTED"}:
        raise AppError(409, "DISCOVERY_CANDIDATE_INACTIVE", "候选不可提升")
    if candidate.status == "PROMOTED":
        existing = (
            db.get(WatchlistItem, candidate.promoted_watchlist_item_id)
            if candidate.promoted_watchlist_item_id is not None
            else None
        )
        if existing is None:
            raise AppError(
                409,
                "DISCOVERY_PROMOTION_INCONSISTENT",
                "候选提升记录不完整，请联系管理员",
            )
        return {
            "candidate_id": candidate.id,
            "watchlist_item": serialize_item(db, existing),
            "reanalysis_request_id": None,
            "reanalysis_run_id": None,
            "idempotent": True,
        }
    source_reference = str(candidate.id)
    existing = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.source_type == WatchlistSourceType.CANDIDATE_DISCOVERY.value,
            WatchlistItem.source_reference == source_reference,
        )
    )
    if existing is None:
        duplicate = db.scalar(
            select(WatchlistItem).where(
                WatchlistItem.symbol == candidate.symbol,
                WatchlistItem.status.not_in(["INVALIDATED", "ARCHIVED"]),
            )
        )
        if duplicate is not None:
            raise AppError(409, "ACTIVE_WATCHLIST_ITEM_EXISTS", "该股票已有活跃观察项")
        try:
            existing = create_watchlist_item(
                db,
                WatchlistCreateRequest(
                    source_type=WatchlistSourceType.CANDIDATE_DISCOVERY,
                    symbol=candidate.symbol,
                    thesis=thesis,
                    analysis_capital=analysis_capital,
                    waiting_conditions=waiting_conditions,
                ),
                now=to_utc_storage_naive(current),
                source_reference_override=source_reference,
            )
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(WatchlistItem).where(
                    WatchlistItem.source_type
                    == WatchlistSourceType.CANDIDATE_DISCOVERY.value,
                    WatchlistItem.source_reference == source_reference,
                )
            )
            if existing is None:
                raise
    candidate = db.get(DiscoveryCandidate, candidate_id)
    candidate.status = "PROMOTED"
    candidate.promoted_watchlist_item_id = existing.id
    candidate.updated_at = to_utc_storage_naive(current)
    request, created = create_reanalysis_request_once(
        db,
        existing,
        reason_codes=["CANDIDATE_DISCOVERY_PROMOTED"],
        observed_at=current,
        cooldown_seconds=1,
    )
    db.commit()
    run = execute_reanalysis(db, request.id) if created else None
    return {
        "candidate_id": candidate.id,
        "watchlist_item": serialize_item(db, existing),
        "reanalysis_request_id": request.id,
        "reanalysis_run_id": run.id if run is not None else None,
        "idempotent": not created,
    }


__all__ = [
    "CandidateDiscoveryService",
    "expire_candidate",
    "promote_candidate",
]
