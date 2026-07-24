from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.data_hub.effective_quality import (
    EffectiveQualityResolver,
    resolve_effective_quality,
    resolve_effective_quality_batch,
)
from app.domain.quality import DataQualityStatus
from app.domain.quality_subject import (
    EffectiveQualityRequest,
    SubjectRef,
    canonical_semantic_key,
)
from app.models import DataQualityRecord, DataQualitySubjectHead


QUOTE_CAPABILITY = "market.quote.realtime"
DAILY_CAPABILITY = "market.daily.qfq"
QUOTE_SEMANTIC_KEY = "realtime/CNY"
DAILY_SEMANTIC_KEY = "qfq/CNY/share"
EVALUATED_AT = datetime(2026, 7, 24, 14, 0)


def _stock(
    symbol: str = "300502",
    semantic_key: str | None = QUOTE_SEMANTIC_KEY,
) -> SubjectRef:
    return SubjectRef(
        subject_type="stock",
        subject_id=symbol,
        semantic_key=semantic_key,
    )


def _record(
    session,
    *,
    capability: str = QUOTE_CAPABILITY,
    subject_type: str | None = "stock",
    subject_id: str | None = "300502",
    semantic_key: str | None = QUOTE_SEMANTIC_KEY,
    quality_status: str = "SINGLE_SOURCE",
    observed_at: datetime | None = EVALUATED_AT - timedelta(minutes=1),
    normalized_digest: str | None = "a" * 64,
    supersedes_record_id: int | None = None,
    trusted: bool | None = None,
    persisted: bool = False,
) -> DataQualityRecord:
    if trusted is None:
        trusted = quality_status in {"VERIFIED", "SINGLE_SOURCE"}
    record = DataQualityRecord(
        symbol=subject_id if subject_type == "stock" else None,
        capability=capability,
        subject_type=subject_type,
        subject_id=subject_id,
        semantic_key=semantic_key,
        supersedes_record_id=supersedes_record_id,
        quality_status=quality_status,
        observed_at=observed_at,
        fetched_at=EVALUATED_AT - timedelta(minutes=1),
        cached_at=EVALUATED_AT - timedelta(minutes=1) if persisted else None,
        provider_id="contract-provider",
        provider_observations=[],
        normalized_digest=normalized_digest,
        conflict_fields=["canonical_business_value"]
        if quality_status == "CONFLICTED"
        else [],
        adjustment="qfq" if capability == DAILY_CAPABILITY else None,
        price_unit="CNY",
        volume_unit="share" if capability == DAILY_CAPABILITY else None,
        row_count=1,
        fallback_used=False,
        cache_used=False,
        trusted=trusted,
        persisted=persisted,
    )
    session.add(record)
    session.commit()
    return record


