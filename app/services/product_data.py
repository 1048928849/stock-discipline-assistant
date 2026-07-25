from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.effective_quality import resolve_effective_quality
from app.data_hub.router import DataHubRouter, ProviderResult
from app.data_hub.trading_calendar import (
    market_storage_naive_to_aware,
    to_market_storage_naive,
    to_shanghai_aware,
    to_utc_storage_naive,
    utc_storage_naive_to_aware,
)
from app.domain.quality_subject import (
    EffectiveQualityResult,
    SubjectRef,
    canonical_semantic_key,
)
from app.models import (
    CompanyChainPosition,
    CompanyConcept,
    Concept,
    DataQualityRecord,
    IndustryChain,
    IndustryChainNode,
    IndustryConstituentSnapshot,
    IndustryMarketSnapshot,
    MappingEvidence,
    MarketAmountSnapshot,
    MarketBreadthSnapshot,
    MarketIntradayBar,
    MarketTurnoverSnapshot,
)


PRODUCT_CAPABILITIES = frozenset(
    {
        "market.intraday.60m",
        "market.turnover.daily",
        "market.breadth.daily",
        "market.amount.daily",
        "market.industry.daily",
        "market.industry.constituents",
        "company.concepts",
        "company.industry_chain",
    }
)


def _values(row: Any) -> dict[str, Any]:
    if is_dataclass(row):
        return asdict(row)
    if isinstance(row, dict):
        return dict(row)
    raise ProviderUnavailableError("product persistence requires structured rows")


def _market_time(value: datetime) -> datetime:
    aware = to_shanghai_aware(value, naive_is_shanghai=False)
    return to_market_storage_naive(aware)


def _stored_time(value: datetime, *, market: bool) -> datetime:
    aware = to_shanghai_aware(value, naive_is_shanghai=False)
    return to_market_storage_naive(aware) if market else to_utc_storage_naive(aware)


def _observed_at(row: dict[str, Any], result: ProviderResult) -> datetime:
    value = row.get("observed_at") or result.observed_at
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderUnavailableError("product persistence requires aware observed_at")
    return _stored_time(value, market=result.capability.startswith("market."))


def _fetched_at(row: dict[str, Any], result: ProviderResult) -> datetime:
    value = row.get("fetched_at") or result.fetched_at
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderUnavailableError("product persistence requires aware fetched_at")
    return _stored_time(value, market=result.capability.startswith("market."))


def _trade_date(row: dict[str, Any]) -> date:
    value = row.get("trade_date") or row.get("snapshot_date")
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ProviderUnavailableError("product persistence requires trade_date")
    return value


def _evidence_key(symbol: str, mapping_type: str, row: dict[str, Any]) -> str:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(symbol.encode("ascii") + mapping_type.encode("ascii") + payload).hexdigest()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    raise ProviderUnavailableError(
        f"mapping evidence contains unsupported JSON value: {type(value).__name__}"
    )


def _persist_intraday(db: Session, result: ProviderResult, rows: list[dict]) -> int:
    symbol = result.subject.subject_id
    db.execute(delete(MarketIntradayBar).where(MarketIntradayBar.symbol == symbol))
    for row in rows:
        if not row.get("completed"):
            raise ProviderUnavailableError("incomplete intraday bar cannot be persisted")
        db.add(
            MarketIntradayBar(
                symbol=symbol,
                trade_date=_trade_date(row),
                bar_start=_market_time(row["bar_start"]),
                bar_end=_market_time(row["bar_end"]),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                amount=row.get("amount"),
                turnover_rate=row.get("turnover_rate"),
                adjustment=row["adjustment"],
                price_unit=row["price_unit"],
                volume_unit=row["volume_unit"],
                observed_at=_observed_at(row, result),
                source=row["source"],
                fetched_at=_fetched_at(row, result),
                quality_record_id=result.quality_record_id,
            )
        )
    return len(rows)


