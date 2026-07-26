from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.data_hub.trading_calendar import utc_storage_naive_to_aware
from app.models import MonitoringEvent, ReanalysisRequest, ReanalysisRun, WatchlistItem
from app.schemas_workflow import OneClickPlanRequest
from app.services.one_click_pipeline import run_one_click_analysis
from app.watchlist.contracts import (
    EventSeverity,
    MonitoringHealth,
    MonitoringRuleType,
    PriceStateInput,
    ReanalysisStatus,
    WatchlistStatus,
)
from app.watchlist.events import create_event_once
from app.watchlist.service import (
    REVISION_FIELDS,
    append_revision,
    extract_product_analysis,
    transition_item,
    utc_now_naive,
)
from app.watchlist.state_machine import (
    determine_price_transition,
    matching_invalidation_rule,
)


_ACTIVE_STATUSES = {
    WatchlistStatus.WATCHING,
    WatchlistStatus.NEAR_ENTRY,
    WatchlistStatus.ENTRY_TRIGGERED,
}


def _blocked_health(quality_status: str | None) -> MonitoringHealth:
    return {
        "CONFLICTED": MonitoringHealth.CONFLICTED,
        "STALE": MonitoringHealth.STALE,
        "MISSING": MonitoringHealth.DATA_BLOCKED,
    }.get(quality_status, MonitoringHealth.DATA_BLOCKED)


def _merge_invalidation_rules(item: WatchlistItem, values: dict) -> None:
    preserved = [
        rule
        for rule in (item.invalidation_rule_specs or [])
        if rule.get("rule_type") != "PRICE_AT_OR_BELOW_HARD_STOP"
    ]
    values["invalidation_rule_specs"] = [
        *preserved,
        *(values.get("invalidation_rule_specs") or []),
    ]


