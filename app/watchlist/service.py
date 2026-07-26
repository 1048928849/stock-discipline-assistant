from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_hub.trading_calendar import to_utc_storage_naive
from app.domain.models import DecisionPackage, StrategyBinding
from app.errors import AppError
from app.models import (
    MonitoringEvent,
    PlanAnalysisRun,
    WatchlistItem,
    WatchlistRevision,
    WatchlistTransition,
)
from app.watchlist.contracts import (
    MonitoringHealth,
    WatchlistCreateRequest,
    WatchlistPatchRequest,
    WatchlistSourceType,
    WatchlistStatus,
)
from app.watchlist.state_machine import validate_transition


REVISION_FIELDS = (
    "thesis",
    "strategy_id",
    "strategy_version",
    "strategy_implementation_hash",
    "strategy_parameter_hash",
    "strategy_signal_hash",
    "strategy_binding_hash",
    "entry_low",
    "entry_high",
    "hard_stop",
    "waiting_conditions",
    "invalidation_conditions",
    "latest_snapshot_hash",
    "latest_package_hash",
)


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json(value: Any) -> Any:
    return jsonable_encoder(value)


def revision_snapshot(item: WatchlistItem) -> dict[str, Any]:
    return _json(
        {
            "symbol": item.symbol,
            "market": item.market,
            "status": item.status,
            "analysis_capital": item.analysis_capital,
            **{field: getattr(item, field) for field in REVISION_FIELDS},
            "latest_analysis_id": item.latest_analysis_id,
        }
    )


def append_revision(
    db: Session,
    item: WatchlistItem,
    *,
    reason: str,
    changed_by: str,
    now: datetime | None = None,
    increment: bool = True,
) -> WatchlistRevision:
    if increment:
        item.revision += 1
    row = WatchlistRevision(
        watchlist_item_id=item.id,
        revision_number=item.revision,
        previous_revision_number=item.revision - 1 if item.revision > 1 else None,
        change_reason=reason,
        changed_by=changed_by,
        snapshot=revision_snapshot(item),
        created_at=now or utc_now_naive(),
    )
    db.add(row)
    db.flush()
    return row


def _binding(package: DecisionPackage) -> StrategyBinding:
    if not package.strategy_bindings:
        raise AppError(
            422,
            "WATCHLIST_STRATEGY_BINDING_REQUIRED",
            "分析结果缺少可验证的策略绑定",
        )
    return package.strategy_bindings[0]


def extract_product_analysis(
    db: Session, analysis_run_id: int
) -> tuple[PlanAnalysisRun, dict[str, Any]]:
    run = db.get(PlanAnalysisRun, analysis_run_id)
    if run is None:
        raise AppError(404, "ANALYSIS_RUN_NOT_FOUND", "分析记录不存在")
    if run.status != "success" or not run.result_snapshot:
        raise AppError(422, "ANALYSIS_RUN_NOT_SUCCESSFUL", "分析记录尚未成功完成")
    result = run.result_snapshot
    try:
        package = DecisionPackage.model_validate(result["decision_package"])
        plan = result["plan"]
        buy_plan = plan["buy_plan"]
        zone = buy_plan["buy_zone"]
        entry_low, entry_high = Decimal(str(zone[0])), Decimal(str(zone[1]))
        hard_stop = Decimal(str(buy_plan["hard_stop"]))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AppError(
            422,
            "WATCHLIST_ANALYSIS_PLAN_INVALID",
            "分析记录不包含可验证的观察计划",
        ) from exc
    if entry_low <= 0 or entry_high < entry_low or not (0 < hard_stop < entry_low):
        raise AppError(422, "WATCHLIST_ANALYSIS_PLAN_INVALID", "分析计划价格区间无效")
    binding = _binding(package)
    health = {
        "CONFLICTED": MonitoringHealth.CONFLICTED.value,
        "STALE": MonitoringHealth.STALE.value,
        "MISSING": MonitoringHealth.DATA_BLOCKED.value,
    }.get(package.quality_status.value, MonitoringHealth.HEALTHY.value)
    request = run.request_snapshot or {}
    capital = Decimal(str(request.get("plan_capital") or 300000))
    return run, {
        "account_id": run.account_id,
        "symbol": run.symbol,
        "name": plan.get("name") or plan.get("stock_name"),
        "status": WatchlistStatus.WATCHING.value,
        "monitoring_health": health,
        "strategy_id": binding.strategy_id,
        "strategy_version": binding.strategy_version,
        "strategy_implementation_hash": binding.implementation_hash,
        "strategy_parameter_hash": binding.parameter_hash,
        "strategy_signal_hash": binding.signal_hash,
        "strategy_binding_hash": binding.binding_hash,
        "analysis_capital": capital,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "hard_stop": hard_stop,
        "latest_snapshot_hash": package.product_snapshot_hash,
        "latest_package_hash": package.package_hash,
        "latest_analysis_id": run.id,
        "market_state": package.market_snapshot.market_state,
        "industry_state": package.market_snapshot.sector_state,
        "data_quality": package.quality_status.value,
        "last_analyzed_at": to_utc_storage_naive(package.created_at),
        "waiting_conditions": list(plan.get("next_observations") or []),
        "invalidation_conditions": [
            value
            for value in [
                plan.get("invalidation_condition"),
                buy_plan.get("invalidation_condition"),
            ]
            if value
        ],
    }