def _persist_turnover(db: Session, result: ProviderResult, rows: list[dict]) -> int:
    symbol = result.subject.subject_id
    db.execute(delete(MarketTurnoverSnapshot).where(MarketTurnoverSnapshot.symbol == symbol))
    for row in rows:
        db.add(
            MarketTurnoverSnapshot(
                symbol=symbol,
                trade_date=_trade_date(row),
                turnover_rate=row["turnover_rate"],
                amount=row.get("amount"),
                observed_at=_observed_at(row, result),
                source=row["source"],
                fetched_at=_fetched_at(row, result),
                quality_record_id=result.quality_record_id,
            )
        )
    return len(rows)


def _persist_breadth(db: Session, result: ProviderResult, rows: list[dict]) -> int:
    dates = {_trade_date(row) for row in rows}
    db.execute(
        delete(MarketBreadthSnapshot).where(
            MarketBreadthSnapshot.market_id == "CN-A",
            MarketBreadthSnapshot.trade_date.in_(dates),
        )
    )
    for row in rows:
        db.add(
            MarketBreadthSnapshot(
                market_id="CN-A",
                trade_date=_trade_date(row),
                advancing=row["advancing"],
                declining=row["declining"],
                unchanged=row["unchanged"],
                limit_up=row["limit_up"],
                limit_down=row["limit_down"],
                new_highs=row.get("new_highs"),
                new_lows=row.get("new_lows"),
                median_change_pct=row.get("median_change_pct"),
                above_ma20_ratio=row.get("above_ma20_ratio"),
                above_ma50_ratio=row.get("above_ma50_ratio"),
                observed_at=_observed_at(row, result),
                source=row["source"],
                fetched_at=_fetched_at(row, result),
                quality_record_id=result.quality_record_id,
            )
        )
    return len(rows)


def _persist_amount(db: Session, result: ProviderResult, rows: list[dict]) -> int:
    dates = {_trade_date(row) for row in rows}
    db.execute(
        delete(MarketAmountSnapshot).where(
            MarketAmountSnapshot.market_id == "CN-A",
            MarketAmountSnapshot.trade_date.in_(dates),
        )
    )
    for row in rows:
        db.add(
            MarketAmountSnapshot(
                market_id="CN-A",
                trade_date=_trade_date(row),
                total_amount=row["total_amount"],
                observed_at=_observed_at(row, result),
                source=row["source"],
                fetched_at=_fetched_at(row, result),
                quality_record_id=result.quality_record_id,
            )
        )
    return len(rows)


def _persist_industry(db: Session, result: ProviderResult, rows: list[dict]) -> int:
    key = result.subject.subject_id
    db.execute(
        delete(IndustryMarketSnapshot).where(IndustryMarketSnapshot.industry_key == key)
    )
    for row in rows:
        db.add(
            IndustryMarketSnapshot(
                industry_key=key,
                industry_name=row["industry"],
                trade_date=_trade_date(row),
                change_pct=row.get("change_pct"),
                amount=row.get("amount"),
                amount_share=row.get("amount_share"),
                advance_ratio=row.get("advance_ratio"),
                limit_up_count=row.get("limit_up_count"),
                leader_strength=row.get("leader_strength"),
                new_high_ratio=row.get("new_high_ratio"),
                observed_at=_observed_at(row, result),
                source=row["source"],
                fetched_at=_fetched_at(row, result),
                quality_record_id=result.quality_record_id,
            )
        )
    return len(rows)


def _persist_constituents(db: Session, result: ProviderResult, rows: list[dict]) -> int:
    key = result.subject.subject_id
    db.execute(
        delete(IndustryConstituentSnapshot).where(
            IndustryConstituentSnapshot.industry_key == key
        )
    )
    for row in rows:
        observed = row.get("observed_at") or result.observed_at
        if not isinstance(observed, datetime):
            raise ProviderUnavailableError("constituent snapshot requires observed_at")
        db.add(
            IndustryConstituentSnapshot(
                industry_key=key,
                industry_name=row["industry"],
                symbol=str(row["symbol"]).zfill(6),
                name=row["name"],
                weight=row.get("weight"),
                snapshot_date=to_shanghai_aware(
                    observed, naive_is_shanghai=observed.tzinfo is None
                ).date(),
                observed_at=_observed_at(row, result),
                source=row["source"],
                fetched_at=_fetched_at(row, result),
                quality_record_id=result.quality_record_id,
            )
        )
    return len(rows)


