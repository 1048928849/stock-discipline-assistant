from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.effective_quality import resolve_effective_quality
from app.data_hub.quality import canonical_digest, policy_for
from app.data_hub.router import DataHubRouter, ProviderResult
from app.data_hub.trading_calendar import (
    market_storage_naive_to_aware,
    to_market_storage_naive,
)
from app.domain.quality_subject import EffectiveQualityResult, SubjectRef, canonical_semantic_key
from app.models import (
    DataQualityRecord,
    IndustryCapitalFlowSnapshot,
    MarketEventPoolSnapshot,
)


DISCOVERY_DATA_CAPABILITIES = frozenset(
    {
        "market.industry.capital_flow",
        "market.limit_up_pool",
        "market.broken_limit_pool",
    }
)


@dataclass(frozen=True)
class DiscoveryCacheSelection:
    rows: list[Any]
    subject: SubjectRef
    quality_record_id: int | None
    observed_at: datetime | None
    effective_quality: EffectiveQualityResult
    executable: bool
    blocking_reason: str | None


def _row_values(row: Any) -> dict[str, Any]:
    if is_dataclass(row):
        return asdict(row)
    if isinstance(row, dict):
        return dict(row)
    raise ProviderUnavailableError("discovery persistence requires structured rows")


def _persist_capital_flow(db: Session, result: ProviderResult, rows: list[dict]) -> int:
    day = rows[0]["trade_date"]
    if any(row["trade_date"] != day for row in rows):
        raise ProviderUnavailableError("capital flow payload contains mixed trade dates")
    db.execute(delete(IndustryCapitalFlowSnapshot).where(IndustryCapitalFlowSnapshot.trade_date == day))
    db.add_all(
        [
            IndustryCapitalFlowSnapshot(
                industry_key=row["industry_key"],
                industry_name=row["industry_name"],
                trade_date=row["trade_date"],
                net_inflow_1d=row["net_inflow_1d"],
                net_inflow_5d=row["net_inflow_5d"],
                net_inflow_10d=row["net_inflow_10d"],
                amount=row["amount"],
                amount_unit=row["amount_unit"],
                source=row["source"],
                observed_at=to_market_storage_naive(row["observed_at"]),
                fetched_at=to_market_storage_naive(row["fetched_at"]),
                quality_record_id=result.quality_record_id,
            )
            for row in rows
        ]
    )
    return len(rows)


def _persist_pool(db: Session, result: ProviderResult, rows: list[dict]) -> int:
    event_type = (
        "LIMIT_UP" if result.capability == "market.limit_up_pool" else "BROKEN_LIMIT"
    )
    if result.observed_at is None:
        raise ProviderUnavailableError("event pool persistence requires observed_at")
    observed_at = result.observed_at
    day = observed_at.date() if isinstance(observed_at, datetime) else observed_at
    if any(row["event_type"] != event_type or row["trade_date"] != day for row in rows):
        raise ProviderUnavailableError("event pool payload scope mismatch")
    db.execute(
        delete(MarketEventPoolSnapshot).where(
            MarketEventPoolSnapshot.trade_date == day,
            MarketEventPoolSnapshot.event_type == event_type,
        )
    )
    db.add_all(
        [
            MarketEventPoolSnapshot(
                symbol=row["symbol"],
                name=row["name"],
                trade_date=row["trade_date"],
                event_type=row["event_type"],
                first_event_at=(
                    to_market_storage_naive(row["first_event_at"])
                    if row["first_event_at"]
                    else None
                ),
                last_event_at=(
                    to_market_storage_naive(row["last_event_at"])
                    if row["last_event_at"]
                    else None
                ),
                sealed_amount=row["sealed_amount"],
                sealed_amount_unit=row["sealed_amount_unit"],
                turnover_rate=row["turnover_rate"],
                turnover_rate_unit=row["turnover_rate_unit"],
                consecutive_days=row["consecutive_days"],
                industry_name=row["industry_name"],
                reason_summary=row["reason_summary"],
                source=row["source"],
                observed_at=to_market_storage_naive(row["observed_at"]),
                fetched_at=to_market_storage_naive(row["fetched_at"]),
                quality_record_id=result.quality_record_id,
            )
            for row in rows
        ]
    )
    return len(rows)


