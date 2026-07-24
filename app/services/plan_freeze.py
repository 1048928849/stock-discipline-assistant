from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_hub.quality import observation_is_stale, policy_for
from app.domain.models import DecisionPackage
from app.errors import AppError
from app.models import CompanyAnnouncement, CompanyProfile, MarketDailyBar
from app.schemas_workflow import TradePlanPreviewRequest, TradePlanSaveRequest


MAX_ANALYSIS_AGE = timedelta(hours=24)
CAPABILITY_POLICY = {
    "stock_daily_bars": "market.daily",
    "benchmark_daily_bars": "market.index_daily",
    "sector_daily_bars": "market.sector_daily",
    "company_profile": "fundamental.profile",
    "announcements": "announcement.catalog",
}


def _validated_package(value: dict[str, Any] | None) -> DecisionPackage:
    if not value:
        raise AppError(
            422,
            "DECISION_PACKAGE_REQUIRED",
            "A valid DecisionPackage is required to freeze a formal trade plan.",
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
        policy_name = CAPABILITY_POLICY.get(evidence.capability)
        if not evidence.required or not policy_name:
            continue
        observed_at = evidence.observed_at
        if observed_at:
            try:
                parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            except ValueError:
                parsed = datetime.fromisoformat(f"{observed_at[:10]}T00:00:00")
        else:
            parsed = None
        if observation_is_stale(parsed, policy_for(policy_name), now=now):
            raise AppError(
                422,
                "DECISION_PACKAGE_EXPIRED",
                f"Required Evidence {evidence.evidence_id} is no longer fresh.",
            )

    changed_after_analysis = [
        db.scalar(
            select(CompanyProfile).where(
                CompanyProfile.symbol == request.symbol,
                CompanyProfile.fetched_at > package.generated_at,
            )
        ),
        db.scalar(
            select(CompanyAnnouncement).where(
                CompanyAnnouncement.symbol == request.symbol,
                CompanyAnnouncement.fetched_at > package.generated_at,
            )
        ),
        db.scalar(
            select(MarketDailyBar).where(
                MarketDailyBar.symbol.in_((request.symbol, "CSI000300")),
                MarketDailyBar.fetched_at > package.generated_at,
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
        return _persist_generated_plan(db, request)
    except Exception:
        db.rollback()
        raise
