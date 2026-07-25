from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.domain.models import (
    DecisionPackage,
    Evidence,
    MARKET_BINDING_REQUIRED_REASON,
    MARKET_EVIDENCE_CAPABILITIES,
    SOURCE_BINDING_REQUIRED_REASON,
    SOURCE_EVIDENCE_CAPABILITIES,
    MarketQualityBinding,
    MarketSnapshot,
    QualitySnapshotItem,
    ResearchClaim,
    ResearchDecision,
    ResearchResult,
    ResearchUncertainty,
    RiskDecision,
    SourceQualityBinding,
    StrategyDecision,
)
from app.domain.quality import DataQualityStatus, worst_quality


STEP_CAPABILITIES: dict[str, tuple[str, bool]] = {
    "market_data": ("stock_daily_bars", True),
    "market_quote": ("market_quote", True),
    "company_mapping": ("company_profile", True),
    "market_judgement": ("benchmark_daily_bars", True),
    "industry_judgement": ("sector_daily_bars", True),
    "company_risk": ("announcements", True),
    "stock_analysis": ("technical_calculation", True),
    "risk_calculation": ("risk_calculation", True),
    "company_research_refresh": ("research_refresh", False),
    "ai_explanation": ("ai_explanation", False),
}

REQUIRED_CAPABILITIES = sorted(
    capability for capability, required in STEP_CAPABILITIES.values() if required
)


def _step_quality(step: dict[str, Any], capability: str) -> DataQualityStatus:
    if capability in MARKET_EVIDENCE_CAPABILITIES:
        effective = step.get("effective_quality") or step.get("quality_status")
        return (
            DataQualityStatus(effective)
            if effective
            else DataQualityStatus.MISSING
        )
    explicit = step.get("quality_status")
    if explicit:
        return DataQualityStatus(explicit)
    if step.get("status") == "failed":
        return DataQualityStatus.MISSING
    if step.get("status") == "partial":
        return (
            DataQualityStatus.STALE
            if step.get("fallback_used")
            else DataQualityStatus.MISSING
        )
    return DataQualityStatus.SINGLE_SOURCE


def _pipeline_evidence(step: dict[str, Any], symbol: str) -> Evidence:
    capability, required = STEP_CAPABILITIES.get(
        step["code"], (f"pipeline.{step['code']}", False)
    )
    observed_at = step.get("observed_at") or step.get("data_time")
    raw_binding = step.get("market_quality_binding")
    binding = (
        MarketQualityBinding.model_validate(raw_binding)
        if raw_binding is not None
        else None
    )
    raw_source_binding = step.get("source_quality_binding")
    source_binding = (
        SourceQualityBinding.model_validate(raw_source_binding)
        if raw_source_binding is not None
        else None
    )
    return Evidence(
        evidence_id=f"pipeline:{step['code']}",
        symbol=symbol,
        capability=capability,
        required=required,
        category=step["code"],
        source_name=step.get("source") or "application_pipeline",
        observed_at=observed_at,
        fetched_at=step.get("fetched_at") or step.get("data_time"),
        cached_at=step.get("cached_at"),
        quality_status=_step_quality(step, capability),
        market_quality_binding=binding,
        source_quality_binding=source_binding,
        payload={
            "name": step.get("name"),
            "detail": step.get("detail"),
            "status": step.get("status"),
            "missing": step.get("missing", []),
            "provider_observations": step.get("provider_observations", []),
            "conflict_fields": step.get("conflict_fields", []),
            "price": step.get("price"),
            "quote_type": step.get("quote_type"),
            "execution_price": step.get("execution_price"),
            "execution_quote_type": step.get("execution_quote_type"),
            "display_price": step.get("display_price"),
            "display_quote_type": step.get("display_quote_type"),
            "fallback_used": step.get("fallback_used", False),
        },
        is_primary=required,
        external_text_is_untrusted=False,
    )


def _source_evidence(source: dict[str, Any], symbol: str) -> Evidence:
    evidence_id = source.get("evidence_id") or source.get("source_id")
    quality = (
        DataQualityStatus.STALE
        if source.get("stale")
        else DataQualityStatus(source.get("quality_status", "SINGLE_SOURCE"))
    )
    category = source.get("category") or "research"
    capability = {
        "financial": "financials",
        "valuation": "valuation",
        "announcement": "announcements_detail",
        "news": "news",
        "social": "social_clues",
    }.get(category, category)
    content = source.get("content")
    payload = content if isinstance(content, dict) else {"content": content, "source": source}
    return Evidence(
        evidence_id=evidence_id,
        symbol=source.get("symbol") or symbol,
        capability=capability,
        required=bool(source.get("required", False)),
        category=category,
        source_name=source.get("source_name") or source.get("name") or "unknown",
        source_url=source.get("source_url"),
        observed_at=source.get("published_at") or source.get("data_date"),
        fetched_at=source.get("fetched_at"),
        cached_at=source.get("cached_at"),
        quality_status=quality,
        payload=payload,
        is_primary=bool(source.get("is_primary")),
        external_text_is_untrusted=source.get("external_text_is_untrusted", True),
    )