def _recalculate_status(
    db: Session,
    item: WatchlistItem,
    *,
    observed_at,
) -> None:
    invalidation = matching_invalidation_rule(
        item.invalidation_rule_specs or [],
        current_price=item.current_price,
        market_state=item.market_state,
        industry_state=item.industry_state,
    )
    evidence = [
        reference
        for reference in (
            f"package:{item.latest_package_hash}" if item.latest_package_hash else None,
            f"snapshot:{item.latest_snapshot_hash}" if item.latest_snapshot_hash else None,
        )
        if reference
    ]
    if invalidation is not None:
        evidence.append(invalidation.evidence_reference)
        transition_item(
            db,
            item,
            WatchlistStatus.INVALIDATED,
            reason_codes=[invalidation.rule_type.value],
            evidence_references=evidence,
            observed_at=observed_at,
        )
        return
    if item.current_price is None:
        transition_item(
            db,
            item,
            WatchlistStatus.WATCHING,
            reason_codes=["REANALYSIS_PLAN_READY_PRICE_UNAVAILABLE"],
            evidence_references=evidence,
            observed_at=observed_at,
        )
        return
    outcome = determine_price_transition(
        PriceStateInput(
            current_status=WatchlistStatus(item.status),
            current_price=item.current_price,
            entry_low=item.entry_low,
            entry_high=item.entry_high,
            hard_stop=item.hard_stop,
            near_entry_distance_pct=get_settings().watchlist_near_entry_distance_pct,
            observed_at=observed_at,
            monitoring_health=MonitoringHealth.HEALTHY,
            reassessment=True,
        )
    )
    transition_item(
        db,
        item,
        outcome.to_status,
        reason_codes=list(outcome.reason_codes),
        evidence_references=evidence,
        observed_at=observed_at,
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
    if WatchlistStatus(item.status) in {
        WatchlistStatus.INVALIDATED,
        WatchlistStatus.ARCHIVED,
    }:
        raise ValueError("inactive_watchlist_item_cannot_be_reanalyzed")

    request_observed_at = utc_storage_naive_to_aware(request.requested_at)
    if WatchlistStatus(item.status) == WatchlistStatus.DISCOVERED:
        transition_item(
            db,
            item,
            WatchlistStatus.RESEARCHING,
            reason_codes=["MANUAL_REANALYSIS_STARTED"],
            evidence_references=[],
            observed_at=request_observed_at,
        )
        db.commit()

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
    run_id = run.id
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
        package_observed_at = request_observed_at
        if package.get("created_at"):
            from datetime import datetime

            package_observed_at = datetime.fromisoformat(
                str(package["created_at"]).replace("Z", "+00:00")
            )
        if not package.get("freeze_allowed"):
            quality_status = package.get("quality_status")
            quality_blocked = quality_status in {"CONFLICTED", "STALE", "MISSING"}
            run.status = ReanalysisStatus.BLOCKED.value
            run.error_code = (
                "PRODUCT_DATA_BLOCKED"
                if quality_blocked
                else "PRODUCT_DECISION_BLOCKED"
            )
            run.error_message = "; ".join(package.get("blocked_reasons") or [])[:2000]
            request.status = ReanalysisStatus.BLOCKED.value
            item.data_quality = quality_status or item.data_quality
            if quality_blocked:
                item.monitoring_health = _blocked_health(quality_status).value
                event, _ = create_event_once(
                    db,
                    item,
                    event_type=MonitoringRuleType.DATA_QUALITY_DEGRADED,
                    severity=EventSeverity.ATTENTION,
                    title="Reanalysis blocked by data quality",
                    reason_codes=[run.error_code],
                    observed_at=package_observed_at,
                    payload={
                        "reanalysis_run_id": run.id,
                        "blocked_reasons": package.get("blocked_reasons") or [],
                        "quality_status": quality_status,
                    },
                    reanalysis_required=False,
                    cooldown_seconds=get_settings().watchlist_monitor_interval_seconds,
                )
                event.reanalysis_run_id = run.id
        else:
            _, values = extract_product_analysis(db, analysis_run_id)
            _merge_invalidation_rules(item, values)
            for field in REVISION_FIELDS:
                if field in values:
                    setattr(item, field, values[field])
            item.latest_analysis_id = analysis_run_id
            item.last_analyzed_at = values["last_analyzed_at"]
            item.current_price = values["current_price"]
            item.current_price_observed_at = values["current_price_observed_at"]
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
            _recalculate_status(
                db,
                item,
                observed_at=package_observed_at,
            )
            item.monitoring_enabled = WatchlistStatus(item.status) in _ACTIVE_STATUSES
            run.status = ReanalysisStatus.SUCCEEDED.value
            request.status = ReanalysisStatus.SUCCEEDED.value
        run.finished_at = utc_now_naive()
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        db.rollback()
        run = db.get(ReanalysisRun, run_id)
        request = db.get(ReanalysisRequest, request_id)
        item = db.get(WatchlistItem, request.watchlist_item_id) if request else None
        if run is not None:
            run.status = ReanalysisStatus.FAILED.value
            run.error_code = type(exc).__name__[:100]
            run.error_message = str(exc)[:2000]
            run.finished_at = utc_now_naive()
        if request is not None:
            request.status = ReanalysisStatus.FAILED.value
        if item is not None:
            event, _ = create_event_once(
                db,
                item,
                event_type="REANALYSIS_FAILED",
                severity=EventSeverity.ATTENTION,
                title="Watchlist reanalysis failed",
                reason_codes=[type(exc).__name__],
                observed_at=request_observed_at,
                payload={"error_code": type(exc).__name__, "error": str(exc)[:1000]},
                reanalysis_required=False,
                cooldown_seconds=get_settings().watchlist_monitor_interval_seconds,
            )
            if run is not None:
                event.reanalysis_run_id = run.id
        db.commit()
        if run is None:
            raise
        return run


__all__ = ["execute_reanalysis"]
