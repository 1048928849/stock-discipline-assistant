import ast
import subprocess
import sys
from datetime import datetime, timedelta
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
    QualitySnapshotItem,
    ResearchClaim,
    ResearchDecision,
    ResearchResult,
    RiskDecision,
    StrategyDecision,
)
from app.research.orchestrator import (
    ExistingAIResearchOrchestrator,
    ResearchExecution,
)
from app.services import data_sources


def _evidence(
    evidence_id: str = "market:1",
    quality: DataQualityStatus = DataQualityStatus.SINGLE_SOURCE,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        symbol="300502",
        capability="stock_daily_bars",
        required=True,
        category="market",
        source_name="test",
        observed_at="2026-07-24",
        fetched_at="2026-07-24T16:00:00",
        quality_status=quality,
        payload={"close": 10.82},
    )


def _decision_package(**overrides) -> DecisionPackage:
    quality = overrides.get("quality_status", DataQualityStatus.SINGLE_SOURCE)
    evidence = overrides.get("evidence", [_evidence(quality=quality)])
    strategy = overrides.get(
        "strategy_decision",
        StrategyDecision(
            rule_status="READY",
            executable_status="WAIT" if quality.blocks_execution else "READY",
            decision_code="TRIAL_ALLOWED",
            label="allow trial",
            next_action="wait for trigger",
            rule_version="1.0.0",
            evidence_ids=["market:1"],
        ),
    )
    generated = datetime.now()
    values = {
        "package_id": "decision:test",
        "created_at": generated,
        "generated_at": generated,
        "expires_at": generated + timedelta(hours=24),
        "market_snapshot": MarketSnapshot(
            symbol="300502",
            as_of="2026-07-24",
            market_state="up",
            sector_state="strong",
            current_price="10.82",
            quality_status=quality,
            evidence_ids=["market:1"],
        ),
        "evidence": evidence,
        "strategy_decision": strategy,
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
            result=ResearchResult(
                claims=[
                    ResearchClaim(
                        text="test",
                        evidence_ids=["market:1"],
                        confidence="high",
                        claim_type="fact",
                    )
                ]
            ),
            orchestrator="single_pass_existing_ai",
            research_completeness=100,
        ),
        "quality_status": quality,
        "ready_allowed": not quality.blocks_execution,
        "freeze_allowed": not quality.blocks_execution,
        "blocked_reasons": [] if not quality.blocks_execution else [quality.value],
        "required_capabilities": ["stock_daily_bars"],
        "evidence_digest": "0" * 64,
        "quality_snapshot": {
            "stock_daily_bars": QualitySnapshotItem(
                capability="stock_daily_bars",
                required=True,
                quality_status=quality,
                evidence_ids=["market:1"],
                observed_at=["2026-07-24"],
                providers=["test"],
            )
        },
        "rule_snapshot": {"version": "1.0.0"},
        "account_snapshot": {"id": 1, "equity": 100000},
        "legacy_preview_hash": "a" * 64,
        "package_hash": "0" * 64,
    }
    values.update(overrides)
    package = DecisionPackage(**values)
    payload = package.model_dump(mode="json")
    payload["evidence_digest"] = package.evidence_digest_value()
    package = DecisionPackage.model_validate(payload)
    payload = package.model_dump(mode="json")
    payload["package_hash"] = package.package_hash_value()
    return DecisionPackage.model_validate(payload)


def test_quality_contract_covers_all_states():
    assert assess_quality([]) == DataQualityStatus.MISSING
    assert assess_quality([QualityObservation("a", {"close": 10})]) == DataQualityStatus.SINGLE_SOURCE
    assert assess_quality(
        [
            QualityObservation("a", {"close": 10}),
            QualityObservation("b", {"close": 10}),
        ]
    ) == DataQualityStatus.VERIFIED
    assert assess_quality(
        [
            QualityObservation("a", {"close": 10}),
            QualityObservation("b", {"close": 11}),
        ]
    ) == DataQualityStatus.CONFLICTED
    assert assess_quality(
        [QualityObservation("cache", {"close": 10}, stale=True)]
    ) == DataQualityStatus.STALE
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

    blocked = _decision_package(quality_status=DataQualityStatus.CONFLICTED)
    assert blocked.freeze_allowed is False
    with pytest.raises(ValidationError, match="cannot allow READY"):
        _decision_package(
            quality_status=DataQualityStatus.STALE,
            ready_allowed=True,
            freeze_allowed=True,
        )


def test_decision_package_rejects_nested_unknown_evidence_id():
    package = _decision_package()
    result = package.research_decision.result.model_copy(
        update={
            "claims": [
                ResearchClaim(
                    text="unknown",
                    evidence_ids=["missing:1"],
                    confidence="high",
                    claim_type="fact",
                )
            ]
        }
    )
    decision = package.research_decision.model_copy(update={"result": result})
    with pytest.raises(ValidationError, match="unknown evidence_id"):
        _decision_package(research_decision=decision)


