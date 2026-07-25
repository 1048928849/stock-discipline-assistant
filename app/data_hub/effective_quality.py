from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.data_hub.quality import observation_is_stale, policy_for
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    shanghai_now,
    storage_naive_to_aware,
    time_storage_semantics_for_capability,
    to_shanghai_aware,
)
from app.domain.quality import DataQualityStatus, worst_quality
from app.domain.quality_subject import (
    EffectiveQualityRequest,
    EffectiveQualityResult,
    QualityKey,
    SubjectRef,
    canonical_semantic_key,
)
from app.models import DataQualityRecord


MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
_TRUSTED = {DataQualityStatus.VERIFIED, DataQualityStatus.SINGLE_SOURCE}
_QUERY_SCOPE_CHUNK = 100
_ScopeKey = tuple[str, str, str, str]
_SubjectScopeKey = tuple[str, str, str]


def _quality(value: str | None) -> DataQualityStatus | None:
    try:
        return DataQualityStatus(value) if value is not None else None
    except ValueError:
        return None


def _scope_key(capability: str, subject: SubjectRef) -> _ScopeKey:
    return (
        capability,
        subject.subject_type,
        subject.subject_id,
        canonical_semantic_key(subject.semantic_key),
    )


def _record_scope(record: DataQualityRecord) -> _ScopeKey:
    return (
        record.capability,
        record.subject_type or "",
        record.subject_id or "",
        canonical_semantic_key(record.semantic_key),
    )


def _subject_scope_key(capability: str, subject: SubjectRef) -> _SubjectScopeKey:
    return (
        capability,
        subject.subject_type,
        subject.subject_id,
    )


def _scope_matches(
    record: DataQualityRecord,
    capability: str,
    subject: SubjectRef,
) -> bool:
    return _record_scope(record) == _scope_key(capability, subject)


def _comparable_datetime(value: datetime | date, capability: str) -> datetime:
    result = (
        value
        if isinstance(value, datetime)
        else datetime.combine(value, time.min)
    )
    return storage_naive_to_aware(
        result,
        semantics=time_storage_semantics_for_capability(capability),
    )


def _conservative_observed_at(
    stored: datetime | date | None,
    supplied: datetime | date | None,
    evaluated_at: datetime,
    capability: str,
) -> datetime | date | None:
    if stored is None:
        return supplied
    if supplied is None:
        return stored
    stored_dt = _comparable_datetime(stored, capability)
    supplied_dt = _comparable_datetime(supplied, capability)
    return stored if stored_dt <= supplied_dt else supplied


def _resolution_applies_to_observation(
    resolution_observed_at: datetime | date | None,
    cached_observed_at: datetime | date | None,
    evaluated_at: datetime,
    capability: str,
) -> bool:
    if resolution_observed_at is None or cached_observed_at is None:
        return True
    del evaluated_at
    return _comparable_datetime(
        resolution_observed_at,
        capability,
    ) >= _comparable_datetime(cached_observed_at, capability)


def _resolution_business_time_is_valid(
    resolution_observed_at: datetime | date | None,
    conflict_observed_at: datetime | date | None,
    evaluated_at: datetime,
    capability: str,
) -> bool:
    if resolution_observed_at is None or conflict_observed_at is None:
        return False
    resolution_dt = _comparable_datetime(resolution_observed_at, capability)
    conflict_dt = _comparable_datetime(conflict_observed_at, capability)
    evaluated_dt = to_shanghai_aware(evaluated_at)
    return conflict_dt <= resolution_dt <= evaluated_dt


def _freshness_quality(
    observed_at: datetime | date | None,
    capability: str,
    evaluated_at: datetime,
    calendar: TradingCalendar,
) -> tuple[DataQualityStatus, str | None]:
    if observed_at is None:
        return DataQualityStatus.MISSING, "missing_observed_at"
    observed_dt = _comparable_datetime(observed_at, capability)
    evaluated_dt = to_shanghai_aware(evaluated_at)
    if observed_dt - evaluated_dt > MAX_FUTURE_CLOCK_SKEW:
        return DataQualityStatus.MISSING, "observed_at_in_future"
    if observation_is_stale(
        observed_at,
        policy_for(capability),
        now=evaluated_at,
        calendar=calendar,
    ):
        return DataQualityStatus.STALE, "observation_stale"
    return DataQualityStatus.VERIFIED, None


