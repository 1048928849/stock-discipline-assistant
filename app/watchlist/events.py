from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data_hub.trading_calendar import to_utc_storage_naive
from app.models import MonitoringEvent, ReanalysisRequest, WatchlistItem
from app.watchlist.contracts import EventSeverity, MonitoringRuleType
from app.watchlist.service import utc_now_naive


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("event observed_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def dedupe_key(
    *,
    item_id: int,
    revision: int,
    kind: str,
    observed_at: datetime,
    cooldown_seconds: int,
) -> str:
    if cooldown_seconds < 1:
        raise ValueError("cooldown_seconds must be positive")
    bucket = int(_aware_utc(observed_at).timestamp()) // cooldown_seconds
    raw = f"{item_id}:{revision}:{kind}:{bucket}".encode()
    return hashlib.sha256(raw).hexdigest()


def create_event_once(
    db: Session,
    item: WatchlistItem,
    *,
    event_type: MonitoringRuleType | str,
    severity: EventSeverity | str,
    title: str,
    reason_codes: list[str],
    observed_at: datetime,
    payload: dict,
    reanalysis_required: bool,
    cooldown_seconds: int,
) -> tuple[MonitoringEvent, bool]:
    event_value = event_type.value if isinstance(event_type, MonitoringRuleType) else event_type
    severity_value = severity.value if isinstance(severity, EventSeverity) else severity
    key = dedupe_key(
        item_id=item.id,
        revision=item.revision,
        kind=event_value,
        observed_at=observed_at,
        cooldown_seconds=cooldown_seconds,
    )
    existing = db.scalar(select(MonitoringEvent).where(MonitoringEvent.dedupe_key == key))
    if existing:
        return existing, False
    row = MonitoringEvent(
        watchlist_item_id=item.id,
        revision_number=item.revision,
        event_type=event_value,
        severity=severity_value,
        title=title[:300],
        reason_codes=reason_codes,
        observed_at=to_utc_storage_naive(observed_at),
        created_at=utc_now_naive(),
        dedupe_key=key,
        payload=payload,
        reanalysis_required=reanalysis_required,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(MonitoringEvent).where(MonitoringEvent.dedupe_key == key)
        )
        if existing is None:
            raise
        return existing, False
    return row, True


def create_reanalysis_request_once(
    db: Session,
    item: WatchlistItem,
    *,
    reason_codes: list[str],
    observed_at: datetime,
    cooldown_seconds: int,
) -> tuple[ReanalysisRequest, bool]:
    key = dedupe_key(
        item_id=item.id,
        revision=item.revision,
        kind="REANALYSIS:" + ",".join(sorted(reason_codes)),
        observed_at=observed_at,
        cooldown_seconds=cooldown_seconds,
    )
    existing = db.scalar(
        select(ReanalysisRequest).where(ReanalysisRequest.dedupe_key == key)
    )
    if existing:
        return existing, False
    row = ReanalysisRequest(
        watchlist_item_id=item.id,
        revision_number=item.revision,
        reason_codes=reason_codes,
        status="PENDING",
        dedupe_key=key,
        requested_at=to_utc_storage_naive(observed_at),
        created_at=utc_now_naive(),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(ReanalysisRequest).where(ReanalysisRequest.dedupe_key == key)
        )
        if existing is None:
            raise
        return existing, False
    return row, True


__all__ = ["create_event_once", "create_reanalysis_request_once", "dedupe_key"]
