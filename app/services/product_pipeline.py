from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import (
    ConceptChainContext,
    ConceptChainInput,
    IndustryAnalysisInput,
    IndustryContext,
    MarketRegime,
    MarketRegimeInput,
    ProductAnalysisSnapshot,
    SnapshotCapability,
    TechnicalAnalysisInput,
    TechnicalContext,
    analyze_concept_chain,
    analyze_industry_mainlines,
    analyze_intraday_turnover,
    analyze_market_regime,
    infer_market_state_without_history,
)
from app.analysis.contracts import (
    BreadthMetrics,
    ChainPosition,
    ConceptMapping,
    IndustryObservation,
    IndustrySeries,
    PriceBar,
    TurnoverPoint,
)
from app.data_hub.market_subjects import (
    company_concepts_subject,
    company_industry_chain_subject,
    industry_universe_subject,
    market_amount_subject,
    market_breadth_subject,
    stock_intraday_subject,
    stock_turnover_subject,
)
from app.data_hub.router import DataHubRouter, ProviderResult
from app.data_hub.trading_calendar import (
    get_trading_calendar,
    market_storage_naive_to_aware,
    to_market_storage_naive,
    to_shanghai_aware,
    utc_storage_naive_to_aware,
)
from app.decision import BaseRulePlan, TradeDecisionContext, TradeDecisionResult
from app.decision.strategy_adapter import (
    candidates_from_signals,
    combine_strategy_candidates,
)
from app.composition.strategies import get_strategy_registry
from app.domain.hashing import canonical_hash
from app.domain.models import Evidence, MarketQualityBinding, StrategyBinding
from app.domain.quality import DataQualityStatus, worst_quality
from app.domain.quality_subject import SubjectRef
from app.models import (
    CompanyChainPosition,
    DataQualityRecord,
    IndustryAnalysisSnapshot,
    IndustryChain,
    IndustryChainNode,
    MarketDailyBar,
    MarketRegimeSnapshot,
)
from app.services.product_data import (
    ProductCacheSelection,
    persist_product_result,
    resolve_product_cache,
)
from app.strategies.contracts import StrategySignal


logger = logging.getLogger(__name__)


class ProductPipelineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RequiredDataAction(str, Enum):
    REFRESH = "REFRESH"
    USE_CACHE = "USE_CACHE"
    OPTIONAL = "OPTIONAL"
    BLOCK = "BLOCK"


class ProductDataState(ProductPipelineModel):
    capability: str = Field(min_length=1, max_length=80)
    subject: SubjectRef
    required: bool
    quality_status: DataQualityStatus
    executable: bool
    cache_used: bool = False
    fallback_used: bool = False
    quality_record_id: int | None = Field(default=None, ge=1)


class RequiredDataItem(ProductPipelineModel):
    capability: str = Field(min_length=1, max_length=80)
    subject: SubjectRef
    required: bool
    action: RequiredDataAction
    quality_status: DataQualityStatus
    executable: bool
    cache_used: bool
    fallback_used: bool
    reason: str = Field(min_length=1, max_length=500)


class RequiredDataPlan(ProductPipelineModel):
    analysis_started_at: datetime
    symbol: str = Field(pattern=r"^\d{6}$")
    items: tuple[RequiredDataItem, ...]
    plan_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_scopes_and_hash(self):
        if self.analysis_started_at.tzinfo is None:
            raise ValueError("analysis_started_at must be timezone-aware")
        scopes = [
            (
                item.capability,
                item.subject.subject_type,
                item.subject.subject_id,
                item.subject.semantic_key or "",
            )
            for item in self.items
        ]
        if len(scopes) != len(set(scopes)):
            raise ValueError("duplicate required data scope")
        expected = canonical_hash(self.model_dump(mode="python", exclude={"plan_hash"}))
        if self.plan_hash is not None and self.plan_hash != expected:
            raise ValueError("plan_hash does not match required data plan")
        object.__setattr__(self, "plan_hash", expected)
        return self


class ProductPipelineResult(ProductPipelineModel):
    required_data: RequiredDataPlan
    snapshot: ProductAnalysisSnapshot
    technical_context: TechnicalContext
    market_regime: MarketRegime
    industry_context: IndustryContext
    concept_chain_context: ConceptChainContext
    trade_decision: TradeDecisionResult
    evidence: tuple[Evidence, ...]
    strategy_bindings: tuple[StrategyBinding, ...]
    strategy_signals: tuple[StrategySignal, ...]
    data_completeness: Decimal = Field(ge=0, le=1)
    breadth_universe: dict[str, Any] | None = None