def _set_raw_semantic_key(
    session,
    record: DataQualityRecord,
    semantic_key: str | None,
) -> None:
    record_id = record.id
    session.execute(
        update(DataQualityRecord)
        .where(DataQualityRecord.id == record_id)
        .values(semantic_key=semantic_key)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    session.expire_all()


def _resolve(
    session,
    record: DataQualityRecord | None,
    *,
    capability: str = QUOTE_CAPABILITY,
    subject: SubjectRef | None = None,
    evaluated_at: datetime = EVALUATED_AT,
):
    return resolve_effective_quality(
        session,
        capability=capability,
        subject=subject or _stock(),
        persisted_quality_record_id=record.id if record else None,
        observed_at=record.observed_at if record else None,
        cached_at=record.cached_at if record else None,
        evaluated_at=evaluated_at,
    )


def _attempt_resolution(
    session,
    *,
    conflict_observed_at: datetime,
    resolution_observed_at: datetime,
):
    cached = _record(
        session,
        observed_at=EVALUATED_AT - timedelta(minutes=10),
        persisted=True,
    )
    conflict = _record(
        session,
        observed_at=conflict_observed_at,
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    resolution = _record(
        session,
        observed_at=resolution_observed_at,
        quality_status="VERIFIED",
        supersedes_record_id=conflict.id,
        normalized_digest=cached.normalized_digest,
    )
    return conflict, resolution, _resolve(session, cached)


def test_subject_ref_is_hashable_serializable_and_validated():
    subject = _stock()
    assert hash(subject) == hash(_stock())
    assert subject.stable_key == ("stock", "300502", QUOTE_SEMANTIC_KEY)
    assert subject.model_dump(mode="json") == {
        "subject_type": "stock",
        "subject_id": "300502",
        "semantic_key": QUOTE_SEMANTIC_KEY,
    }
    assert SubjectRef(subject_type="index", subject_id="csi000300").subject_id == "CSI000300"
    with pytest.raises(ValidationError):
        SubjectRef(subject_type="stock", subject_id="")
    with pytest.raises(ValidationError):
        SubjectRef(subject_type="stock", subject_id="30050")
    with pytest.raises(ValidationError):
        SubjectRef(subject_type="account", subject_id="1")


def test_none_and_empty_semantic_key_share_canonical_scope(session):
    cached = _record(session, semantic_key=None, persisted=True)
    conflict = _record(
        session,
        semantic_key="",
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )

    result = _resolve(session, cached, subject=_stock(semantic_key=None))

    assert canonical_semantic_key(None) == canonical_semantic_key("")
    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id


def test_subject_head_and_record_use_same_semantic_scope(session):
    cached = _record(session, semantic_key=None, persisted=True)
    head = DataQualitySubjectHead(
        capability=QUOTE_CAPABILITY,
        subject_type="stock",
        subject_id="300502",
        semantic_key="",
        current_record_id=cached.id,
        generation=1,
    )
    session.add(head)
    session.commit()

    assert canonical_semantic_key(cached.semantic_key) == canonical_semantic_key(
        head.semantic_key
    )
    result = _resolve(session, cached, subject=_stock(semantic_key=None))
    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE


def test_semantic_key_whitespace_is_canonicalized(session):
    cached = _record(session, semantic_key=" \t ", persisted=True)
    subject = _stock(semantic_key="  ")

    result = _resolve(session, cached, subject=subject)

    assert subject.semantic_key is None
    assert canonical_semantic_key(cached.semantic_key) == ""
    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE


def test_nonempty_semantic_keys_remain_isolated(session):
    cached = _record(session, semantic_key=" realtime/CNY ", persisted=True)
    _record(
        session,
        semantic_key="latest_close/CNY",
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )

    result = _resolve(
        session,
        cached,
        subject=_stock(semantic_key="realtime/CNY"),
    )

    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE
    assert result.executable is True


def test_tab_only_conflict_blocks_none_semantic_cache(session):
    cached = _record(session, semantic_key=None, persisted=True)
    _set_raw_semantic_key(session, cached, None)
    conflict = _record(
        session,
        semantic_key="placeholder",
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    _set_raw_semantic_key(session, conflict, "\t")

    result = _resolve(session, cached, subject=_stock(semantic_key=None))

    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id
    assert result.executable is False


def test_newline_only_conflict_blocks_empty_semantic_cache(session):
    cached = _record(session, semantic_key="", persisted=True)
    _set_raw_semantic_key(session, cached, "")
    conflict = _record(
        session,
        semantic_key="placeholder",
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    _set_raw_semantic_key(session, conflict, "\n")

    result = _resolve(session, cached, subject=_stock(semantic_key=""))

    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id
    assert result.executable is False


def test_mixed_whitespace_conflict_blocks_canonical_scope(session):
    cached = _record(session, semantic_key=None, persisted=True)
    _set_raw_semantic_key(session, cached, None)
    conflict = _record(
        session,
        semantic_key="placeholder",
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    _set_raw_semantic_key(session, conflict, " \t\r\n ")

    result = _resolve(session, cached, subject=_stock(semantic_key=" "))

    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id
    assert result.executable is False


def test_nonempty_semantic_key_with_tab_padding_matches(session):
    cached = _record(
        session,
        semantic_key="realtime/CNY",
        persisted=True,
    )
    conflict = _record(
        session,
        semantic_key="placeholder",
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    _set_raw_semantic_key(session, conflict, "\trealtime/CNY\n")

    result = _resolve(
        session,
        cached,
        subject=_stock(semantic_key="  realtime/CNY  "),
    )

    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id
    assert result.executable is False


def test_different_nonempty_semantic_key_remains_isolated(session):
    cached = _record(
        session,
        semantic_key="realtime/CNY",
        persisted=True,
    )
    conflict = _record(
        session,
        semantic_key="placeholder",
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    _set_raw_semantic_key(session, conflict, "\tlatest_close/CNY\n")

    result = _resolve(
        session,
        cached,
        subject=_stock(semantic_key="realtime/CNY"),
    )

    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE
    assert result.blocking_record_id is None
    assert result.executable is True


def test_subject_head_write_normalizes_none_to_empty(session):
    record = _record(session, persisted=True)
    head = DataQualitySubjectHead(
        capability=QUOTE_CAPABILITY,
        subject_type="stock",
        subject_id="300502",
        semantic_key=None,
        current_record_id=record.id,
        generation=1,
    )
    session.add(head)
    session.flush()

    assert head.semantic_key == ""
    assert session.scalar(
        select(DataQualitySubjectHead.semantic_key).where(
            DataQualitySubjectHead.id == head.id
        )
    ) == ""


def test_subject_head_write_normalizes_whitespace_to_empty(session):
    record = _record(session, persisted=True)
    head = DataQualitySubjectHead(
        capability=QUOTE_CAPABILITY,
        subject_type="stock",
        subject_id="300502",
        semantic_key=" \t\r\n ",
        current_record_id=record.id,
        generation=1,
    )
    session.add(head)
    session.flush()

    assert head.semantic_key == ""


def test_subject_head_write_trims_nonempty_key(session):
    record = _record(session, persisted=True)
    head = DataQualitySubjectHead(
        capability=QUOTE_CAPABILITY,
        subject_type="stock",
        subject_id="300502",
        semantic_key="\trealtime/CNY\n",
        current_record_id=record.id,
        generation=1,
    )
    session.add(head)
    session.flush()

    assert head.semantic_key == "realtime/CNY"
    head.semantic_key = "\tlatest_close/CNY\n"
    session.flush()
    assert head.semantic_key == "latest_close/CNY"
    assert session.scalar(
        select(DataQualitySubjectHead.semantic_key).where(
            DataQualitySubjectHead.id == head.id
        )
    ) == "latest_close/CNY"


def test_quality_record_write_uses_same_semantic_normalizer(session):
    empty = _record(session, semantic_key="\t\r\n")
    nonempty = _record(
        session,
        subject_id="600000",
        semantic_key="  realtime/CNY  ",
    )

    assert empty.semantic_key == ""
    assert nonempty.semantic_key == "realtime/CNY"
    assert canonical_semantic_key(empty.semantic_key) == canonical_semantic_key(
        DataQualitySubjectHead(semantic_key=None).semantic_key
    )


def test_subject_head_canonical_scope_is_unique(session):
    record = _record(session, persisted=True)
    first = DataQualitySubjectHead(
        capability=QUOTE_CAPABILITY,
        subject_type="stock",
        subject_id="300502",
        semantic_key=None,
        current_record_id=record.id,
        generation=1,
    )
    session.add(first)
    session.commit()
    assert first.semantic_key == ""

    duplicate = DataQualitySubjectHead(
        capability=QUOTE_CAPABILITY,
        subject_type="stock",
        subject_id="300502",
        semantic_key="\t",
        current_record_id=record.id,
        generation=2,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_distinct_nonempty_subject_heads_can_coexist(session):
    record = _record(session, persisted=True)
    session.add_all(
        [
            DataQualitySubjectHead(
                capability=QUOTE_CAPABILITY,
                subject_type="stock",
                subject_id="300502",
                semantic_key=" realtime/CNY ",
                current_record_id=record.id,
                generation=1,
            ),
            DataQualitySubjectHead(
                capability=QUOTE_CAPABILITY,
                subject_type="stock",
                subject_id="300502",
                semantic_key="\tlatest_close/CNY\n",
                current_record_id=record.id,
                generation=1,
            ),
        ]
    )
    session.flush()

    keys = session.scalars(
        select(DataQualitySubjectHead.semantic_key).order_by(
            DataQualitySubjectHead.semantic_key
        )
    ).all()
    assert keys == ["latest_close/CNY", "realtime/CNY"]


def test_legacy_unlinked_cache_is_missing(session):
    result = _resolve(session, None)
    assert result.effective_quality == DataQualityStatus.MISSING
    assert result.executable is False
    assert result.requires_refresh is True
    assert result.blocking_reason == "missing_lineage"


def test_record_capability_mismatch_is_missing(session):
    record = _record(session, persisted=True)
    result = _resolve(session, record, capability=DAILY_CAPABILITY)
    assert result.effective_quality == DataQualityStatus.MISSING
    assert result.blocking_reason == "lineage_scope_mismatch"


def test_record_subject_mismatch_is_missing(session):
    record = _record(session, persisted=True)
    result = _resolve(session, record, subject=_stock("600000"))
    assert result.effective_quality == DataQualityStatus.MISSING
    assert result.blocking_reason == "lineage_scope_mismatch"


def test_single_source_quote_becomes_stale_with_time(session):
    observed_at = datetime(2026, 7, 24, 10, 0)
    record = _record(session, observed_at=observed_at, persisted=True)
    fresh = _resolve(session, record, evaluated_at=observed_at + timedelta(minutes=30))
    stale = _resolve(session, record, evaluated_at=observed_at + timedelta(minutes=31))
    assert fresh.effective_quality == DataQualityStatus.SINGLE_SOURCE
    assert stale.freshness_quality == DataQualityStatus.STALE
    assert stale.effective_quality == DataQualityStatus.STALE
    assert stale.executable is False


def test_daily_freshness_uses_trading_calendar(session):
    friday = datetime(2026, 7, 24, 15, 0)
    saturday = datetime(2026, 7, 25, 12, 0)
    record = _record(
        session,
        capability=DAILY_CAPABILITY,
        semantic_key=DAILY_SEMANTIC_KEY,
        observed_at=friday,
        persisted=True,
    )
    result = _resolve(
        session,
        record,
        capability=DAILY_CAPABILITY,
        subject=_stock(semantic_key=DAILY_SEMANTIC_KEY),
        evaluated_at=saturday,
    )
    assert result.freshness_quality == DataQualityStatus.VERIFIED
    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE


def test_future_observed_at_is_blocking(session):
    record = _record(
        session,
        observed_at=EVALUATED_AT + timedelta(minutes=6),
        persisted=True,
    )
    result = _resolve(session, record)
    assert result.effective_quality == DataQualityStatus.MISSING
    assert result.executable is False
    assert result.requires_refresh is True
    assert result.blocking_reason == "observed_at_in_future"


def test_new_conflict_blocks_old_trusted_cache(session):
    cached = _record(session, persisted=True)
    conflict = _record(
        session,
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    result = _resolve(session, cached)
    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.newest_signal_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id
    assert result.executable is False


def test_unrelated_subject_conflict_is_ignored(session):
    cached = _record(session, persisted=True)
    _record(
        session,
        subject_id="600000",
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    result = _resolve(session, cached)
    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE
    assert result.newest_signal_quality is None


def test_unrelated_capability_conflict_is_ignored(session):
    cached = _record(session, persisted=True)
    _record(
        session,
        capability=DAILY_CAPABILITY,
        semantic_key=DAILY_SEMANTIC_KEY,
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    result = _resolve(session, cached)
    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE
    assert result.newest_signal_quality is None


def test_refresh_failure_does_not_invalidate_fresh_cache(session):
    cached = _record(session, persisted=True)
    _record(
        session,
        quality_status="MISSING",
        observed_at=None,
        normalized_digest=None,
    )
    result = _resolve(session, cached)
    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE
    assert result.executable is True
    assert result.newest_signal_quality is None


def test_refresh_failure_without_cache_is_missing(session):
    failure = _record(
        session,
        quality_status="MISSING",
        observed_at=None,
        normalized_digest=None,
    )
    result = _resolve(session, failure)
    assert result.effective_quality == DataQualityStatus.MISSING
    assert result.executable is False
    assert result.requires_refresh is True


def test_cache_reread_does_not_resolve_conflict(session):
    cached = _record(session, persisted=True)
    conflict = _record(
        session,
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    first = _resolve(session, cached, evaluated_at=EVALUATED_AT)
    second = resolve_effective_quality(
        session,
        capability=QUOTE_CAPABILITY,
        subject=_stock(),
        persisted_quality_record_id=cached.id,
        observed_at=cached.observed_at,
        cached_at=EVALUATED_AT + timedelta(minutes=1),
        evaluated_at=EVALUATED_AT + timedelta(minutes=1),
    )
    assert first.blocking_record_id == conflict.id
    assert second.blocking_record_id == conflict.id
    assert second.effective_quality == DataQualityStatus.CONFLICTED


def test_rewritten_cache_does_not_resolve_conflict(session):
    cached = _record(session, persisted=True)
    conflict = _record(
        session,
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    rewritten = _record(
        session,
        observed_at=cached.observed_at,
        normalized_digest=cached.normalized_digest,
        persisted=True,
    )

    result = _resolve(session, rewritten)

    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id
    assert result.blocking_reason == "newer_conflict"


def test_later_single_source_does_not_resolve_conflict(session):
    cached = _record(session, persisted=True)
    conflict = _record(
        session,
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    _record(
        session,
        supersedes_record_id=conflict.id,
        normalized_digest=cached.normalized_digest,
    )
    result = _resolve(session, cached)
    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id


def test_verified_explicit_supersession_resolves_conflict(session):
    cached = _record(session, persisted=True)
    conflict = _record(
        session,
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    resolution = _record(
        session,
        quality_status="VERIFIED",
        supersedes_record_id=conflict.id,
        normalized_digest=cached.normalized_digest,
    )
    result = _resolve(session, cached)
    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE
    assert result.newest_signal_quality is None
    assert result.blocking_record_id is None
    assert result.requires_refresh is False
    assert resolution.id in result.source_quality_record_ids


def test_stale_verified_supersession_does_not_resolve_conflict(session):
    conflict, _, result = _attempt_resolution(
        session,
        conflict_observed_at=EVALUATED_AT - timedelta(minutes=40),
        resolution_observed_at=EVALUATED_AT - timedelta(minutes=31),
    )

    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id
    assert result.executable is False


def test_future_verified_supersession_does_not_resolve_conflict(session):
    conflict, _, result = _attempt_resolution(
        session,
        conflict_observed_at=EVALUATED_AT - timedelta(minutes=2),
        resolution_observed_at=EVALUATED_AT + timedelta(minutes=1),
    )

    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id
    assert result.executable is False


def test_fresh_verified_supersession_can_resolve_conflict(session):
    _, resolution, result = _attempt_resolution(
        session,
        conflict_observed_at=EVALUATED_AT - timedelta(minutes=2),
        resolution_observed_at=EVALUATED_AT - timedelta(minutes=1),
    )

    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE
    assert result.blocking_record_id is None
    assert result.executable is True
    assert resolution.id in result.source_quality_record_ids


def test_resolution_observed_before_conflict_does_not_resolve(session):
    conflict, _, result = _attempt_resolution(
        session,
        conflict_observed_at=EVALUATED_AT - timedelta(minutes=2),
        resolution_observed_at=EVALUATED_AT - timedelta(minutes=3),
    )

    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id


def test_resolution_observed_equal_to_conflict_can_resolve(session):
    observed_at = EVALUATED_AT - timedelta(minutes=2)
    _, _, result = _attempt_resolution(
        session,
        conflict_observed_at=observed_at,
        resolution_observed_at=observed_at,
    )

    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE
    assert result.executable is True


def test_resolution_observed_after_conflict_can_resolve(session):
    _, _, result = _attempt_resolution(
        session,
        conflict_observed_at=EVALUATED_AT - timedelta(minutes=2),
        resolution_observed_at=EVALUATED_AT - timedelta(minutes=1),
    )

    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE
    assert result.executable is True


def test_later_record_id_with_older_business_time_does_not_resolve(session):
    conflict, resolution, result = _attempt_resolution(
        session,
        conflict_observed_at=EVALUATED_AT - timedelta(minutes=5),
        resolution_observed_at=EVALUATED_AT - timedelta(minutes=6),
    )

    assert resolution.id > conflict.id
    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id


def test_resolved_conflict_does_not_pin_later_business_value(session):
    cached = _record(
        session,
        observed_at=EVALUATED_AT - timedelta(minutes=10),
        persisted=True,
    )
    conflict = _record(
        session,
        observed_at=EVALUATED_AT - timedelta(minutes=9),
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    _record(
        session,
        observed_at=EVALUATED_AT - timedelta(minutes=8),
        quality_status="VERIFIED",
        supersedes_record_id=conflict.id,
        normalized_digest=cached.normalized_digest,
    )
    later = _record(
        session,
        observed_at=EVALUATED_AT - timedelta(minutes=1),
        normalized_digest="c" * 64,
        persisted=True,
    )

    result = _resolve(session, later)

    assert result.effective_quality == DataQualityStatus.SINGLE_SOURCE
    assert result.executable is True
    assert result.requires_refresh is False


def test_verified_resolution_without_observed_at_does_not_resolve_conflict(session):
    cached = _record(session, persisted=True)
    conflict = _record(
        session,
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    _record(
        session,
        quality_status="VERIFIED",
        observed_at=None,
        supersedes_record_id=conflict.id,
        normalized_digest=cached.normalized_digest,
    )

    result = _resolve(session, cached)

    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id


def test_unlinked_verified_record_does_not_resolve_conflict(session):
    cached = _record(session, persisted=True)
    conflict = _record(
        session,
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    _record(
        session,
        quality_status="VERIFIED",
        normalized_digest=cached.normalized_digest,
    )
    result = _resolve(session, cached)
    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id


def test_semantic_key_mismatch_does_not_resolve_conflict(session):
    cached = _record(session, persisted=True)
    conflict = _record(
        session,
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    _record(
        session,
        semantic_key="latest_close/CNY",
        quality_status="VERIFIED",
        supersedes_record_id=conflict.id,
        normalized_digest=cached.normalized_digest,
    )
    result = _resolve(session, cached)
    assert result.effective_quality == DataQualityStatus.CONFLICTED
    assert result.blocking_record_id == conflict.id


def test_resolution_with_different_digest_requires_refresh(session):
    cached = _record(session, persisted=True)
    conflict = _record(
        session,
        quality_status="CONFLICTED",
        normalized_digest="b" * 64,
    )
    resolution = _record(
        session,
        quality_status="VERIFIED",
        supersedes_record_id=conflict.id,
        normalized_digest="c" * 64,
    )
    result = _resolve(session, cached)
    assert result.effective_quality == DataQualityStatus.MISSING
    assert result.blocking_record_id == resolution.id
    assert result.blocking_reason == "resolved_value_changed"
    assert result.requires_refresh is True


def test_batch_exact_duplicate_request_is_deterministic(session):
    record = _record(session, persisted=True)
    request = EffectiveQualityRequest(
        capability=QUOTE_CAPABILITY,
        subject=_stock(),
        persisted_quality_record_id=record.id,
        observed_at=record.observed_at,
        cached_at=record.cached_at,
    )

    batch = resolve_effective_quality_batch(
        session,
        [request, request],
        evaluated_at=EVALUATED_AT,
    )
    single = EffectiveQualityResolver(session).resolve(
        request,
        evaluated_at=EVALUATED_AT,
    )

    assert batch == {request.key: single}


def test_batch_duplicate_key_with_different_observed_at_is_rejected(session):
    record = _record(session, persisted=True)
    request = EffectiveQualityRequest(
        capability=QUOTE_CAPABILITY,
        subject=_stock(),
        persisted_quality_record_id=record.id,
        observed_at=record.observed_at,
        cached_at=record.cached_at,
    )
    conflicting = request.model_copy(
        update={"observed_at": record.observed_at - timedelta(seconds=1)}
    )

    with pytest.raises(
        ValueError,
        match="duplicate_quality_key_with_conflicting_context",
    ):
        resolve_effective_quality_batch(
            session,
            [request, conflicting],
            evaluated_at=EVALUATED_AT,
        )


def test_batch_duplicate_key_with_different_cached_context_is_rejected(session):
    record = _record(session, persisted=True)
    request = EffectiveQualityRequest(
        capability=QUOTE_CAPABILITY,
        subject=_stock(),
        persisted_quality_record_id=record.id,
        observed_at=record.observed_at,
        cached_at=record.cached_at,
    )
    conflicting = request.model_copy(
        update={"cached_at": record.cached_at + timedelta(seconds=1)}
    )

    with pytest.raises(
        ValueError,
        match="duplicate_quality_key_with_conflicting_context",
    ):
        resolve_effective_quality_batch(
            session,
            [request, conflicting],
            evaluated_at=EVALUATED_AT,
        )


def test_batch_unique_requests_match_single_resolver(session):
    first = _record(session, persisted=True)
    second = _record(
        session,
        subject_id="600000",
        observed_at=EVALUATED_AT - timedelta(minutes=2),
        normalized_digest="d" * 64,
        persisted=True,
    )
    requests = [
        EffectiveQualityRequest(
            capability=QUOTE_CAPABILITY,
            subject=_stock(),
            persisted_quality_record_id=first.id,
            observed_at=first.observed_at,
            cached_at=first.cached_at,
        ),
        EffectiveQualityRequest(
            capability=QUOTE_CAPABILITY,
            subject=_stock("600000"),
            persisted_quality_record_id=second.id,
            observed_at=second.observed_at,
            cached_at=second.cached_at,
        ),
    ]
    batch = resolve_effective_quality_batch(
        session,
        requests,
        evaluated_at=EVALUATED_AT,
    )
    resolver = EffectiveQualityResolver(session)
    for request in requests:
        single = resolver.resolve(request, evaluated_at=EVALUATED_AT)
        assert batch[request.key] == single


def test_resolver_does_not_write_database(session):
    cached = _record(session, persisted=True)
    session.add(
        DataQualitySubjectHead(
            capability=QUOTE_CAPABILITY,
            subject_type="stock",
            subject_id="300502",
            semantic_key=QUOTE_SEMANTIC_KEY,
            current_record_id=cached.id,
            generation=1,
        )
    )
    session.commit()
    record_count = session.scalar(select(func.count(DataQualityRecord.id)))
    head_count = session.scalar(select(func.count(DataQualitySubjectHead.id)))
    result = _resolve(session, cached)
    assert result.executable is True
    assert session.scalar(select(func.count(DataQualityRecord.id))) == record_count
    assert session.scalar(select(func.count(DataQualitySubjectHead.id))) == head_count
    assert not session.new
    assert not session.dirty
    assert not session.deleted


def test_sector_subject_requires_stable_identifier():
    with pytest.raises(ValidationError):
        SubjectRef(subject_type="sector", subject_id=" ")
    subject = SubjectRef(
        subject_type="sector",
        subject_id="sw2-270000",
        semantic_key="通信设备",
    )
    assert subject.stable_key == ("sector", "sw2-270000", "通信设备")
