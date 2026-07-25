from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import (
    MARKET_EVIDENCE_CAPABILITIES,
    DecisionPackage,
)
from app.errors import AppError
from app.models import (
    CompanyProfile,
    CompanyResearchRefresh,
)
from app.schemas_workflow import TradePlanPreviewRequest, TradePlanSaveRequest
from app.services.market_cache import resolve_market_quality_binding


MAX_ANALYSIS_AGE = timedelta(hours=24)
def _validated_package(value: dict[str, Any] | None) -> DecisionPackage:
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
    try:
        package = DecisionPackage.model_validate(value)
    except ValidationError as exc:
        raise AppError(
            422,
            "DECISION_PACKAGE_CHANGED",
            "DecisionPackage integrity validation failed; run a new analysis.",
        ) from exc
    if package.expires_at <= datetime.now():
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
    package = _validated_package(decision_package)
    if analysis_created_at and datetime.now() - analysis_created_at > MAX_ANALYSIS_AGE:
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
    now = datetime.now()
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

    # Company profile and announcement checks remain on the legacy C.2 path.
    changed_after_analysis = [
        db.scalar(
            select(CompanyProfile).where(
                CompanyProfile.symbol == request.symbol,
                CompanyProfile.fetched_at > package.generated_at,
            )
        ),
        db.scalar(
            select(CompanyResearchRefresh).where(
                CompanyResearchRefresh.symbol == request.symbol,
                CompanyResearchRefresh.section == "announcements",
                CompanyResearchRefresh.checked_at > package.generated_at,
            )
        ),
    ]
    if any(changed_after_analysis):
        raise AppError(
            422,
            "DECISION_PACKAGE_CHANGED",
            "Required source data changed after analysis; run a new analysis.",
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