def _persist_concepts(db: Session, result: ProviderResult, rows: list[dict]) -> int:
    symbol = result.subject.subject_id
    db.execute(delete(CompanyConcept).where(CompanyConcept.symbol == symbol))
    db.execute(
        delete(MappingEvidence).where(
            MappingEvidence.symbol == symbol,
            MappingEvidence.mapping_type == "concept",
        )
    )
    for row in rows:
        name = str(row.get("concept") or "").strip()
        if not name:
            raise ProviderUnavailableError("concept mapping requires a concept name")
        concept = db.scalar(select(Concept).where(Concept.name == name))
        if concept is None:
            concept = Concept(
                name=name,
                source=row["source"],
                quality_record_id=result.quality_record_id,
            )
            db.add(concept)
            db.flush()
        db.add(
            CompanyConcept(
                symbol=symbol,
                concept_id=concept.id,
                relevance=row.get("relevance", "INSUFFICIENT_EVIDENCE"),
                evidence_summary=row.get("evidence_summary"),
                observed_at=_observed_at(row, result),
                quality_record_id=result.quality_record_id,
            )
        )
        db.add(
            MappingEvidence(
                evidence_key=_evidence_key(symbol, "concept", row),
                symbol=symbol,
                mapping_type="concept",
                source_name=row["source"],
                source_url=row.get("source_url"),
                excerpt=row.get("evidence_summary") or name,
                raw_data=_json_value(row),
                observed_at=_observed_at(row, result),
                quality_record_id=result.quality_record_id,
            )
        )
    return len(rows)


def _persist_chain(db: Session, result: ProviderResult, rows: list[dict]) -> int:
    symbol = result.subject.subject_id
    db.execute(delete(CompanyChainPosition).where(CompanyChainPosition.symbol == symbol))
    db.execute(
        delete(MappingEvidence).where(
            MappingEvidence.symbol == symbol,
            MappingEvidence.mapping_type == "industry_chain",
        )
    )
    for row in rows:
        chain_name = str(row.get("chain") or "").strip()
        node_name = str(row.get("node") or "").strip()
        if not chain_name or not node_name:
            raise ProviderUnavailableError("industry-chain mapping requires chain and node")
        chain = db.scalar(select(IndustryChain).where(IndustryChain.name == chain_name))
        if chain is None:
            chain = IndustryChain(
                name=chain_name,
                source=row["source"],
                quality_record_id=result.quality_record_id,
            )
            db.add(chain)
            db.flush()
        node = db.scalar(
            select(IndustryChainNode).where(
                IndustryChainNode.chain_id == chain.id,
                IndustryChainNode.name == node_name,
            )
        )
        if node is None:
            node = IndustryChainNode(
                chain_id=chain.id,
                name=node_name,
                stage=row.get("stage", "UNKNOWN"),
                sort_order=int(row.get("sort_order", 0)),
                quality_record_id=result.quality_record_id,
            )
            db.add(node)
            db.flush()
        db.add(
            CompanyChainPosition(
                symbol=symbol,
                node_id=node.id,
                relevance=row.get("relevance", "INSUFFICIENT_EVIDENCE"),
                primary_products=row.get("primary_products"),
                revenue_relevance=row.get("revenue_relevance", "unknown"),
                core_level=row.get("core_level"),
                substitutability=row.get("substitutability"),
                competitive_position=row.get("competitive_position"),
                observed_at=_observed_at(row, result),
                quality_record_id=result.quality_record_id,
            )
        )
        db.add(
            MappingEvidence(
                evidence_key=_evidence_key(symbol, "industry_chain", row),
                symbol=symbol,
                mapping_type="industry_chain",
                source_name=row["source"],
                source_url=row.get("source_url"),
                excerpt=row.get("evidence_summary") or f"{chain_name}/{node_name}",
                raw_data=_json_value(row),
                observed_at=_observed_at(row, result),
                quality_record_id=result.quality_record_id,
            )
        )
    return len(rows)


