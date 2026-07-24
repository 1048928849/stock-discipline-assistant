import ast
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.data_hub.quality import (
    DataQualityStatus,
    QualityObservation,
    assess_quality,
)
from app.data_hub.router import ProviderResult
from app.domain.models import (
    DecisionPackage,
    Evidence,
    MarketSnapshot,
    ResearchDecision,
    RiskDecision,
    StrategyDecision,
)
from app.research.orchestrator import ExistingAIResearchOrchestrator


def _evidence(evidence_id: str = "market:1") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        symbol="300502",
        category="market",
        source_name="test",
        quality_status=DataQualityStatus.SINGLE_SOURCE,
        payload={"close": 10.82},
    )


def _decision_package(**overrides) -> DecisionPackage:
    values = {
        "package_id": "decision:test",
        "created_at": datetime.now(),
        "market_snapshot": MarketSnapshot(
            symbol="300502",
            as_of="2026-07-24",
            market_state="上升",
            sector_state="强",
            current_price="10.82",
            quality_status=DataQualityStatus.SINGLE_SOURCE,
            evidence_ids=["market:1"],
        ),
        "evidence": [_evidence()],
        "strategy_decision": StrategyDecision(
            rule_status="READY",
            executable_status="READY",
            decision_code="TRIAL_ALLOWED",
            label="允许试仓",
            next_action="按冻结计划等待触发",
            rule_version="1.0.0",
            evidence_ids=["market:1"],
        ),
        "risk_decision": RiskDecision(
            status="success",
            final_position_quantity=1000,
            trial_quantity=300,
            hard_stop="9.50",
            hard_stop_triggered=False,
            calculations={"final_allowed_quantity": 1000},
            evidence_ids=["market:1"],
        ),
        "research_decision": ResearchDecision(
            status="completed",
            ai_status="success",
            evidence_ids=["market:1"],
            missing_data=[],
            result={"summary": "test"},
            orchestrator="single_pass_existing_ai",
        ),
        "quality_status": DataQualityStatus.SINGLE_SOURCE,
        "ready_allowed": True,
        "freeze_allowed": True,
        "blocked_reasons": [],
        "legacy_preview_hash": "a" * 64,
    }
    values.update(overrides)
    return DecisionPackage(**values)


def test_quality_contract_covers_all_states():
    assert assess_quality([]) == DataQualityStatus.MISSING
    assert (
        assess_quality([QualityObservation("a", {"close": 10})])
        == DataQualityStatus.SINGLE_SOURCE
    )
    assert (
        assess_quality(
            [
                QualityObservation("a", {"close": 10}),
                QualityObservation("b", {"close": 10}),
            ]
        )
        == DataQualityStatus.VERIFIED
    )
    assert (
        assess_quality(
            [
                QualityObservation("a", {"close": 10}),
                QualityObservation("b", {"close": 11}),
            ]
        )
        == DataQualityStatus.CONFLICTED
    )
    assert (
        assess_quality([QualityObservation("cache", {"close": 10}, stale=True)])
        == DataQualityStatus.STALE
    )
    routed = ProviderResult(
        value={"close": 10},
        provider_id="test",
        capability="market.quote",
        fetched_at=datetime.now(),
        fallback_used=False,
        cache_used=False,
        errors=[],
        quality_status=DataQualityStatus.SINGLE_SOURCE,
    )
    assert routed.public_meta()["quality_status"] == "SINGLE_SOURCE"


def test_decision_package_requires_valid_evidence_ids_and_blocks_ready():
    package = _decision_package()
    assert package.strategy_decision.authority == "deterministic_rule_engine"
    bad_strategy = package.strategy_decision.model_copy(
        update={"evidence_ids": ["missing:1"]}
    )
    with pytest.raises(ValidationError, match="unknown evidence_id"):
        _decision_package(strategy_decision=bad_strategy)

    blocked_strategy = package.strategy_decision.model_copy(
        update={"executable_status": "WAIT"}
    )
    blocked = _decision_package(
        strategy_decision=blocked_strategy,
        quality_status=DataQualityStatus.CONFLICTED,
        ready_allowed=False,
        freeze_allowed=False,
        blocked_reasons=["执行所需数据质量为 CONFLICTED"],
    )
    assert blocked.freeze_allowed is False
    with pytest.raises(ValidationError, match="cannot allow READY"):
        _decision_package(quality_status=DataQualityStatus.STALE)


def test_research_orchestrator_is_single_pass_and_replaceable():
    calls = []
    orchestrator = ExistingAIResearchOrchestrator(
        lambda request: calls.append(request) or {"status": "success"}
    )
    assert orchestrator.run("request") == {"status": "success"}
    assert calls == ["request"]


def test_api_and_domain_dependency_boundaries():
    root = Path(__file__).parents[1] / "app"
    api_imports = set()
    for path in (root / "api").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                api_imports.add(node.module)
    assert not {name for name in api_imports if name.startswith("app.providers")}

    for package in ("domain", "research"):
        for path in (root / package).glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "sqlalchemy" not in text
            assert "app.models" not in text
