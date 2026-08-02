from datetime import date, timedelta

import pytest

from app.domain.strategy_management import StrategyLifecycle
from app.domain.strategy_research import EvidenceType, ValidationStatus
from app.domain.strategy_validation import ValidationTrade
from app.errors import AppError
from app.services.strategy_management import StrategyManagementService
from app.services.strategy_research import StrategyResearchService
from app.services.strategy_validation_engine import evaluate_validation


def draft_version(session):
    management = StrategyManagementService(session)
    management.create_strategy(
        strategy_id="research_asset",
        name="研究资产",
        description="研究平台测试策略",
        category="研究",
        owner="tester",
    )
    return management.create_version(
        strategy_id="research_asset",
        version="1.0.0",
        rule_snapshot={"rule": "test"},
        parameter_snapshot={"window": 20},
    )


def research_payload(version_id: int) -> dict:
    return {
        "strategy_version_id": version_id,
        "hypothesis": "结构确认后具有可控风险机会",
        "thesis": "供需变化通过结构和量能得到确认",
        "causal_chain": ["整理", "突破", "回踩", "确认"],
        "market_conditions": ["非下降市场"],
        "applicable_scenarios": ["日线趋势"],
        "failure_conditions": ["结构破坏"],
    }


def test_create_research_record(session):
    version = draft_version(session)
    record = StrategyResearchService(session).create_research_record(**research_payload(version.id))

    assert record.strategy_version_id == version.id
    assert record.causal_chain == ("整理", "突破", "回踩", "确认")


def test_add_evidence(session):
    version = draft_version(session)
    service = StrategyResearchService(session)
    evidence = service.add_evidence(
        strategy_version_id=version.id,
        evidence_type=EvidenceType.CASE_STUDY,
        source="manual-review",
        content="案例显示回踩缩量",
        reference="case-001",
    )

    assert evidence.type is EvidenceType.CASE_STUDY
    assert service.get_history(version.id).evidence_records == (evidence,)


def test_add_validation_result(session):
    version = draft_version(session)
    validation = StrategyResearchService(session).add_validation(
        strategy_version_id=version.id,
        validation_type="manual_sample_review",
        status=ValidationStatus.PASSED,
        sample_size=20,
        result_summary="样本满足预设纪律条件",
    )

    assert validation.status is ValidationStatus.PASSED
    assert validation.sample_size == 20


def test_strategy_version_aggregates_research_associations(session):
    version = draft_version(session)
    research = StrategyResearchService(session)
    research.create_research_record(**research_payload(version.id))
    research.add_evidence(
        strategy_version_id=version.id,
        evidence_type=EvidenceType.MARKET_DATA,
        source="local-fixture",
        content="结构化行情证据",
    )
    research.add_validation(
        strategy_version_id=version.id,
        validation_type="shadow_observation",
        status=ValidationStatus.PENDING,
        sample_size=0,
        result_summary="等待影子样本",
    )

    linked = StrategyManagementService(session).repository.get_version("research_asset", "1.0.0")
    assert linked.research_record.hypothesis == "结构确认后具有可控风险机会"
    assert len(linked.evidence_records) == 1
    assert len(linked.validation_records) == 1


def test_query_research_history_preserves_append_only_records(session):
    version = draft_version(session)
    service = StrategyResearchService(session)
    service.create_research_record(**research_payload(version.id))
    for source in ("case-a", "case-b"):
        service.add_evidence(
            strategy_version_id=version.id,
            evidence_type=EvidenceType.MANUAL,
            source=source,
            content=f"证据 {source}",
        )
    service.add_validation(
        strategy_version_id=version.id,
        validation_type="review",
        status=ValidationStatus.RUNNING,
        sample_size=5,
        result_summary="验证进行中",
    )

    history = service.get_history(version.id)
    assert [item.source for item in history.evidence_records] == ["case-a", "case-b"]
    assert history.validation_records[0].status is ValidationStatus.RUNNING


def test_active_strategy_research_core_is_immutable(session):
    management = StrategyManagementService(session)
    active = management.ensure_platform_breakout()
    research = StrategyResearchService(session)
    history = research.get_history(active.id)

    assert history.research_record is not None
    assert history.evidence_records[0].source == "existing_strategy_migration"
    with pytest.raises(AppError) as error:
        research.update_research_record(
            **research_payload(active.id),
        )
    assert error.value.code == "ACTIVE_STRATEGY_RESEARCH_IMMUTABLE"
    assert (
        management.repository.get_version("platform_breakout_pullback", "1.0.0").status
        is StrategyLifecycle.ACTIVE
    )


def test_validation_report_is_persisted_as_append_only_result(session):
    version = draft_version(session)
    trades = []
    for index in range(30):
        signal = date(2024, 1, 1) + timedelta(days=index * 3)
        trades.append(
            ValidationTrade(
                str(index),
                signal,
                signal + timedelta(days=1),
                signal + timedelta(days=2),
                1 if index % 2 == 0 else -0.3,
            )
        )
    report = evaluate_validation(trades)

    saved = StrategyResearchService(session).record_validation_report(
        strategy_version_id=version.id,
        validation_type="walk_forward",
        report=report,
    )

    assert saved.status is ValidationStatus.PASSED
    assert "expectancy_r=" in saved.result_summary