def build_required_data_plan(
    *,
    symbol: str,
    analysis_started_at: datetime,
    states: tuple[ProductDataState, ...],
    force_refresh: bool,
    refresh_attempted: bool = False,
) -> RequiredDataPlan:
    items = []
    for state in states:
        if state.executable and not force_refresh:
            action = RequiredDataAction.USE_CACHE
            reason = "effective quality permits the exact persisted cache"
        elif not refresh_attempted:
            action = RequiredDataAction.REFRESH
            reason = (
                "forced refresh bypasses the executable cache"
                if force_refresh and state.executable
                else "current effective quality requires an automatic refresh"
            )
        elif state.required:
            action = RequiredDataAction.BLOCK
            reason = "required data is not executable after refresh"
        else:
            action = RequiredDataAction.OPTIONAL
            reason = "optional data is unavailable"
        items.append(
            RequiredDataItem(
                capability=state.capability,
                subject=state.subject,
                required=state.required,
                action=action,
                quality_status=state.quality_status,
                executable=state.executable,
                cache_used=state.cache_used,
                fallback_used=state.fallback_used,
                reason=reason,
            )
        )
    return RequiredDataPlan(
        analysis_started_at=analysis_started_at,
        symbol=symbol,
        items=tuple(items),
    )


PRODUCT_REQUIRED_CAPABILITIES = frozenset(
    {
        "market.intraday.60m",
        "market.turnover.daily",
        "market.breadth.daily",
        "market.amount.daily",
        "market.industry.daily",
        "market.industry.constituents",
    }
)


def _product_subjects(symbol: str, industry: str | None) -> tuple[tuple[str, SubjectRef], ...]:
    del industry
    rows: list[tuple[str, SubjectRef]] = [
        ("market.intraday.60m", stock_intraday_subject(symbol)),
        ("market.turnover.daily", stock_turnover_subject(symbol)),
        ("market.breadth.daily", market_breadth_subject()),
        ("market.amount.daily", market_amount_subject()),
        ("company.concepts", company_concepts_subject(symbol)),
        ("company.industry_chain", company_industry_chain_subject(symbol)),
    ]
    rows.extend(
        [
            (
                "market.industry.daily",
                industry_universe_subject(),
            ),
            (
                "market.industry.constituents",
                industry_universe_subject(constituents=True),
            ),
        ]
    )
    return tuple(rows)


def _state_from_selection(
    capability: str,
    selection: ProductCacheSelection,
) -> ProductDataState:
    return ProductDataState(
        capability=capability,
        subject=selection.subject,
        required=capability in PRODUCT_REQUIRED_CAPABILITIES,
        quality_status=selection.effective_quality.effective_quality,
        executable=selection.executable,
        cache_used=bool(selection.rows),
        fallback_used=False,
        quality_record_id=selection.quality_record_id,
    )


def select_product_data(
    db: Session,
    *,
    symbol: str,
    industry: str | None,
    evaluated_at: datetime,
) -> dict[str, ProductCacheSelection]:
    return {
        capability: resolve_product_cache(
            db,
            capability=capability,
            subject=subject,
            evaluated_at=evaluated_at,
        )
        for capability, subject in _product_subjects(symbol, industry)
    }


def has_product_cache(db: Session, symbol: str) -> bool:
    return (
        db.scalar(
            select(DataQualityRecord.id)
            .where(
                DataQualityRecord.persisted.is_(True),
                DataQualityRecord.subject_type == "stock",
                DataQualityRecord.subject_id == symbol,
                DataQualityRecord.capability.in_(
                    (
                        "market.intraday.60m",
                        "market.turnover.daily",
                        "company.concepts",
                        "company.industry_chain",
                    )
                ),
            )
            .limit(1)
        )
        is not None
    )


def router_supports_product_data(router: DataHubRouter) -> bool:
    return all(
        any(
            provider.metadata.enabled and provider.configured
            for provider in router.registry.providers_for(capability)
        )
        for capability in PRODUCT_REQUIRED_CAPABILITIES
    )


def should_run_product_pipeline(db: Session, router: DataHubRouter, symbol: str) -> bool:
    return has_product_cache(db, symbol) or router_supports_product_data(router)