def _ids(item: dict[str, Any]) -> list[str]:
    values = item.get("evidence_ids")
    if values is None:
        values = item.get("source_ids")
    return [str(value) for value in values] if isinstance(values, list) else []


def research_result_from_ai(value: Any) -> ResearchResult | None:
    if not isinstance(value, dict):
        return None
    claims: list[ResearchClaim] = []

    def append_claim(
        rows: Any,
        *,
        text_key: str,
        claim_type: str,
        default_confidence: str = "medium",
    ) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = row.get(text_key)
            evidence_ids = _ids(row)
            if text and not evidence_ids:
                raise ValueError(
                    f"Research {claim_type} conclusion is missing evidence_ids"
                )
            if text:
                claims.append(
                    ResearchClaim(
                        text=str(text),
                        evidence_ids=evidence_ids,
                        confidence=row.get("confidence", default_confidence),
                        claim_type=claim_type,
                    )
                )

    append_claim(value.get("raw_facts"), text_key="fact", claim_type="fact", default_confidence="high")
    append_claim(value.get("ai_summaries"), text_key="content", claim_type="fact")
    append_claim(value.get("ai_inferences"), text_key="content", claim_type="inference")
    for key in ("supporting_evidence", "opposing_evidence", "invalidation_conditions"):
        append_claim(value.get(key), text_key="claim", claim_type="fact")
    append_claim(value.get("risk_events"), text_key="claim", claim_type="risk")
    append_claim(value.get("conflicts"), text_key="description", claim_type="conflict")
    uncertainties = [
        ResearchUncertainty(text=str(item), uncertainty_type="missing_information")
        for item in value.get("missing_data", [])
        if str(item).strip()
    ]
    return ResearchResult(claims=claims, uncertainties=uncertainties)


def _quality_snapshot(evidence: list[Evidence]) -> dict[str, QualitySnapshotItem]:
    result: dict[str, QualitySnapshotItem] = {}
    for capability in sorted({item.capability for item in evidence}):
        rows = [item for item in evidence if item.capability == capability]
        result[capability] = QualitySnapshotItem(
            capability=capability,
            required=any(item.required for item in rows),
            quality_status=worst_quality([item.quality_status for item in rows]),
            evidence_ids=sorted(item.evidence_id for item in rows),
            observed_at=sorted(
                item.observed_at for item in rows if item.observed_at is not None
            ),
            providers=sorted({item.source_name for item in rows}),
        )
    return result