_PERSISTERS = {
    "market.intraday.60m": _persist_intraday,
    "market.turnover.daily": _persist_turnover,
    "market.breadth.daily": _persist_breadth,
    "market.amount.daily": _persist_amount,
    "market.industry.daily": _persist_industry,
    "market.industry.constituents": _persist_constituents,
    "company.concepts": _persist_concepts,
    "company.industry_chain": _persist_chain,
}


def persist_product_result(
    db: Session,
    router: DataHubRouter,
    result: ProviderResult,
) -> int:
    if result.capability not in PRODUCT_CAPABILITIES:
        raise ProviderUnavailableError("unsupported product persistence capability")
    router.validate_persistence_result(result)
    value = result.require_trusted_value()
    if not isinstance(value, list) or not value:
        raise ProviderUnavailableError("product persistence requires non-empty rows")
    rows = [_values(row) for row in value]
    if len(rows) != DataHubRouter.payload_row_count(result.capability, result.value):
        raise ProviderUnavailableError("product persistence row count does not match lineage")
    with db.begin_nested():
        count = _PERSISTERS[result.capability](db, result, rows)
        db.flush()
        router.mark_persisted(result)
    return count


class ProductCacheSelection:
    def __init__(
        self,
        *,
        rows: list[Any],
        subject: SubjectRef,
        quality_record_id: int | None,
        observed_at: datetime | date | None,
        effective_quality: EffectiveQualityResult,
        structure_reason: str | None,
    ):
        self.rows = rows
        self.subject = subject
        self.quality_record_id = quality_record_id
        self.observed_at = observed_at
        self.effective_quality = effective_quality
        self.structure_reason = structure_reason
        self.executable = effective_quality.executable and structure_reason is None and bool(rows)
        self.blocking_reason = structure_reason or effective_quality.blocking_reason


def _stored_rows(
    db: Session,
    capability: str,
    subject: SubjectRef,
    quality_record_id: int,
) -> list[Any]:
    if capability == "market.intraday.60m":
        return list(
            db.scalars(
                select(MarketIntradayBar)
                .where(
                    MarketIntradayBar.symbol == subject.subject_id,
                    MarketIntradayBar.quality_record_id == quality_record_id,
                )
                .order_by(MarketIntradayBar.bar_start)
            )
        )
    if capability == "market.turnover.daily":
        return list(
            db.scalars(
                select(MarketTurnoverSnapshot)
                .where(
                    MarketTurnoverSnapshot.symbol == subject.subject_id,
                    MarketTurnoverSnapshot.quality_record_id == quality_record_id,
                )
                .order_by(MarketTurnoverSnapshot.trade_date)
            )
        )
    if capability == "market.breadth.daily":
        return list(
            db.scalars(
                select(MarketBreadthSnapshot)
                .where(MarketBreadthSnapshot.quality_record_id == quality_record_id)
                .order_by(MarketBreadthSnapshot.trade_date)
            )
        )
    if capability == "market.amount.daily":
        return list(
            db.scalars(
                select(MarketAmountSnapshot)
                .where(MarketAmountSnapshot.quality_record_id == quality_record_id)
                .order_by(MarketAmountSnapshot.trade_date)
            )
        )
    if capability == "market.industry.daily":
        return list(
            db.scalars(
                select(IndustryMarketSnapshot)
                .where(
                    IndustryMarketSnapshot.industry_key == subject.subject_id,
                    IndustryMarketSnapshot.quality_record_id == quality_record_id,
                )
                .order_by(IndustryMarketSnapshot.trade_date)
            )
        )
    if capability == "market.industry.constituents":
        return list(
            db.scalars(
                select(IndustryConstituentSnapshot)
                .where(
                    IndustryConstituentSnapshot.industry_key == subject.subject_id,
                    IndustryConstituentSnapshot.quality_record_id == quality_record_id,
                )
                .order_by(IndustryConstituentSnapshot.symbol)
            )
        )
    if capability == "company.concepts":
        return list(
            db.execute(
                select(CompanyConcept, Concept)
                .join(Concept, Concept.id == CompanyConcept.concept_id)
                .where(
                    CompanyConcept.symbol == subject.subject_id,
                    CompanyConcept.quality_record_id == quality_record_id,
                )
                .order_by(Concept.name)
            ).all()
        )
    if capability == "company.industry_chain":
        return list(
            db.execute(
                select(CompanyChainPosition, IndustryChainNode, IndustryChain)
                .join(IndustryChainNode, IndustryChainNode.id == CompanyChainPosition.node_id)
                .join(IndustryChain, IndustryChain.id == IndustryChainNode.chain_id)
                .where(
                    CompanyChainPosition.symbol == subject.subject_id,
                    CompanyChainPosition.quality_record_id == quality_record_id,
                )
                .order_by(IndustryChain.name, IndustryChainNode.sort_order)
            ).all()
        )
    raise ProviderUnavailableError("unsupported product cache capability")