def persist_discovery_result(
    db: Session,
    router: DataHubRouter,
    result: ProviderResult,
) -> int:
    if result.capability not in DISCOVERY_DATA_CAPABILITIES:
        raise ProviderUnavailableError("unsupported discovery persistence capability")
    router.validate_persistence_result(result)
    value = result.require_trusted_value()
    if not isinstance(value, list):
        raise ProviderUnavailableError("discovery persistence requires a list payload")
    if canonical_digest(value, policy_for(result.capability)) != result.normalized_digest:
        raise ProviderUnavailableError("discovery payload digest does not match Router audit")
    rows = [_row_values(row) for row in value]
    if len(rows) != DataHubRouter.payload_row_count(result.capability, value):
        raise ProviderUnavailableError("discovery persistence row count mismatch")
    if result.capability == "market.industry.capital_flow" and not rows:
        raise ProviderUnavailableError("capital flow persistence requires non-empty rows")
    with db.begin_nested():
        count = (
            _persist_capital_flow(db, result, rows)
            if result.capability == "market.industry.capital_flow"
            else _persist_pool(db, result, rows)
        )
        db.flush()
        router.mark_persisted(result)
    return count


def resolve_discovery_cache(
    db: Session,
    *,
    capability: str,
    subject: SubjectRef,
    evaluated_at: datetime,
) -> DiscoveryCacheSelection:
    if capability not in DISCOVERY_DATA_CAPABILITIES:
        raise ValueError("unsupported discovery cache capability")
    candidates = db.scalars(
        select(DataQualityRecord)
        .where(
            DataQualityRecord.capability == capability,
            DataQualityRecord.subject_type == subject.subject_type,
            DataQualityRecord.subject_id == subject.subject_id,
            DataQualityRecord.persisted.is_(True),
        )
        .order_by(DataQualityRecord.id.desc())
    ).all()
    record = next(
        (
            item
            for item in candidates
            if canonical_semantic_key(item.semantic_key)
            == canonical_semantic_key(subject.semantic_key)
        ),
        None,
    )
    rows: list[Any] = []
    if record is not None and capability == "market.industry.capital_flow":
        rows = list(
            db.scalars(
                select(IndustryCapitalFlowSnapshot)
                .where(IndustryCapitalFlowSnapshot.quality_record_id == record.id)
                .order_by(IndustryCapitalFlowSnapshot.industry_key)
            )
        )
    elif record is not None:
        event_type = "LIMIT_UP" if capability == "market.limit_up_pool" else "BROKEN_LIMIT"
        rows = list(
            db.scalars(
                select(MarketEventPoolSnapshot)
                .where(
                    MarketEventPoolSnapshot.quality_record_id == record.id,
                    MarketEventPoolSnapshot.event_type == event_type,
                )
                .order_by(MarketEventPoolSnapshot.symbol)
            )
        )
    observed_at = (
        market_storage_naive_to_aware(record.observed_at)
        if record is not None and record.observed_at is not None
        else None
    )
    cached_at = (
        market_storage_naive_to_aware(record.cached_at)
        if record is not None and record.cached_at is not None
        else None
    )
    effective = resolve_effective_quality(
        db,
        capability=capability,
        subject=subject,
        persisted_quality_record_id=record.id if record else None,
        observed_at=observed_at,
        cached_at=cached_at,
        evaluated_at=evaluated_at,
    )
    row_shape_valid = bool(rows) or (
        record is not None
        and record.row_count == 0
        and capability in {"market.limit_up_pool", "market.broken_limit_pool"}
    )
    return DiscoveryCacheSelection(
        rows=rows,
        subject=subject,
        quality_record_id=record.id if record else None,
        observed_at=observed_at,
        effective_quality=effective,
        executable=effective.executable and row_shape_valid,
        blocking_reason=(
            effective.blocking_reason
            if not effective.executable
            else None
            if row_shape_valid
            else "discovery cache row shape mismatch"
        ),
    )


__all__ = [
    "DISCOVERY_DATA_CAPABILITIES",
    "DiscoveryCacheSelection",
    "persist_discovery_result",
    "resolve_discovery_cache",
]
