from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.domain.models import (
    MARKET_EVIDENCE_CAPABILITIES,
    SOURCE_EVIDENCE_CAPABILITIES,
    DecisionPackage,
)
from app.errors import AppError
from app.schemas_workflow import TradePlanPreviewRequest, TradePlanSaveRequest
from app.services.market_cache import resolve_market_quality_binding
from app.services.research_cache import (
    resolve_cached_announcement_catalog,
    resolve_cached_company_profile,
)
from app.data_hub.trading_calendar import (
    market_storage_naive_to_aware,
    shanghai_now,
    to_shanghai_aware,
)


MAX_ANALYSIS_AGE = timedelta(hours=24)


def _package_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return market_storage_naive_to_aware(value)
    return to_shanghai_aware(value)


def _validated_package(
    value: dict[str, Any] | None,
    *,
    evaluated_at: datetime,
) -> DecisionPackage:
    if not value:
        raise AppError(
            422,
            "DECISION_PACKAGE_REQUIRED",
            "A valid DecisionPackage is required to freeze a formal trade plan.",
        )
    raw_evidence = value.get("evidence", []) if isinstance(value, dict) else []
    required_market_evidence = [
        item
        for item in raw_evidence
        if isinstance(item, dict)
        and item.get("required")
        and item.get("capability") in MARKET_EVIDENCE_CAPABILITIES
    ]
    if any(not item.get("market_quality_binding") for item in required_market_evidence):
        raise AppError(
            422,
            "DECISION_PACKAGE_MARKET_BINDING_REQUIRED",
            "Legacy DecisionPackage has no exact market quality binding; run a new analysis.",
        )
    required_source_evidence = [
        item
        for item in raw_evidence
        if isinstance(item, dict)
        and item.get("required")
        and item.get("capability") in SOURCE_EVIDENCE_CAPABILITIES
    ]
    if any(
        not item.get("source_quality_binding") for item in required_source_evidence
    ):
        raise AppError(
            422,
            "DECISION_PACKAGE_SOURCE_BINDING_REQUIRED",
            "Legacy DecisionPackage has no exact research source binding; run a new analysis.",
        )
    try:
        package = DecisionPackage.model_validate(value)
    except ValidationError as exc:
        raise AppError(
            422,
            "DECISION_PACKAGE_CHANGED",
            "DecisionPackage integrity validation failed; run a new analysis.",
        ) from exc
    if _package_time(package.expires_at) <= evaluated_at:
        raise AppError(
            422,
            "DECISION_PACKAGE_EXPIRED",
            "DecisionPackage has expired; run a new analysis.",
        )
    if not package.freeze_allowed:
        raise AppError(
            422,
            "ANALYSIS_QUALITY_BLOCKED",
            "; ".join(package.blocked_reasons)
            or "Data quality does not allow a formal plan to be frozen.",
        )
    return package


def freeze_trade_plan(
    db: Session,
    *,
    request: TradePlanSaveRequest,
    decision_package: dict[str, Any] | None,
    analysis_created_at: datetime | None = None,
    analysis_run_id: int | None = None,
) -> dict:
    now = to_shanghai_aware(shanghai_now())
    package = _validated_package(decision_package, evaluated_at=now)
    if (
        package.generated_at.tzinfo is None
        and analysis_created_at
        and now - market_storage_naive_to_aware(analysis_created_at)
        > MAX_ANALYSIS_AGE
    ):
        raise AppError(
            422,
            "DECISION_PACKAGE_EXPIRED",
            "The analysis is older than the allowed confirmation window.",
        )
    if package.legacy_preview_hash != request.preview_hash:
        raise AppError(
            422,
            "DECISION_PACKAGE_CHANGED",
            "DecisionPackage does not belong to the supplied preview.",
        )
    for evidence in package.evidence:
        if not evidence.required or evidence.capability not in MARKET_EVIDENCE_CAPABILITIES:
            continue
        validation = resolve_market_quality_binding(
            db,
            evidence.market_quality_binding,
            evaluated_at=now,
        )
        if not validation.executable:
            raise AppError(
                422,
                validation.error_code or "MARKET_BINDING_NOT_EXECUTABLE",
                f"Market Evidence {evidence.evidence_id} changed: {validation.reason}",
            )

    for evidence in package.evidence:
        if not evidence.required or evidence.capability not in SOURCE_EVIDENCE_CAPABILITIES:
            continue
        binding = evidence.source_quality_binding
        if binding.subject_id != request.symbol:
            raise AppError(
                422,
                "SOURCE_BINDING_SCOPE_MISMATCH",
                f"Research Evidence {evidence.evidence_id} belongs to another symbol.",
            )
        if evidence.capability == "company_profile":
            selection = resolve_cached_company_profile(
                db, binding.subject_id, evaluated_at=now
            )
            current_scan_start = current_scan_end = current_checked_at = None
        else:
            selection = resolve_cached_announcement_catalog(
                db,
                binding.subject_id,
                binding.scan_start.date(),
                binding.scan_end.date(),
                evaluated_at=now,
            )
            current_scan_start = selection.scan_start
            current_scan_end = selection.scan_end
            current_checked_at = selection.checked_at
        if not selection.executable:
            error_code = (
                "DECISION_PACKAGE_CHANGED"
                if selection.structure_reason
                else "SOURCE_BINDING_NOT_EXECUTABLE"
            )
            raise AppError(
                422,
                error_code,
                f"Research Evidence {evidence.evidence_id} changed: "
                f"{selection.blocking_reason or 'not executable'}",
            )
        selected_observed_at = selection.effective_quality.observed_at
        if (
            selection.quality_record_id != binding.quality_record_id
            or selection.subject.subject_type != binding.subject_type
            or selection.subject.subject_id != binding.subject_id
            or (selection.subject.semantic_key or "") != binding.semantic_key
            or selected_observed_at != binding.observed_at
            or current_scan_start != binding.scan_start
            or current_scan_end != binding.scan_end
            or current_checked_at != binding.checked_at
        ):
            raise AppError(
                422,
                "SOURCE_BINDING_CHANGED",
                f"Research Evidence {evidence.evidence_id} uses different lineage.",
            )

    from app.services.trade_plan_generator import (
        _persist_generated_plan,
        generate_trade_plan_preview,
    )

    preview_request = TradePlanPreviewRequest(
        **request.model_dump(
            exclude={"preview_hash", "ai_analysis_id", "decision_package"}
        )
    )
    current_preview = generate_trade_plan_preview(db, preview_request)
    if current_preview["preview_hash"] != request.preview_hash:
        raise AppError(
            409,
            "PREVIEW_CHANGED",
            "Market, account, or rule inputs changed; generate a new analysis.",
        )
    if (
        package.rule_snapshot != current_preview["rule"]
        or package.account_snapshot != current_preview["account"]
    ):
        raise AppError(
            422,
            "DECISION_PACKAGE_CHANGED",
            "Rule or account snapshot no longer matches the analysis.",
        )
    try:
        return _persist_generated_plan(
            db,
            request,
            analysis_run_id=analysis_run_id,
        )
    except Exception:
        db.rollback()
        raise