def resolve_product_cache(
    db: Session,
    *,
    capability: str,
    subject: SubjectRef,
    evaluated_at: datetime,
) -> ProductCacheSelection:
    candidates = list(
        db.scalars(
            select(DataQualityRecord)
            .where(
                DataQualityRecord.capability == capability,
                DataQualityRecord.subject_type == subject.subject_type,
                DataQualityRecord.subject_id == subject.subject_id,
                DataQualityRecord.persisted.is_(True),
            )
            .order_by(DataQualityRecord.id.desc())
        )
    )
    latest_record = next(
        (
            item
            for item in candidates
            if canonical_semantic_key(item.semantic_key)
            == canonical_semantic_key(subject.semantic_key)
        ),
        None,
    )
    selected_quality_id = latest_record.id if latest_record else None
    rows = (
        _stored_rows(db, capability, subject, selected_quality_id)
        if selected_quality_id
        else []
    )
    quality_ids = {
        item.quality_record_id
        if hasattr(item, "quality_record_id")
        else item[0].quality_record_id
        for item in rows
    }
    quality_id = next(iter(quality_ids)) if len(quality_ids) == 1 else None
    structure_reason = None
    if rows and quality_id is None:
        structure_reason = "cached product rows do not share exact quality lineage"
    record = db.get(DataQualityRecord, quality_id) if quality_id else None
    observed_at = record.observed_at if record else None
    cached_at = record.cached_at if record else None
    if record and capability.startswith("market."):
        observed_at = (
            market_storage_naive_to_aware(observed_at) if observed_at else None
        )
        cached_at = market_storage_naive_to_aware(cached_at) if cached_at else None
    elif record:
        observed_at = utc_storage_naive_to_aware(observed_at) if observed_at else None
        cached_at = utc_storage_naive_to_aware(cached_at) if cached_at else None
    effective = resolve_effective_quality(
        db,
        capability=capability,
        subject=subject,
        persisted_quality_record_id=quality_id,
        observed_at=observed_at,
        cached_at=cached_at,
        evaluated_at=evaluated_at,
    )
    return ProductCacheSelection(
        rows=rows,
        subject=subject,
        quality_record_id=quality_id,
        observed_at=observed_at,
        effective_quality=effective,
        structure_reason=structure_reason,
    )


__all__ = [
    "PRODUCT_CAPABILITIES",
    "ProductCacheSelection",
    "persist_product_result",
    "resolve_product_cache",
]