def create_watchlist_item(
    db: Session,
    payload: WatchlistCreateRequest,
    *,
    now: datetime | None = None,
) -> WatchlistItem:
    created_at = now or utc_now_naive()
    if payload.source_type == WatchlistSourceType.PRODUCT_ANALYSIS:
        assert payload.analysis_run_id is not None
        _, values = extract_product_analysis(db, payload.analysis_run_id)
        source_reference = str(payload.analysis_run_id)
        values["waiting_conditions"] = (
            payload.waiting_conditions or values["waiting_conditions"]
        )
        values["invalidation_conditions"] = (
            payload.invalidation_conditions or values["invalidation_conditions"]
        )
    else:
        assert payload.symbol is not None
        source_reference = uuid4().hex
        values = {
            "account_id": None,
            "symbol": payload.symbol,
            "name": None,
            "status": WatchlistStatus.DISCOVERED.value,
            "monitoring_health": MonitoringHealth.DISABLED.value,
            "analysis_capital": payload.analysis_capital or Decimal("300000"),
            "waiting_conditions": payload.waiting_conditions,
            "invalidation_conditions": payload.invalidation_conditions,
        }
    if payload.analysis_capital is not None:
        values["analysis_capital"] = payload.analysis_capital
    item = WatchlistItem(
        **values,
        market="CN",
        source_type=payload.source_type.value,
        source_reference=source_reference,
        thesis=payload.thesis.strip(),
        monitoring_enabled=values["status"] == WatchlistStatus.WATCHING.value,
        revision=1,
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(item)
    db.flush()
    append_revision(
        db,
        item,
        reason="WATCHLIST_CREATED",
        changed_by="USER",
        now=created_at,
        increment=False,
    )
    db.commit()
    db.refresh(item)
    return item


def patch_watchlist_item(
    db: Session,
    item: WatchlistItem,
    payload: WatchlistPatchRequest,
) -> WatchlistItem:
    changed = payload.model_dump(exclude_unset=True)
    if changed.get("monitoring_enabled") is True and (
        WatchlistStatus(item.status)
        not in {
            WatchlistStatus.WATCHING,
            WatchlistStatus.NEAR_ENTRY,
            WatchlistStatus.ENTRY_TRIGGERED,
        }
        or item.entry_low is None
        or item.entry_high is None
        or item.hard_stop is None
    ):
        raise AppError(
            409,
            "WATCHLIST_MONITORING_PLAN_REQUIRED",
            "只有包含完整价格计划的有效观察项才能启用监控",
        )
    revision_change = any(key in REVISION_FIELDS for key in changed)
    for field, value in changed.items():
        setattr(item, field, value)
    if "monitoring_enabled" in changed:
        item.monitoring_health = (
            MonitoringHealth.HEALTHY.value
            if item.monitoring_enabled
            else MonitoringHealth.DISABLED.value
        )
    item.updated_at = utc_now_naive()
    if revision_change:
        append_revision(db, item, reason="USER_EDIT", changed_by="USER")
    db.commit()
    db.refresh(item)
    return item


def transition_item(
    db: Session,
    item: WatchlistItem,
    to_status: WatchlistStatus,
    *,
    reason_codes: list[str],
    evidence_references: list[str],
    observed_at: datetime,
    automatic: bool = True,
) -> WatchlistTransition | None:
    current = WatchlistStatus(item.status)
    if current == to_status:
        return None
    validate_transition(current, to_status, automatic=automatic)
    stored_observed_at = to_utc_storage_naive(observed_at)
    row = WatchlistTransition(
        watchlist_item_id=item.id,
        revision_number=item.revision,
        from_status=current.value,
        to_status=to_status.value,
        reason_codes=reason_codes,
        evidence_references=evidence_references,
        observed_at=stored_observed_at,
        created_at=utc_now_naive(),
    )
    item.status = to_status.value
    item.updated_at = utc_now_naive()
    if to_status == WatchlistStatus.ARCHIVED:
        item.monitoring_enabled = False
        item.monitoring_health = MonitoringHealth.DISABLED.value
    db.add(row)
    db.flush()
    return row


def archive_watchlist_item(db: Session, item: WatchlistItem) -> WatchlistItem:
    transition_item(
        db,
        item,
        WatchlistStatus.ARCHIVED,
        reason_codes=["USER_ARCHIVED"],
        evidence_references=[],
        observed_at=datetime.now(timezone.utc),
        automatic=False,
    )
    db.commit()
    db.refresh(item)
    return item


def serialize_item(db: Session, item: WatchlistItem) -> dict[str, Any]:
    unread = db.scalar(
        select(func.count(MonitoringEvent.id)).where(
            MonitoringEvent.watchlist_item_id == item.id,
            MonitoringEvent.acknowledged_at.is_(None),
        )
    )
    data = {column.name: getattr(item, column.name) for column in item.__table__.columns}
    data["unread_event_count"] = int(unread or 0)
    if item.current_price is not None and item.entry_high:
        data["distance_to_entry_pct"] = str(
            ((item.current_price - item.entry_high) / item.entry_high * 100).quantize(
                Decimal("0.0001")
            )
        )
    else:
        data["distance_to_entry_pct"] = None
    return _json(data)


__all__ = [
    "append_revision",
    "archive_watchlist_item",
    "create_watchlist_item",
    "extract_product_analysis",
    "patch_watchlist_item",
    "revision_snapshot",
    "serialize_item",
    "transition_item",
    "utc_now_naive",
]
