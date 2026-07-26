from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import AppError
from app.models import (
    MonitoringEvent,
    WatchlistItem,
    WatchlistRevision,
    WatchlistTransition,
)
from app.watchlist.contracts import (
    WatchlistCreateRequest,
    WatchlistPatchRequest,
    WatchlistScanRequest,
)
from app.watchlist.events import create_reanalysis_request_once
from app.watchlist.monitoring import WatchlistMonitoringService
from app.watchlist.reanalysis import execute_reanalysis
from app.watchlist.service import (
    archive_watchlist_item,
    create_watchlist_item,
    patch_watchlist_item,
    serialize_item,
    utc_now_naive,
)


router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


def _item(db: Session, item_id: int) -> WatchlistItem:
    item = db.get(WatchlistItem, item_id)
    if item is None:
        raise AppError(404, "WATCHLIST_ITEM_NOT_FOUND", "观察项不存在")
    return item


@router.post("/items", status_code=201)
def create_item(
    payload: WatchlistCreateRequest,
    db: Session = Depends(get_db),
):
    return serialize_item(db, create_watchlist_item(db, payload))


@router.get("/items")
def list_items(
    status: str | None = Query(default=None, max_length=30),
    monitoring_health: str | None = Query(default=None, max_length=30),
    db: Session = Depends(get_db),
):
    query = select(WatchlistItem)
    if status:
        query = query.where(WatchlistItem.status == status)
    if monitoring_health:
        query = query.where(WatchlistItem.monitoring_health == monitoring_health)
    rows = db.scalars(query.order_by(WatchlistItem.updated_at.desc())).all()
    return [serialize_item(db, item) for item in rows]


@router.get("/items/{item_id}")
def get_item(item_id: int, db: Session = Depends(get_db)):
    item = _item(db, item_id)
    result = serialize_item(db, item)
    result["latest_decision_package"] = None
    if item.latest_analysis_id:
        from app.models import PlanAnalysisRun

        analysis = db.get(PlanAnalysisRun, item.latest_analysis_id)
        if analysis and analysis.result_snapshot:
            result["latest_decision_package"] = analysis.result_snapshot.get(
                "decision_package"
            )
    return result


@router.patch("/items/{item_id}")
def patch_item(
    item_id: int,
    payload: WatchlistPatchRequest,
    db: Session = Depends(get_db),
):
    return serialize_item(db, patch_watchlist_item(db, _item(db, item_id), payload))


@router.post("/items/{item_id}/archive")
def archive_item(item_id: int, db: Session = Depends(get_db)):
    return serialize_item(db, archive_watchlist_item(db, _item(db, item_id)))


@router.post("/items/{item_id}/reanalyze")
def reanalyze_item(item_id: int, db: Session = Depends(get_db)):
    item = _item(db, item_id)
    observed_at = datetime.now(timezone.utc)
    request, _ = create_reanalysis_request_once(
        db,
        item,
        reason_codes=["MANUAL_REANALYSIS"],
        observed_at=observed_at,
        cooldown_seconds=1,
    )
    db.commit()
    run = execute_reanalysis(db, request.id)
    return jsonable_encoder(run)


@router.get("/items/{item_id}/revisions")
def list_revisions(item_id: int, db: Session = Depends(get_db)):
    _item(db, item_id)
    return db.scalars(
        select(WatchlistRevision)
        .where(WatchlistRevision.watchlist_item_id == item_id)
        .order_by(WatchlistRevision.revision_number.desc())
    ).all()


@router.get("/items/{item_id}/transitions")
def list_transitions(item_id: int, db: Session = Depends(get_db)):
    _item(db, item_id)
    return db.scalars(
        select(WatchlistTransition)
        .where(WatchlistTransition.watchlist_item_id == item_id)
        .order_by(WatchlistTransition.observed_at.desc())
    ).all()


@router.get("/events")
def list_events(
    acknowledged: bool | None = None,
    item_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = select(MonitoringEvent)
    if acknowledged is not None:
        query = query.where(
            MonitoringEvent.acknowledged_at.is_not(None)
            if acknowledged
            else MonitoringEvent.acknowledged_at.is_(None)
        )
    if item_id is not None:
        query = query.where(MonitoringEvent.watchlist_item_id == item_id)
    return db.scalars(query.order_by(MonitoringEvent.created_at.desc()).limit(limit)).all()


@router.post("/events/{event_id}/acknowledge")
def acknowledge_event(event_id: int, db: Session = Depends(get_db)):
    event = db.get(MonitoringEvent, event_id)
    if event is None:
        raise AppError(404, "WATCHLIST_EVENT_NOT_FOUND", "监控事件不存在")
    if event.acknowledged_at is None:
        event.acknowledged_at = utc_now_naive()
        db.commit()
        db.refresh(event)
    return event


@router.post("/scan")
def scan_watchlist(
    payload: WatchlistScanRequest,
    db: Session = Depends(get_db),
):
    now = payload.now or datetime.now(timezone.utc)
    return WatchlistMonitoringService(db).scan(now=now, item_ids=payload.item_ids)


__all__ = ["router"]