def test_research_result_rejects_unknown_evidence_in_claim():
    package = _decision_package()
    result = ResearchResult(
        claims=[
            ResearchClaim(
                text="unknown claim",
                evidence_ids=["unknown:claim"],
                confidence="high",
                claim_type="fact",
            )
        ]
    )
    decision = package.research_decision.model_copy(update={"result": result})
    with pytest.raises(ValidationError, match="unknown evidence_id"):
        _decision_package(research_decision=decision)


def test_research_result_rejects_unknown_evidence_in_conflict():
    package = _decision_package()
    result = ResearchResult(
        claims=[
            ResearchClaim(
                text="unknown conflict",
                evidence_ids=["unknown:a", "unknown:b"],
                confidence="medium",
                claim_type="conflict",
            )
        ]
    )
    decision = package.research_decision.model_copy(update={"result": result})
    with pytest.raises(ValidationError, match="unknown evidence_id"):
        _decision_package(research_decision=decision)


def test_freeze_hash_includes_quality_and_evidence_digest():
    single = _decision_package(quality_status=DataQualityStatus.SINGLE_SOURCE)
    verified = _decision_package(quality_status=DataQualityStatus.VERIFIED)
    assert single.evidence_digest != verified.evidence_digest
    assert single.package_hash != verified.package_hash


def test_decision_package_recomputes_quality_from_evidence():
    package = _decision_package()
    payload = package.model_dump(mode="json")
    payload["quality_status"] = "VERIFIED"
    payload["market_snapshot"]["quality_status"] = "VERIFIED"
    payload["package_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="required Evidence"):
        DecisionPackage.model_validate(payload)


def test_source_ids_alias_still_requires_valid_evidence():
    claim = ResearchClaim.model_validate(
        {
            "text": "legacy",
            "source_ids": ["missing:1"],
            "confidence": "high",
            "claim_type": "fact",
        }
    )
    package = _decision_package()
    result = package.research_decision.result.model_copy(update={"claims": [claim]})
    decision = package.research_decision.model_copy(update={"result": result})
    with pytest.raises(ValidationError, match="unknown evidence_id"):
        _decision_package(research_decision=decision)
    assert "source_ids" not in claim.model_dump()


def test_research_result_rejects_excessive_item_count():
    claim = {
        "text": "item",
        "evidence_ids": ["market:1"],
        "confidence": "high",
        "claim_type": "fact",
    }
    with pytest.raises(ValidationError):
        ResearchResult.model_validate({"claims": [claim] * 161})


def test_research_result_rejects_excessive_depth():
    nested = {"claims": [], "unexpected": {"a": {"b": {"c": {}}}}}
    with pytest.raises(ValidationError):
        ResearchResult.model_validate(nested)


def test_research_orchestrator_returns_typed_result():
    calls = []
    orchestrator = ExistingAIResearchOrchestrator(
        lambda request: calls.append(request)
        or {
            "status": "success",
            "result": {
                "ai_summaries": [
                    {
                        "content": "typed",
                        "evidence_ids": ["market:1"],
                        "confidence": "high",
                    }
                ]
            },
        }
    )
    result = orchestrator.run("request")
    assert isinstance(result, ResearchExecution)
    assert isinstance(result.result, ResearchResult)
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


@pytest.mark.parametrize(
    ("module", "forbidden"),
    [
        ("app.domain.models", ["app.models", "app.data_hub.router", "app.providers.akshare_provider"]),
        ("app.data_hub", ["app.data_hub.router", "app.providers.akshare_provider"]),
    ],
)
def test_domain_import_does_not_load_orm_or_providers(module, forbidden):
    script = (
        f"import sys; import {module}; "
        f"bad=[name for name in {forbidden!r} if name in sys.modules]; "
        "print(','.join(bad))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""


def test_data_hub_init_does_not_load_router_or_providers():
    script = (
        "import sys; import app.data_hub; "
        "bad=[name for name in "
        "['app.data_hub.router','app.models','app.providers.akshare_provider'] "
        "if name in sys.modules]; print(','.join(bad))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""


def test_all_production_callers_use_single_composition_root():
    root = Path(__file__).parents[1] / "app"
    constructors = []
    for path in root.rglob("*.py"):
        if path == root / "composition" / "data_hub.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "DataHubRouter"
            ):
                constructors.append(str(path.relative_to(root)))
    assert constructors == []


def test_compatibility_service_delegates_to_composition_root(monkeypatch):
    sentinel = object()
    calls = []

    def fake_builder(db, registry=None):
        calls.append((db, registry))
        return sentinel

    monkeypatch.setattr(data_sources, "_build_data_hub", fake_builder)
    registry = object()
    assert data_sources.build_data_hub("db", registry=registry) is sentinel
    assert data_sources.UnifiedDataService("db", registry=registry) is sentinel
    assert calls == [("db", registry), ("db", registry)]
