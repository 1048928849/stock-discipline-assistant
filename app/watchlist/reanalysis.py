from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MonitoringEvent, ReanalysisRequest, ReanalysisRun, WatchlistItem
from app.schemas_workflow import OneClickPlanRequest
from app.services.one_click_pipeline import run_one_click_analysis
from app.watchlist.contracts import (
    EventSeverity,
    MonitoringHealth,
    MonitoringRuleType,
    ReanalysisStatus,
)
from app.watchlist.events import create_event_once
from app.watchlist.service import (
    REVISION_FIELDS,
    append_revision,
    extract_product_analysis,
    utc_now_naive,
)


def execute_reanalysis(db: Session, request_id: int) -> ReanalysisRun:
    request = db.scalar(
        select(ReanalysisRequest)
        .where(ReanalysisRequest.id == request_id)
        .with_for_update()
    )
    if request is None:
        raise ValueError("reanalysis_request_not_found")
    existing = db.scalar(
        select(ReanalysisRun).where(ReanalysisRun.request_id == request.id)
    )
    if existing:
        return existing
    item = db.get(WatchlistItem, request.watchlist_item_id)
    if item is None:
        raise ValueError("watchlist_item_not_found")
    started_at = utc_now_naive()
    request.status = ReanalysisStatus.RUNNING.value
    run = ReanalysisRun(
        request_id=request.id,
        watchlist_item_id=item.id,
        status=ReanalysisStatus.RUNNING.value,
        started_at=started_at,
        created_at=started_at,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    db.execute(
        update(MonitoringEvent)
        .where(
            MonitoringEvent.watchlist_item_id == item.id,
            MonitoringEvent.revision_number == request.revision_number,
            MonitoringEvent.reanalysis_required.is_(True),
            MonitoringEvent.reanalysis_run_id.is_(None),
        )
        .values(reanalysis_run_id=run.id)
    )
    db.commit()

    try:
        result = run_one_click_analysis(
            db,
            OneClickPlanRequest(
                symbol=item.symbol,
                account_id=item.account_id,
                plan_capital=item.analysis_capital,
                refresh=False,
                enable_ai=False,
            ),
        )
        analysis_run_id = int(result["run_id"])
        run.analysis_run_id = analysis_run_id
        package = result.get("decision_package") or {}
        if not package.get("freeze_allowed"):
            quality_blocked = package.get("quality_status") in {
                "CONFLICTED",
                "STALE",
                "MISSING",
            }
            run.status = ReanalysisStatus.BLOCKED.value
            run.error_code = (
                "PRODUCT_DATA_BLOCKED"
                if quality_blocked
                else "PRODUCT_DECISION_BLOCKED"
            )
            run.error_message = "; ".join(package.get("blocked_reasons") or [])[:2000]
            request.status = ReanalysisStatus.BLOCKED.value
            item.data_quality = package.get("quality_status") or item.data_quality
            if quality_blocked:
                item.monitoring_health = MonitoringHealth.DATA_BLOCKED.value
                observed = datetime.now(timezone.utc)
                event, _ = create_event_once(
                    db,
                    item,
                    event_type=MonitoringRuleType.DATA_QUALITY_DEGRADED,
                    severity=EventSeverity.ATTENTION,
                    title="重新分析因数据质量被阻断",
                    reason_codes=[run.error_code],
                    observed_at=observed,
                    payload={
                        "reanalysis_run_id": run.id,
                        "blocked_reasons": package.get("blocked_reasons") or [],
                    },
                    reanalysis_required=False,
                    cooldown_seconds=get_settings().watchlist_monitor_interval_seconds,
                )
                event.reanalysis_run_id = run.id
        else:
            _, values = extract_product_analysis(db, analysis_run_id)
            for field in REVISION_FIELDS:
                if field in values:
                    setattr(item, field, values[field])
            item.latest_analysis_id = analysis_run_id
            item.last_analyzed_at = values["last_analyzed_at"]
            item.market_state = values["market_state"]
            item.industry_state = values["industry_state"]
            item.data_quality = values["data_quality"]
            item.monitoring_health = MonitoringHealth.HEALTHY.value
            append_revision(
                db,
                item,
                reason="AUTOMATIC_REANALYSIS",
                changed_by="SYSTEM",
            )
            run.status = ReanalysisStatus.SUCCEEDED.value
            request.status = ReanalysisStatus.SUCCEEDED.value
        run.finished_at = utc_now_naive()
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        db.rollback()
        run = db.get(ReanalysisRun, run.id)
        request = db.get(ReanalysisRequest, request.id)
        if run is not None:
            run.status = ReanalysisStatus.FAILED.value
            run.error_code = type(exc).__name__[:100]
            run.error_message = str(exc)[:2000]
            run.finished_at = utc_now_naive()
        if request is not None:
            request.status = ReanalysisStatus.FAILED.value
        db.commit()
        if run is None:
            raise
        return run


__all__ = ["execute_reanalysis"]