def build_decision_package(
    *,
    preview: dict[str, Any],
    decision: dict[str, Any],
    steps: list[dict[str, Any]],
    ai_result: dict[str, Any],
    orchestrator_id: str,
) -> DecisionPackage:
    symbol = preview["symbol"]
    evidence_by_id: dict[str, Evidence] = {}
    for step in steps:
        item = _pipeline_evidence(step, symbol)
        evidence_by_id[item.evidence_id] = item
        for capability in step.get("optional_missing", []):
            optional = Evidence(
                evidence_id=f"pipeline:{step['code']}:optional:{capability}",
                symbol=symbol,
                capability=capability,
                required=False,
                category=step["code"],
                source_name=step.get("source") or "application_pipeline",
                observed_at=step.get("observed_at"),
                fetched_at=step.get("data_time"),
                quality_status=DataQualityStatus.MISSING,
                payload={"status": "missing", "capability": capability},
                external_text_is_untrusted=False,
            )
            evidence_by_id[optional.evidence_id] = optional
    for source in ai_result.get("sources") or []:
        item = _source_evidence(source, symbol)
        evidence_by_id[item.evidence_id] = item
    promoted_required = set(
        preview.get("required_research_capabilities") or []
    )
    if promoted_required:
        evidence_by_id = {
            evidence_id: (
                item.model_copy(update={"required": True})
                if item.capability in promoted_required
                else item
            )
            for evidence_id, item in evidence_by_id.items()
        }
    evidence = list(evidence_by_id.values())
    required_evidence = [item for item in evidence if item.required]
    quality = worst_quality([item.quality_status for item in required_evidence])
    blocked = quality.blocks_execution

    market_evidence_ids = [
        f"pipeline:{code}"
        for code in ("market_data", "market_judgement", "industry_judgement")
        if f"pipeline:{code}" in evidence_by_id
    ]
    risk_evidence_ids = [
        f"pipeline:{code}"
        for code in ("market_data", "risk_calculation")
        if f"pipeline:{code}" in evidence_by_id
    ]
    strategy_evidence_ids = sorted(
        item.evidence_id for item in required_evidence
    )
    research_result = research_result_from_ai(ai_result.get("result"))
    research_evidence_ids = (
        sorted(research_result.evidence_ids) if research_result else []
    )
    if not research_evidence_ids and "pipeline:company_risk" in evidence_by_id:
        research_evidence_ids = ["pipeline:company_risk"]

    calculations = preview.get("position_calculation") or {}
    hard_stop = preview.get("buy_plan", {}).get("hard_stop")
    rule_status = preview["status"]
    executable_status = "WAIT" if blocked and rule_status == "READY" else rule_status
    executable_decision_code = (
        "WAIT"
        if blocked
        and decision["status"]
        in {
            "TRIAL_ALLOWED",
            "CONDITIONAL_ADD",
            "REDUCE",
            "PLAN_INVALID_EXIT",
        }
        else decision["status"]
    )
    executable_label = (
        "等待可靠数据"
        if executable_decision_code == "WAIT" and executable_decision_code != decision["status"]
        else decision["label"]
    )
    ready_allowed = not blocked
    freeze_inputs_present = bool(hard_stop and preview.get("data_date"))
    freeze_allowed = ready_allowed and freeze_inputs_present
    blocked_reasons = []
    if blocked:
        blocked_reasons.append(f"required Evidence quality is {quality.value}")
    if not freeze_inputs_present:
        blocked_reasons.append("missing buy zone, hard stop, or market date")
    missing_market_binding = any(
        item.required
        and item.capability in MARKET_EVIDENCE_CAPABILITIES
        and item.market_quality_binding is None
        for item in evidence
    )
    if missing_market_binding:
        freeze_allowed = False
        blocked_reasons.append(MARKET_BINDING_REQUIRED_REASON)
    missing_source_binding = any(
        item.required
        and item.capability in SOURCE_EVIDENCE_CAPABILITIES
        and item.source_quality_binding is None
        for item in evidence
    )
    if missing_source_binding:
        freeze_allowed = False
        blocked_reasons.append(SOURCE_BINDING_REQUIRED_REASON)

    optional_unavailable = sorted(
        {
            item.capability
            for item in evidence
            if not item.required and item.quality_status.blocks_execution
        }
    )
    completeness = max(0, 100 - 10 * len(optional_unavailable))
    existing = preview.get("existing_position") or {}
    market = preview.get("market_assessment") or {}
    industry = preview.get("industry_assessment") or {}
    current_price = existing.get("current_price")
    generated_at = datetime.fromisoformat(preview["generated_at"])
    package = DecisionPackage.model_construct(
        package_id=f"decision:{preview['preview_hash']}",
        created_at=generated_at,
        generated_at=generated_at,
        expires_at=generated_at + timedelta(hours=24),
        market_snapshot=MarketSnapshot(
            symbol=symbol,
            as_of=preview.get("data_date"),
            market_state=market.get("state", "无法判断"),
            sector_state=industry.get("state", "无法判断"),
            current_price=Decimal(str(current_price)) if current_price is not None else None,
            quality_status=quality,
            evidence_ids=market_evidence_ids,
        ),
        evidence=evidence,
        strategy_decision=StrategyDecision(
            rule_status=rule_status,
            executable_status=executable_status,
            decision_code=executable_decision_code,
            label=executable_label,
            next_action=decision["next_action"],
            rule_version=preview["rule"]["version"],
            evidence_ids=strategy_evidence_ids,
        ),
        risk_decision=RiskDecision(
            status=next(
                (
                    step["status"]
                    for step in steps
                    if step["code"] == "risk_calculation"
                ),
                "partial",
            ),
            final_position_quantity=int(calculations.get("final_allowed_quantity", 0)),
            trial_quantity=int(calculations.get("trial_quantity", 0)),
            hard_stop=Decimal(str(hard_stop)) if hard_stop is not None else None,
            hard_stop_triggered=bool(existing.get("hard_stop_triggered")),
            calculations=calculations,
            evidence_ids=risk_evidence_ids,
        ),
        research_decision=ResearchDecision(
            status="completed" if ai_result.get("status") == "success" else "degraded",
            ai_status=ai_result.get("status", "skipped"),
            evidence_ids=research_evidence_ids,
            missing_data=[
                item.text
                for item in (research_result.uncertainties if research_result else [])
            ],
            result=research_result,
            orchestrator=orchestrator_id,
            research_completeness=completeness,
            missing_optional_evidence=optional_unavailable,
        ),
        quality_status=quality,
        ready_allowed=ready_allowed,
        freeze_allowed=freeze_allowed,
        blocked_reasons=blocked_reasons,
        required_capabilities=sorted(
            {*REQUIRED_CAPABILITIES, *promoted_required}
        ),
        evidence_digest="0" * 64,
        quality_snapshot=_quality_snapshot(evidence),
        rule_snapshot=preview["rule"],
        account_snapshot=preview["account"],
        legacy_preview_hash=preview["preview_hash"],
        package_hash="0" * 64,
    )
    with_digest = package.model_copy(
        update={"evidence_digest": package.evidence_digest_value()}
    )
    payload = with_digest.model_dump(mode="json")
    payload["package_hash"] = with_digest.package_hash_value()
    return DecisionPackage.model_validate(payload)