def _missing_result(
    request: EffectiveQualityRequest,
    evaluated_at: datetime,
    *,
    stored_quality: DataQualityStatus | None = None,
    observed_at: datetime | date | None = None,
    blocking_record_id: int | None = None,
    blocking_reason: str,
    source_quality_record_ids: list[int] | None = None,
) -> EffectiveQualityResult:
    return EffectiveQualityResult(
        capability=request.capability,
        subject_type=request.subject.subject_type,
        subject_id=request.subject.subject_id,
        semantic_key=request.subject.semantic_key,
        stored_quality=stored_quality,
        freshness_quality=DataQualityStatus.MISSING,
        newest_signal_quality=None,
        effective_quality=DataQualityStatus.MISSING,
        executable=False,
        observed_at=observed_at,
        evaluated_at=evaluated_at,
        blocking_record_id=blocking_record_id,
        blocking_reason=blocking_reason,
        source_quality_record_ids=source_quality_record_ids or [],
        requires_refresh=True,
    )


class EffectiveQualityResolver:
    """Read-only point-of-use quality evaluation for persisted cache lineage."""

    def __init__(
        self,
        db: Session,
        *,
        calendar: TradingCalendar | None = None,
    ):
        self.db = db
        self.calendar = calendar or get_trading_calendar()

    def resolve(
        self,
        request: EffectiveQualityRequest,
        *,
        evaluated_at: datetime | None = None,
    ) -> EffectiveQualityResult:
        return self.resolve_batch(
            [request],
            evaluated_at=evaluated_at,
        )[request.key]

    def resolve_batch(
        self,
        requests: Sequence[EffectiveQualityRequest],
        *,
        evaluated_at: datetime | None = None,
    ) -> dict[QualityKey, EffectiveQualityResult]:
        current = to_shanghai_aware(
            evaluated_at or shanghai_now(),
            naive_is_shanghai=evaluated_at is not None and evaluated_at.tzinfo is None,
        )
        if not requests:
            return {}

        unique_requests: dict[QualityKey, EffectiveQualityRequest] = {}
        for request in requests:
            existing = unique_requests.get(request.key)
            if existing is not None and existing != request:
                raise ValueError("duplicate_quality_key_with_conflicting_context")
            unique_requests[request.key] = request
        resolved_requests = list(unique_requests.values())

        record_ids = {
            request.persisted_quality_record_id
            for request in resolved_requests
            if request.persisted_quality_record_id is not None
        }
        with self.db.no_autoflush:
            base_records = {
                record.id: record
                for record in self.db.scalars(
                    select(DataQualityRecord).where(DataQualityRecord.id.in_(record_ids))
                ).all()
            } if record_ids else {}

            subject_scopes: set[_SubjectScopeKey] = set()
            for request in resolved_requests:
                record = base_records.get(request.persisted_quality_record_id)
                if record is None or not _scope_matches(
                    record,
                    request.capability,
                    request.subject,
                ):
                    continue
                subject_scopes.add(
                    _subject_scope_key(request.capability, request.subject)
                )

            scoped_records: dict[_ScopeKey, list[DataQualityRecord]] = defaultdict(list)
            scope_items = list(subject_scopes)
            for offset in range(0, len(scope_items), _QUERY_SCOPE_CHUNK):
                conditions = []
                for scope in scope_items[offset : offset + _QUERY_SCOPE_CHUNK]:
                    capability, subject_type, subject_id = scope
                    conditions.append(
                        and_(
                            DataQualityRecord.capability == capability,
                            DataQualityRecord.subject_type == subject_type,
                            DataQualityRecord.subject_id == subject_id,
                        )
                    )
                if not conditions:
                    continue
                rows = self.db.scalars(
                    select(DataQualityRecord)
                    .where(or_(*conditions))
                    .order_by(DataQualityRecord.id)
                ).all()
                for record in rows:
                    scoped_records[_record_scope(record)].append(record)

        results = {}
        for request in resolved_requests:
            record = base_records.get(request.persisted_quality_record_id)
            scope = _scope_key(request.capability, request.subject)
            results[request.key] = self._resolve_loaded(
                request,
                record,
                scoped_records.get(scope, []),
                current,
            )
        return results

    def _resolve_loaded(
        self,
        request: EffectiveQualityRequest,
        record: DataQualityRecord | None,
        scope_records: list[DataQualityRecord],
        evaluated_at: datetime,
    ) -> EffectiveQualityResult:
        if request.persisted_quality_record_id is None:
            return _missing_result(
                request,
                evaluated_at,
                blocking_reason="missing_lineage",
            )
        if record is None:
            return _missing_result(
                request,
                evaluated_at,
                blocking_record_id=request.persisted_quality_record_id,
                blocking_reason="lineage_record_not_found",
            )

        stored_quality = _quality(record.quality_status)
        if not _scope_matches(record, request.capability, request.subject):
            return _missing_result(
                request,
                evaluated_at,
                stored_quality=stored_quality,
                observed_at=record.observed_at,
                blocking_record_id=record.id,
                blocking_reason="lineage_scope_mismatch",
                source_quality_record_ids=[record.id],
            )
        if not record.persisted:
            return _missing_result(
                request,
                evaluated_at,
                stored_quality=stored_quality,
                observed_at=record.observed_at,
                blocking_record_id=record.id,
                blocking_reason="lineage_not_persisted",
                source_quality_record_ids=[record.id],
            )
        if stored_quality is None:
            return _missing_result(
                request,
                evaluated_at,
                observed_at=record.observed_at,
                blocking_record_id=record.id,
                blocking_reason="invalid_stored_quality",
                source_quality_record_ids=[record.id],
            )
        if stored_quality in _TRUSTED and not record.trusted:
            return _missing_result(
                request,
                evaluated_at,
                stored_quality=stored_quality,
                observed_at=record.observed_at,
                blocking_record_id=record.id,
                blocking_reason="lineage_not_trusted",
                source_quality_record_ids=[record.id],
            )

        observed_at = _conservative_observed_at(
            record.observed_at,
            request.observed_at,
            evaluated_at,
            request.capability,
        )
        freshness_quality, freshness_reason = _freshness_quality(
            observed_at,
            request.capability,
            evaluated_at,
            self.calendar,
        )
        source_ids = sorted(
            {
                record.id,
                *(
                    item.id
                    for item in scope_records
                    if item.id >= record.id
                ),
            }
        )

        policy = policy_for(request.capability)
        resolutions_by_blocker: dict[int, list[DataQualityRecord]] = defaultdict(list)
        for item in scope_records:
            if item.supersedes_record_id is not None:
                resolutions_by_blocker[item.supersedes_record_id].append(item)

        unresolved_conflicts: list[DataQualityRecord] = []
        changed_resolutions: list[DataQualityRecord] = []
        for conflict in scope_records:
            if _quality(conflict.quality_status) != DataQualityStatus.CONFLICTED:
                continue
            accepted = []
            for candidate in resolutions_by_blocker.get(conflict.id, []):
                candidate_quality = _quality(candidate.quality_status)
                candidate_freshness, _ = _freshness_quality(
                    candidate.observed_at,
                    request.capability,
                    evaluated_at,
                    self.calendar,
                )
                allowed = (
                    {DataQualityStatus.VERIFIED}
                    if policy.verify_multiple_sources
                    else _TRUSTED
                )
                if (
                    candidate.id > conflict.id
                    and candidate_quality in allowed
                    and candidate_freshness == DataQualityStatus.VERIFIED
                    and _resolution_business_time_is_valid(
                        candidate.observed_at,
                        conflict.observed_at,
                        evaluated_at,
                        request.capability,
                    )
                    and candidate.trusted
                ):
                    accepted.append(candidate)
            if not accepted:
                unresolved_conflicts.append(conflict)
                continue
            resolution = max(accepted, key=lambda item: item.id)
            if (
                _resolution_applies_to_observation(
                    resolution.observed_at,
                    observed_at,
                    evaluated_at,
                    request.capability,
                )
                and (
                    record.normalized_digest is None
                    or resolution.normalized_digest is None
                    or resolution.normalized_digest != record.normalized_digest
                )
            ):
                changed_resolutions.append(resolution)

        if unresolved_conflicts:
            blocker = max(unresolved_conflicts, key=lambda item: item.id)
            effective = worst_quality(
                [
                    stored_quality,
                    freshness_quality,
                    DataQualityStatus.CONFLICTED,
                ]
            )
            return EffectiveQualityResult(
                capability=request.capability,
                subject_type=request.subject.subject_type,
                subject_id=request.subject.subject_id,
                semantic_key=request.subject.semantic_key,
                stored_quality=stored_quality,
                freshness_quality=freshness_quality,
                newest_signal_quality=DataQualityStatus.CONFLICTED,
                effective_quality=effective,
                executable=False,
                observed_at=observed_at,
                evaluated_at=evaluated_at,
                blocking_record_id=blocker.id,
                blocking_reason="newer_conflict",
                source_quality_record_ids=source_ids,
                requires_refresh=True,
            )

        if changed_resolutions:
            blocker = max(changed_resolutions, key=lambda item: item.id)
            reason = (
                "resolution_digest_unproven"
                if record.normalized_digest is None or blocker.normalized_digest is None
                else "resolved_value_changed"
            )
            return _missing_result(
                request,
                evaluated_at,
                stored_quality=stored_quality,
                observed_at=observed_at,
                blocking_record_id=blocker.id,
                blocking_reason=reason,
                source_quality_record_ids=source_ids,
            )

        effective = worst_quality([stored_quality, freshness_quality])
        blocking_reason = None
        blocking_record_id = None
        if effective.blocks_execution:
            blocking_record_id = record.id
            blocking_reason = (
                freshness_reason
                if freshness_quality.blocks_execution
                else f"stored_quality_{stored_quality.value.lower()}"
            )
        return EffectiveQualityResult(
            capability=request.capability,
            subject_type=request.subject.subject_type,
            subject_id=request.subject.subject_id,
            semantic_key=request.subject.semantic_key,
            stored_quality=stored_quality,
            freshness_quality=freshness_quality,
            newest_signal_quality=None,
            effective_quality=effective,
            executable=not effective.blocks_execution,
            observed_at=observed_at,
            evaluated_at=evaluated_at,
            blocking_record_id=blocking_record_id,
            blocking_reason=blocking_reason,
            source_quality_record_ids=source_ids,
            requires_refresh=effective.blocks_execution,
        )


def resolve_effective_quality(
    db: Session,
    *,
    capability: str,
    subject: SubjectRef,
    persisted_quality_record_id: int | None = None,
    observed_at: datetime | date | None = None,
    cached_at: datetime | None = None,
    evaluated_at: datetime | None = None,
) -> EffectiveQualityResult:
    request = EffectiveQualityRequest(
        capability=capability,
        subject=subject,
        persisted_quality_record_id=persisted_quality_record_id,
        observed_at=observed_at,
        cached_at=cached_at,
    )
    return EffectiveQualityResolver(db).resolve(request, evaluated_at=evaluated_at)


def resolve_effective_quality_batch(
    db: Session,
    requests: Sequence[EffectiveQualityRequest],
    *,
    evaluated_at: datetime | None = None,
) -> dict[QualityKey, EffectiveQualityResult]:
    return EffectiveQualityResolver(db).resolve_batch(
        requests,
        evaluated_at=evaluated_at,
    )


__all__ = [
    "EffectiveQualityResolver",
    "MAX_FUTURE_CLOCK_SKEW",
    "resolve_effective_quality",
    "resolve_effective_quality_batch",
]