def _refresh_call(
    router: DataHubRouter,
    capability: str,
    *,
    symbol: str,
    industry: str | None,
    analysis_started_at: datetime,
) -> ProviderResult:
    calendar = get_trading_calendar()
    business_date = calendar.latest_completed_session(analysis_started_at)
    start_date = business_date - timedelta(days=45)
    if capability == "market.intraday.60m":
        return router.get_intraday_60m(
            symbol,
            datetime.combine(start_date, datetime.min.time(), tzinfo=analysis_started_at.tzinfo),
            analysis_started_at,
        )
    if capability == "market.turnover.daily":
        return router.get_turnover_daily(symbol, start_date, business_date)
    if capability == "market.breadth.daily":
        return router.get_market_breadth(business_date)
    if capability == "market.amount.daily":
        return router.get_market_amount_history(start_date, business_date)
    if capability == "market.industry.daily":
        return router.get_industry_universe(start_date, business_date)
    if capability == "market.industry.constituents":
        return router.get_industry_constituents_universe()
    if capability == "company.concepts":
        return router.company_concepts(symbol)
    if capability == "company.industry_chain":
        return router.company_industry_chain(symbol)
    raise ValueError(f"unsupported product refresh capability: {capability}")


def refresh_product_data(
    db: Session,
    router: DataHubRouter,
    *,
    symbol: str,
    industry: str | None,
    analysis_started_at: datetime,
    force_refresh: bool,
) -> tuple[RequiredDataPlan, dict[str, ProductCacheSelection]]:
    selected = select_product_data(
        db,
        symbol=symbol,
        industry=industry,
        evaluated_at=analysis_started_at,
    )
    initial_plan = build_required_data_plan(
        symbol=symbol,
        analysis_started_at=analysis_started_at,
        states=tuple(
            _state_from_selection(capability, selection)
            for capability, selection in sorted(selected.items())
        ),
        force_refresh=force_refresh,
    )
    for item in initial_plan.items:
        if item.action != RequiredDataAction.REFRESH:
            continue
        try:
            result = _refresh_call(
                router,
                item.capability,
                symbol=symbol,
                industry=industry,
                analysis_started_at=analysis_started_at,
            )
            if (
                result.quality_status
                in {
                    DataQualityStatus.VERIFIED,
                    DataQualityStatus.SINGLE_SOURCE,
                }
                and result.value
            ):
                persist_product_result(db, router, result)
        except Exception:
            # The Router has already audited provider failures. Selection below decides
            # whether the previous exact cache can still be used.
            logger.exception(
                "product capability refresh failed",
                extra={"capability": item.capability, "symbol": symbol},
            )
            continue
    selected = select_product_data(
        db,
        symbol=symbol,
        industry=industry,
        evaluated_at=analysis_started_at,
    )
    final_plan = build_required_data_plan(
        symbol=symbol,
        analysis_started_at=analysis_started_at,
        states=tuple(
            _state_from_selection(capability, selection)
            for capability, selection in sorted(selected.items())
        ),
        force_refresh=False,
        refresh_attempted=True,
    )
    return final_plan, selected


