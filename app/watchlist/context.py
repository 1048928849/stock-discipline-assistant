from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_hub.effective_quality import resolve_effective_quality
from app.data_hub.trading_calendar import market_storage_naive_to_aware
from app.domain.quality import DataQualityStatus, worst_quality
from app.domain.quality_subject import SubjectRef
from app.models import (
    DataQualityRecord,
    IndustryAnalysisSnapshot,
    MarketRegimeSnapshot,
)
from app.watchlist.contracts import MonitoringHealth


MARKET_STATE_CAPABILITIES = frozenset(
    {"market.breadth.daily", "market.amount.daily", "market.index_daily"}
)
INDUSTRY_STATE_CAPABILITIES = frozenset(
    {
        "market.industry.daily",
        "market.industry.constituents",
        "market.index_daily",
    }
)


@dataclass(frozen=True)
class MonitoredAnalysisState:
    value: str | None
    quality_status: DataQualityStatus
    executable: bool
    observed_at: datetime | None
    snapshot_hash: str | None
    evidence_references: tuple[str, ...]
    blocking_reason: str | None = None

    @property
    def health(self) -> MonitoringHealth:
        if self.executable:
            return MonitoringHealth.HEALTHY
        return {
            DataQualityStatus.STALE: MonitoringHealth.STALE,
            DataQualityStatus.CONFLICTED: MonitoringHealth.CONFLICTED,
            DataQualityStatus.MISSING: MonitoringHealth.DATA_BLOCKED,
        }.get(self.quality_status, MonitoringHealth.DATA_BLOCKED)


def _missing(reason: str) -> MonitoredAnalysisState:
    return MonitoredAnalysisState(
        value=None,
        quality_status=DataQualityStatus.MISSING,
        executable=False,
        observed_at=None,
        snapshot_hash=None,
        evidence_references=(),
        blocking_reason=reason,
    )


def _validate_bindings(
    db: Session,
    bindings: list,
    *,
    expected_capabilities: frozenset[str],
    evaluated_at: datetime,
) -> tuple[DataQualityStatus, bool, tuple[str, ...], str | None]:
    by_capability = {
        str(binding.get("capability")): binding
        for binding in bindings
        if isinstance(binding, dict) and binding.get("capability")
    }
    if set(by_capability) != set(expected_capabilities):
        return DataQualityStatus.MISSING, False, (), "analysis_quality_binding_missing"
    qualities = []
    references = []
    for capability in sorted(expected_capabilities):
        binding = by_capability[capability]
        try:
            record_id = int(binding["quality_record_id"])
            subject = SubjectRef.model_validate(binding["subject"])
            bound_observed_at = datetime.fromisoformat(
                str(binding["observed_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            return DataQualityStatus.MISSING, False, tuple(references), (
                "analysis_quality_binding_invalid"
            )
        if bound_observed_at.tzinfo is None:
            return DataQualityStatus.MISSING, False, tuple(references), (
                "analysis_quality_binding_time_naive"
            )
        record = db.get(DataQualityRecord, record_id)
        if record is None or not record.persisted:
            return DataQualityStatus.MISSING, False, tuple(references), (
                "analysis_quality_record_missing"
            )
        effective = resolve_effective_quality(
            db,
            capability=capability,
            subject=subject,
            persisted_quality_record_id=record_id,
            observed_at=record.observed_at,
            cached_at=record.cached_at,
            evaluated_at=evaluated_at,
        )
        qualities.append(effective.effective_quality)
        references.append(f"quality_record:{record_id}")
        if not effective.executable:
            return (
                effective.effective_quality,
                False,
                tuple(references),
                effective.blocking_reason,
            )
        record_observed_at = effective.observed_at
        if isinstance(record_observed_at, datetime):
            if record_observed_at.tzinfo is None:
                record_observed_at = market_storage_naive_to_aware(record_observed_at)
            if record_observed_at != bound_observed_at:
                return DataQualityStatus.MISSING, False, tuple(references), (
                    "analysis_quality_binding_observed_at_changed"
                )
    quality = worst_quality(qualities)
    return quality, not quality.blocks_execution, tuple(references), None


def latest_market_state(db: Session, *, evaluated_at: datetime) -> MonitoredAnalysisState:
    row = db.scalar(
        select(MarketRegimeSnapshot)
        .order_by(MarketRegimeSnapshot.trade_date.desc(), MarketRegimeSnapshot.id.desc())
        .limit(1)
    )
    if row is None:
        return _missing("market_regime_snapshot_missing")
    quality, executable, refs, reason = _validate_bindings(
        db,
        row.quality_bindings or [],
        expected_capabilities=MARKET_STATE_CAPABILITIES,
        evaluated_at=evaluated_at,
    )
    return MonitoredAnalysisState(
        value=row.state if executable else None,
        quality_status=quality,
        executable=executable,
        observed_at=market_storage_naive_to_aware(row.observed_at),
        snapshot_hash=row.product_snapshot_hash,
        evidence_references=refs,
        blocking_reason=reason,
    )


def latest_industry_state(
    db: Session,
    *,
    industry_name: str,
    evaluated_at: datetime,
) -> MonitoredAnalysisState:
    row = db.scalar(
        select(IndustryAnalysisSnapshot)
        .where(IndustryAnalysisSnapshot.industry_name == industry_name)
        .order_by(
            IndustryAnalysisSnapshot.trade_date.desc(),
            IndustryAnalysisSnapshot.id.desc(),
        )
        .limit(1)
    )
    if row is None:
        return _missing("industry_analysis_snapshot_missing")
    quality, executable, refs, reason = _validate_bindings(
        db,
        row.quality_bindings or [],
        expected_capabilities=INDUSTRY_STATE_CAPABILITIES,
        evaluated_at=evaluated_at,
    )
    return MonitoredAnalysisState(
        value=row.classification if executable else None,
        quality_status=quality,
        executable=executable,
        observed_at=market_storage_naive_to_aware(row.observed_at),
        snapshot_hash=row.product_snapshot_hash,
        evidence_references=refs,
        blocking_reason=reason,
    )


__all__ = [
    "INDUSTRY_STATE_CAPABILITIES",
    "MARKET_STATE_CAPABILITIES",
    "MonitoredAnalysisState",
    "latest_industry_state",
    "latest_market_state",
]
