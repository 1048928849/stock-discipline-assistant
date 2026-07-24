from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.data_hub.quality import DataQualityStatus, worst_quality
from app.domain.models import (
    DecisionPackage,
    Evidence,
    MarketSnapshot,
    ResearchDecision,
    RiskDecision,
    StrategyDecision,
)


EXECUTION_DATA_STEPS = {
    "market_data",
    "market_judgement",
    "industry_judgement",
    "stock_analysis",
    "risk_calculation",
}


def _step_quality(step: dict[str, Any]) -> DataQualityStatus:
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
    return Evidence(
        evidence_id=f"pipeline:{step['code']}",
        symbol=symbol,
        category=step["code"],
        source_name=step.get("source") or "application_pipeline",
        observed_at=step.get("data_time"),
        fetched_at=step.get("data_time"),
        quality_status=_step_quality(step),
        payload={
            "name": step.get("name"),
            "detail": step.get("detail"),
            "status": step.get("status"),
            "missing": step.get("missing", []),
        },
        is_primary=step["code"] in {"market_data", "risk_calculation"},
        external_text_is_untrusted=False,
    )


def _source_evidence(source: dict[str, Any], symbol: str) -> Evidence:
    evidence_id = source.get("evidence_id") or source.get("source_id")
    quality = (
        DataQualityStatus.STALE
        if source.get("stale")
        else DataQualityStatus.SINGLE_SOURCE
    )
    return Evidence(
        evidence_id=evidence_id,
        symbol=source.get("symbol") or symbol,
        category=source.get("category") or "research",
        source_name=source.get("source_name") or source.get("name") or "unknown",
        source_url=source.get("source_url"),
        observed_at=source.get("published_at") or source.get("data_date"),
        fetched_at=source.get("fetched_at"),
        quality_status=quality,
        payload=source.get("content") or source,
        is_primary=bool(source.get("is_primary")),
        external_text_is_untrusted=source.get("external_text_is_untrusted", True),
    )


def _collect_evidence_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"evidence_ids", "source_ids"} and isinstance(child, list):
                found.update(str(item) for item in child)
            else:
                found.update(_collect_evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_evidence_ids(child))
    return found


def build_decision_package(
    *,
    preview: dict[str, Any],
    decision: dict[str, Any],
    steps: list[dict[str, Any]],
    ai_result: dict[str, Any],
    orchestrator_id: str,
) -> DecisionPackage:
    symbol = preview["symbol"]
    core_steps = [step for step in steps if step["code"] in EXECUTION_DATA_STEPS]
    quality = worst_quality([_step_quality(step) for step in core_steps])
    blocked = quality.blocks_execution

    evidence_by_id = {
        item.evidence_id: item for item in (_pipeline_evidence(step, symbol) for step in steps)
    }
    for source in ai_result.get("sources") or []:
        item = _source_evidence(source, symbol)
        evidence_by_id[item.evidence_id] = item

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
    strategy_evidence_ids = [
        f"pipeline:{step['code']}" for step in core_steps if f"pipeline:{step['code']}" in evidence_by_id
    ]
    research_result = ai_result.get("result")
    research_evidence_ids = sorted(
        item for item in _collect_evidence_ids(research_result) if item in evidence_by_id
    )
    if not research_evidence_ids and "pipeline:company_risk" in evidence_by_id:
        research_evidence_ids = ["pipeline:company_risk"]

    calculations = preview.get("position_calculation") or {}
    hard_stop = preview.get("buy_plan", {}).get("hard_stop")
    rule_status = preview["status"]
    executable_status = "WAIT" if blocked and rule_status == "READY" else rule_status
    executable_decision_code = (
        "WAIT" if blocked and decision["status"] in {"TRIAL_ALLOWED", "CONDITIONAL_ADD"} else decision["status"]
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
        blocked_reasons.append(f"执行所需数据质量为 {quality.value}")
    if not freeze_inputs_present:
        blocked_reasons.append("缺少可靠买入区、硬止损或行情日期")

    existing = preview.get("existing_position") or {}
    market = preview.get("market_assessment") or {}
    industry = preview.get("industry_assessment") or {}
    current_price = existing.get("current_price")
    return DecisionPackage(
        package_id=f"decision:{preview['preview_hash']}",
        created_at=datetime.fromisoformat(preview["generated_at"]),
        market_snapshot=MarketSnapshot(
            symbol=symbol,
            as_of=preview.get("data_date"),
            market_state=market.get("state", "无法判断"),
            sector_state=industry.get("state", "无法判断"),
            current_price=Decimal(str(current_price)) if current_price is not None else None,
            quality_status=quality,
            evidence_ids=market_evidence_ids,
        ),
        evidence=list(evidence_by_id.values()),
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
            missing_data=(research_result or {}).get("missing_data", [])
            if isinstance(research_result, dict)
            else ["AI辅助解释"],
            result=research_result,
            orchestrator=orchestrator_id,
        ),
        quality_status=quality,
        ready_allowed=ready_allowed,
        freeze_allowed=freeze_allowed,
        blocked_reasons=blocked_reasons,
        legacy_preview_hash=preview["preview_hash"],
    )