def _aware_record_time(record: DataQualityRecord, value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    if record.capability.startswith("market."):
        return market_storage_naive_to_aware(value)
    return utc_storage_naive_to_aware(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat(timespec="microseconds")
        return value.isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in sorted(value.items())}
    return value


def _orm_payload(row: Any) -> dict[str, Any]:
    if isinstance(row, tuple) or hasattr(row, "_mapping"):
        items = tuple(row)
        position = next((item for item in items if isinstance(item, CompanyChainPosition)), None)
        node = next((item for item in items if isinstance(item, IndustryChainNode)), None)
        chain = next((item for item in items if isinstance(item, IndustryChain)), None)
        if position is not None and node is not None and chain is not None:
            return {
                "chain_name": chain.name,
                "node_name": node.name,
                "stage": node.stage,
                "relevance": position.relevance,
                "primary_products": _json_value(position.primary_products or []),
                "revenue_relevance": position.revenue_relevance,
                "core_level": position.core_level,
                "substitutability": position.substitutability,
                "competitive_position": position.competitive_position,
                "observed_at": _json_value(position.observed_at),
                "source": chain.source,
            }
        values: dict[str, Any] = {}
        for item in items:
            values.update(_orm_payload(item))
        return values
    table = getattr(row, "__table__", None)
    if table is None:
        return _json_value(dict(row))
    return {
        column.name: _json_value(getattr(row, column.name))
        for column in table.columns
        if column.name not in {"id", "quality_record_id"}
    }


def snapshot_from_product_data(
    db: Session,
    *,
    symbol: str,
    analysis_started_at: datetime,
    selected: dict[str, ProductCacheSelection],
    legacy_evidence: tuple[Evidence, ...] = (),
) -> ProductAnalysisSnapshot:
    capabilities: list[SnapshotCapability] = []
    for capability, selection in sorted(selected.items()):
        record = (
            db.get(DataQualityRecord, selection.quality_record_id)
            if selection.quality_record_id
            else None
        )
        capabilities.append(
            SnapshotCapability(
                capability=capability,
                subject=selection.subject,
                required=capability in PRODUCT_REQUIRED_CAPABILITIES,
                quality_status=selection.effective_quality.effective_quality,
                executable=selection.executable,
                quality_record_id=selection.quality_record_id,
                observed_at=_aware_record_time(record, record.observed_at) if record else None,
                fetched_at=_aware_record_time(record, record.fetched_at) if record else None,
                normalized_digest=record.normalized_digest if record else None,
                rows=tuple(_orm_payload(row) for row in selection.rows),
                evidence_refs=(f"product:{capability}",),
            )
        )
    existing = {item.capability for item in capabilities}
    for evidence in legacy_evidence:
        binding = evidence.market_quality_binding or evidence.source_quality_binding
        data_capability = binding.data_capability if binding else None
        if not evidence.required or not binding or data_capability in existing:
            continue
        record = db.get(DataQualityRecord, binding.quality_record_id)
        rows: tuple[dict[str, Any], ...] = (dict(evidence.payload),)
        if data_capability in {"market.daily.qfq", "market.index_daily"}:
            cache_symbol = symbol if data_capability == "market.daily.qfq" else binding.subject_id
            stored = db.scalars(
                select(MarketDailyBar)
                .where(
                    MarketDailyBar.symbol == cache_symbol,
                    MarketDailyBar.quality_record_id == binding.quality_record_id,
                )
                .order_by(MarketDailyBar.trade_date)
            ).all()
            rows = tuple(_orm_payload(row) for row in stored)
        capabilities.append(
            SnapshotCapability(
                capability=data_capability,
                subject=SubjectRef(
                    subject_type=binding.subject_type,
                    subject_id=binding.subject_id,
                    semantic_key=binding.semantic_key,
                ),
                required=True,
                quality_status=evidence.quality_status,
                executable=not evidence.quality_status.blocks_execution,
                quality_record_id=binding.quality_record_id,
                observed_at=_aware_record_time(record, record.observed_at)
                if record
                else to_shanghai_aware(binding.observed_at),
                fetched_at=_aware_record_time(record, record.fetched_at) if record else None,
                normalized_digest=record.normalized_digest if record else None,
                rows=rows,
                evidence_refs=(evidence.evidence_id,),
            )
        )
        existing.add(data_capability)
    return ProductAnalysisSnapshot(
        analysis_started_at=analysis_started_at,
        symbol=symbol,
        capabilities=tuple(capabilities),
    )


def _capability(snapshot: ProductAnalysisSnapshot, name: str) -> SnapshotCapability | None:
    return next((item for item in snapshot.capabilities if item.capability == name), None)


def _price_bars(rows: tuple[dict[str, Any], ...], *, intraday: bool) -> tuple[PriceBar, ...]:
    result = []
    calendar = get_trading_calendar()
    for row in rows:
        if intraday:
            timestamp = datetime.fromisoformat(str(row["bar_end"]))
            timestamp = to_shanghai_aware(timestamp, naive_is_shanghai=True)
        else:
            timestamp = calendar.session_close_at(date.fromisoformat(str(row["trade_date"])))
        result.append(
            PriceBar(
                timestamp=timestamp,
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
            )
        )
    return tuple(result)


def analyze_snapshot(
    snapshot: ProductAnalysisSnapshot,
    *,
    previous_market_state: str | None = None,
) -> tuple[TechnicalContext, MarketRegime, IndustryContext, ConceptChainContext]:
    daily = _capability(snapshot, "market.daily.qfq")
    intraday = _capability(snapshot, "market.intraday.60m")
    turnover = _capability(snapshot, "market.turnover.daily")
    breadth = _capability(snapshot, "market.breadth.daily")
    amount = _capability(snapshot, "market.amount.daily")
    industry = _capability(snapshot, "market.industry.daily")
    concepts = _capability(snapshot, "company.concepts")
    chains = _capability(snapshot, "company.industry_chain")
    index_changes = {}
    benchmark_changes: tuple[Decimal, ...] = ()
    for index_capability in (
        item for item in snapshot.capabilities if item.capability == "market.index_daily"
    ):
        closes = [
            Decimal(str(row["close"]))
            for row in index_capability.rows
            if row.get("close") is not None
        ]
        if len(closes) >= 2 and closes[-2] != 0:
            index_changes[index_capability.subject.subject_id] = (
                (closes[-1] - closes[-2]) / closes[-2] * Decimal("100")
            )
            if not benchmark_changes:
                benchmark_changes = tuple(
                    (current - previous) / previous * Decimal("100")
                    for previous, current in zip(closes, closes[1:])
                    if previous != 0
                )
    technical = analyze_intraday_turnover(
        TechnicalAnalysisInput(
            daily_bars=_price_bars(daily.rows, intraday=False) if daily else (),
            intraday_bars=_price_bars(intraday.rows, intraday=True) if intraday else (),
            turnover=tuple(
                TurnoverPoint(
                    trade_date=date.fromisoformat(str(row["trade_date"])),
                    turnover_rate=row["turnover_rate"],
                )
                for row in (turnover.rows if turnover else ())
            ),
        )
    )
    breadth_row = breadth.rows[-1] if breadth and breadth.rows else None
    if breadth_row:
        market_input = dict(
            current=BreadthMetrics(
                trade_date=date.fromisoformat(str(breadth_row["trade_date"])),
                advancing=breadth_row["advancing"],
                declining=breadth_row["declining"],
                unchanged=breadth_row["unchanged"],
                limit_up=breadth_row["limit_up"],
                limit_down=breadth_row["limit_down"],
                new_highs=breadth_row.get("new_highs"),
                new_lows=breadth_row.get("new_lows"),
                median_change_pct=breadth_row.get("median_change_pct"),
                above_ma20_ratio=breadth_row.get("above_ma20_ratio"),
                above_ma50_ratio=breadth_row.get("above_ma50_ratio"),
            ),
            amount_history=tuple(
                Decimal(str(row["total_amount"])) for row in (amount.rows if amount else ())
            ),
            index_changes=index_changes,
        )
        if previous_market_state is None:
            previous_market_state = infer_market_state_without_history(
                market_input["current"],
                amount_history=market_input["amount_history"],
                index_changes=market_input["index_changes"],
            )
        market = analyze_market_regime(
            MarketRegimeInput(previous_state=previous_market_state, **market_input)
        )
    else:
        market = MarketRegime(
            state="CONTRACTION",
            previous_state="CONTRACTION",
            transition="CONTRACTION->CONTRACTION",
            data_completeness=0,
            supporting_indicators=(),
            conflicting_indicators=("market breadth is missing",),
            confidence="LOW",
            offensive_allowed=False,
            market_position_cap=0,
            advance_ratio=None,
            amount_ratio_5d=None,
            amount_ratio_20d=None,
        )
    industry_series: tuple[IndustrySeries, ...] = ()
    if industry and industry.rows:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in industry.rows:
            grouped.setdefault(str(row["industry_name"]), []).append(row)
        industry_series = tuple(
            IndustrySeries(
                name=name,
                observations=tuple(
                    IndustryObservation(
                        trade_date=date.fromisoformat(str(row["trade_date"])),
                        change_pct=row.get("change_pct"),
                        amount=row.get("amount"),
                        amount_share=row.get("amount_share"),
                        advance_ratio=row.get("advance_ratio"),
                        limit_up_count=row.get("limit_up_count"),
                        leader_strength=row.get("leader_strength"),
                        new_high_ratio=row.get("new_high_ratio"),
                    )
                    for row in rows
                ),
            )
            for name, rows in sorted(grouped.items())
        )
    industry_context = analyze_industry_mainlines(
        IndustryAnalysisInput(
            industries=industry_series,
            benchmark_changes=benchmark_changes,
        )
    )
    concept_input = tuple(
        ConceptMapping(
            concept=str(row.get("name") or row.get("concept") or "unknown"),
            relevance=str(row.get("relevance") or "INSUFFICIENT_EVIDENCE"),
            evidence_refs=("product:company.concepts",),
        )
        for row in (concepts.rows if concepts else ())
    )
    chain_input = tuple(
        ChainPosition(
            chain=str(row.get("chain_name") or row.get("chain") or "unknown"),
            node=str(row.get("node_name") or row.get("node") or "unknown"),
            stage=str(row.get("stage") or "unknown"),
            relevance=str(row.get("relevance") or "INSUFFICIENT_EVIDENCE"),
            primary_products=tuple(row.get("primary_products") or ()),
            revenue_relevance=str(row.get("revenue_relevance") or "unknown"),
            core_level=row.get("core_level"),
            substitutability=row.get("substitutability"),
            competitive_position=row.get("competitive_position"),
            evidence_refs=("product:company.industry_chain",),
        )
        for row in (chains.rows if chains else ())
    )
    concept_chain = analyze_concept_chain(
        ConceptChainInput(
            symbol=snapshot.symbol,
            concepts=concept_input,
            chain_positions=chain_input,
            current_mainlines=industry_context.mainlines,
        )
    )
    return technical, market, industry_context, concept_chain


def _previous_market_state(db: Session, snapshot: ProductAnalysisSnapshot) -> str | None:
    breadth = _capability(snapshot, "market.breadth.daily")
    if not breadth or not breadth.rows:
        return None
    trade_date = date.fromisoformat(str(breadth.rows[-1]["trade_date"]))
    return db.scalar(
        select(MarketRegimeSnapshot.state)
        .where(MarketRegimeSnapshot.trade_date < trade_date)
        .order_by(MarketRegimeSnapshot.trade_date.desc())
        .limit(1)
    )


_MARKET_STATE_CAPABILITIES = frozenset(
    {"market.breadth.daily", "market.amount.daily", "market.index_daily"}
)
_INDUSTRY_STATE_CAPABILITIES = frozenset(
    {
        "market.industry.daily",
        "market.industry.constituents",
        "market.index_daily",
    }
)


def _analysis_quality_bindings(
    snapshot: ProductAnalysisSnapshot,
    capabilities: frozenset[str],
) -> list[dict[str, Any]]:
    bindings = []
    for item in snapshot.capabilities:
        if item.capability not in capabilities:
            continue
        if item.quality_record_id is None or item.observed_at is None:
            continue
        bindings.append(
            {
                "capability": item.capability,
                "subject": item.subject.model_dump(mode="json"),
                "quality_record_id": item.quality_record_id,
                "observed_at": item.observed_at.isoformat(),
            }
        )
    return sorted(
        bindings,
        key=lambda row: (
            row["capability"],
            row["subject"]["subject_type"],
            row["subject"]["subject_id"],
            row["subject"].get("semantic_key") or "",
        ),
    )


def _analysis_quality(
    snapshot: ProductAnalysisSnapshot,
    capabilities: frozenset[str],
) -> DataQualityStatus:
    by_capability = {
        item.capability: item.quality_status
        for item in snapshot.capabilities
        if item.capability in capabilities
    }
    return worst_quality(
        [
            by_capability.get(capability, DataQualityStatus.MISSING)
            for capability in sorted(capabilities)
        ]
    )


def _persist_market_regime(
    db: Session,
    snapshot: ProductAnalysisSnapshot,
    regime: MarketRegime,
) -> None:
    breadth = _capability(snapshot, "market.breadth.daily")
    if not breadth or not breadth.rows:
        return
    trade_date = date.fromisoformat(str(breadth.rows[-1]["trade_date"]))
    observed_at = breadth.observed_at or snapshot.analysis_started_at
    stored = db.scalar(
        select(MarketRegimeSnapshot).where(
            MarketRegimeSnapshot.market_id == "CN-A",
            MarketRegimeSnapshot.trade_date == trade_date,
        )
    )
    if stored is None:
        stored = MarketRegimeSnapshot(market_id="CN-A", trade_date=trade_date)
        db.add(stored)
    stored.state = regime.state
    stored.previous_state = regime.previous_state
    stored.transition = regime.transition
    stored.product_snapshot_hash = snapshot.snapshot_hash
    stored.observed_at = _market_time_for_storage(observed_at)
    stored.quality_status = _analysis_quality(snapshot, _MARKET_STATE_CAPABILITIES).value
    stored.quality_bindings = _analysis_quality_bindings(snapshot, _MARKET_STATE_CAPABILITIES)


def _persist_industry_context(
    db: Session,
    snapshot: ProductAnalysisSnapshot,
    context: IndustryContext,
) -> None:
    source = _capability(snapshot, "market.industry.daily")
    if not source or not source.rows:
        return
    trade_date = max(date.fromisoformat(str(row["trade_date"])) for row in source.rows)
    observed_at = source.observed_at or snapshot.analysis_started_at
    quality = _analysis_quality(snapshot, _INDUSTRY_STATE_CAPABILITIES).value
    bindings = _analysis_quality_bindings(snapshot, _INDUSTRY_STATE_CAPABILITIES)
    for assessment in context.industries:
        stored = db.scalar(
            select(IndustryAnalysisSnapshot).where(
                IndustryAnalysisSnapshot.industry_name == assessment.name,
                IndustryAnalysisSnapshot.trade_date == trade_date,
            )
        )
        if stored is None:
            stored = IndustryAnalysisSnapshot(
                industry_name=assessment.name,
                trade_date=trade_date,
            )
            db.add(stored)
        stored.classification = assessment.classification
        stored.product_snapshot_hash = snapshot.snapshot_hash
        stored.observed_at = _market_time_for_storage(observed_at)
        stored.quality_status = quality
        stored.quality_bindings = bindings


def _market_time_for_storage(value: datetime) -> datetime:
    return to_market_storage_naive(to_shanghai_aware(value))


def _base_rule_plan(preview: dict[str, Any]) -> BaseRulePlan:
    buy_plan = preview.get("buy_plan") or {}
    calculations = preview.get("position_calculation") or {}
    zone = buy_plan.get("buy_zone") or [0, 0]
    final_quantity = int(calculations.get("final_allowed_quantity") or 0)
    return BaseRulePlan(
        rule_status=preview.get("deterministic_rule_status") or preview["status"],
        buy_zone_low=Decimal(str(zone[0] or 0)),
        buy_zone_high=Decimal(str(zone[1] or 0)),
        hard_stop=Decimal(str(buy_plan.get("hard_stop") or 0)),
        final_quantity=final_quantity,
        trial_quantity=int(calculations.get("trial_quantity") or 0),
        per_share_risk=Decimal(str(calculations.get("per_share_risk") or 0)),
        maximum_loss=Decimal(str(calculations.get("maximum_loss") or 0)),
        base_position_pct=Decimal(str(preview.get("account", {}).get("position_pct") or 0)),
        max_position_pct=Decimal("100"),
        trigger_condition=str(buy_plan.get("trigger_condition") or "data required"),
        logical_invalidation=str(
            buy_plan.get("logic_invalidation")
            or buy_plan.get("structure_invalidation")
            or "data required"
        ),
    )


def _product_evidence(
    db: Session,
    snapshot: ProductAnalysisSnapshot,
) -> tuple[Evidence, ...]:
    rows = []
    for item in snapshot.capabilities:
        if not item.capability.startswith(("market.", "company.")):
            continue
        record = (
            db.get(DataQualityRecord, item.quality_record_id) if item.quality_record_id else None
        )
        binding = None
        if item.quality_record_id and item.observed_at:
            binding = MarketQualityBinding(
                data_capability=item.capability,
                subject_type=item.subject.subject_type,
                subject_id=item.subject.subject_id,
                semantic_key=item.subject.semantic_key or "",
                quality_record_id=item.quality_record_id,
                observed_at=item.observed_at,
            )
        rows.append(
            Evidence(
                evidence_id=f"product:{item.capability}",
                symbol=snapshot.symbol,
                capability=item.capability,
                required=item.required,
                category="product_data",
                source_name=record.provider_id if record else "unavailable",
                observed_at=item.observed_at.isoformat() if item.observed_at else None,
                fetched_at=item.fetched_at.isoformat() if item.fetched_at else None,
                cached_at=(
                    _aware_record_time(record, record.cached_at).isoformat()
                    if record and record.cached_at
                    else None
                ),
                quality_status=item.quality_status,
                market_quality_binding=binding,
                payload={
                    "subject": item.subject.model_dump(mode="json"),
                    "normalized_digest": item.normalized_digest,
                    "row_count": len(item.rows),
                    "snapshot_hash": snapshot.snapshot_hash,
                    "summary": list(item.rows[-3:]),
                },
                external_text_is_untrusted=False,
            )
        )
    return tuple(rows)


def run_product_pipeline(
    db: Session,
    router: DataHubRouter,
    *,
    symbol: str,
    industry: str | None,
    analysis_started_at: datetime,
    force_refresh: bool,
    preview: dict[str, Any],
    legacy_evidence: tuple[Evidence, ...],
    announcement_risk: str = "UNKNOWN",
) -> ProductPipelineResult:
    required_data, selected = refresh_product_data(
        db,
        router,
        symbol=symbol,
        industry=industry,
        analysis_started_at=analysis_started_at,
        force_refresh=force_refresh,
    )
    snapshot = snapshot_from_product_data(
        db,
        symbol=symbol,
        analysis_started_at=analysis_started_at,
        selected=selected,
        legacy_evidence=legacy_evidence,
    )
    previous_market_state = _previous_market_state(db, snapshot)
    technical, market, industry_context, concept_chain = analyze_snapshot(
        snapshot,
        previous_market_state=previous_market_state,
    )
    _persist_market_regime(db, snapshot, market)
    _persist_industry_context(db, snapshot, industry_context)
    required_statuses = [item.quality_status for item in snapshot.capabilities if item.required]
    data_quality = worst_quality(required_statuses)
    constituent_capability = _capability(snapshot, "market.industry.constituents")
    universe_industry = next(
        (
            str(row.get("industry_name") or row.get("industry"))
            for row in (constituent_capability.rows if constituent_capability else ())
            if str(row.get("symbol") or "").zfill(6) == symbol
        ),
        None,
    )
    selected_industry = universe_industry or industry
    current_industry = next(
        (
            item
            for item in industry_context.industries
            if selected_industry and item.name == selected_industry
        ),
        None,
    )
    decision_context = TradeDecisionContext(
        base_plan=_base_rule_plan(preview),
        technical=technical,
        market=market,
        industry=current_industry,
        concept_chain=concept_chain,
        data_quality=data_quality,
        announcement_risk=announcement_risk,
    )
    registry = get_strategy_registry()
    signals = []
    bindings = []
    for strategy in registry.enabled_strategies():
        manifest = strategy.manifest()
        parameters = strategy.validate_parameters(manifest.default_parameters)
        missing_capabilities = tuple(
            capability
            for capability in strategy.required_capabilities()
            if not snapshot.has_capability(capability)
        )
        signal = strategy.evaluate(snapshot, parameters)
        if (
            signal.strategy_id != manifest.strategy_id
            or signal.strategy_version != manifest.version
        ):
            raise ValueError("strategy signal identity does not match registered manifest")
        if missing_capabilities and signal.applicable:
            raise ValueError(
                "strategy signal cannot be applicable with missing required capabilities"
            )
        signals.append(signal)
        bindings.append(
            StrategyBinding(
                strategy_id=signal.strategy_id,
                strategy_version=signal.strategy_version,
                implementation_hash=manifest.implementation_hash,
                parameter_hash=signal.parameter_hash,
                signal_hash=signal.signal_hash,
            )
        )
    strategy_signals = tuple(signals)
    candidates = candidates_from_signals(strategy_signals)
    trade_decision = combine_strategy_candidates(
        candidates,
        context=decision_context,
    )
    evidence = _product_evidence(db, snapshot)
    executable_count = sum(item.executable for item in snapshot.capabilities if item.required)
    required_count = sum(item.required for item in snapshot.capabilities)
    completeness = (
        Decimal(executable_count) / Decimal(required_count) if required_count else Decimal(0)
    )
    breadth_capability = _capability(snapshot, "market.breadth.daily")
    breadth_row = (
        breadth_capability.rows[-1] if breadth_capability and breadth_capability.rows else None
    )
    breadth_universe = None
    if breadth_row:
        breadth_universe = {
            "universe_id": breadth_row.get("universe_id"),
            "universe_version": breadth_row.get("universe_version"),
            "universe_name": "沪深普通A股",
            "exchanges": ["上海证券交易所", "深圳证券交易所"],
            "boards": ["沪市主板", "科创板", "深市主板", "创业板"],
            "as_of_date": breadth_row.get("trade_date"),
            "completeness": breadth_row.get("reconciliation_status"),
            "primary_count": breadth_row.get("primary_count"),
            "supplementary_count": breadth_row.get("supplementary_count"),
            "degradation_status": (
                "SUPPLEMENTARY_DATA_USED"
                if breadth_row.get("supplementary_count", 0) > 0
                else "NONE"
            ),
        }
    return ProductPipelineResult(
        required_data=required_data,
        snapshot=snapshot,
        technical_context=technical,
        market_regime=market,
        industry_context=industry_context,
        concept_chain_context=concept_chain,
        trade_decision=trade_decision,
        evidence=evidence,
        strategy_bindings=tuple(bindings),
        strategy_signals=strategy_signals,
        data_completeness=completeness,
        breadth_universe=breadth_universe,
    )


__all__ = [
    "ProductDataState",
    "ProductPipelineResult",
    "RequiredDataAction",
    "RequiredDataItem",
    "RequiredDataPlan",
    "build_required_data_plan",
    "has_product_cache",
    "refresh_product_data",
    "router_supports_product_data",
    "should_run_product_pipeline",
    "run_product_pipeline",
    "select_product_data",
    "snapshot_from_product_data",
]
