import pytest
from sqlalchemy import select

from app.domain.strategy_management import StrategyLifecycle
from app.errors import AppError
from app.models import StrategyVersionRecord, TradePlan
from app.services.strategy_management import StrategyManagementService
from tests.trade_plan.test_trade_plan_boundaries import create_account, payload, seed_pattern


def create_strategy(service: StrategyManagementService, strategy_id: str = "research_method"):
    return service.create_strategy(
        strategy_id=strategy_id,
        name="研究策略",
        description="用于验证策略资产管理",
        category="趋势",
        owner="tester",
    )


def create_version(service: StrategyManagementService, strategy_id: str = "research_method"):
    return service.create_version(
        strategy_id=strategy_id,
        version="1.0.0",
        rule_snapshot={"rules": ["rule_a"]},
        parameter_snapshot={"window": 20},
    )


def test_create_strategy(session):
    service = StrategyManagementService(session)
    strategy = create_strategy(service)

    assert strategy.id == "research_method"
    assert strategy.status is StrategyLifecycle.DRAFT
    assert service.repository.get_strategy(strategy.id) == strategy


def test_create_and_query_strategy_version(session):
    service = StrategyManagementService(session)
    create_strategy(service)
    version = create_version(service)

    assert version.version == "1.0.0"
    assert version.status is StrategyLifecycle.DRAFT
    assert service.repository.get_version("research_method", "1.0.0") == version


def test_active_version_is_immutable(session):
    service = StrategyManagementService(session)
    create_strategy(service)
    create_version(service)
    service.transition("research_method", "1.0.0", StrategyLifecycle.RESEARCH)
    service.transition("research_method", "1.0.0", StrategyLifecycle.SHADOW)
    active = service.activate("research_method", "1.0.0")

    assert active.status is StrategyLifecycle.ACTIVE
    with pytest.raises(AppError) as error:
        service.update_version_snapshots(
            "research_method",
            "1.0.0",
            rule_snapshot={"changed": True},
            parameter_snapshot={"window": 30},
        )
    assert error.value.code == "ACTIVE_STRATEGY_VERSION_IMMUTABLE"


def test_new_version_copies_frozen_snapshots(session):
    service = StrategyManagementService(session)
    create_strategy(service)
    original = create_version(service)
    created = service.create_new_version("research_method", "1.0.0", "1.1.0")

    assert created.version == "1.1.0"
    assert created.status is StrategyLifecycle.DRAFT
    assert dict(created.rule_snapshot) == dict(original.rule_snapshot)
    assert dict(created.parameter_snapshot) == dict(original.parameter_snapshot)


def test_lifecycle_transitions_are_tracked_and_invalid_jump_is_rejected(session):
    service = StrategyManagementService(session)
    create_strategy(service)
    create_version(service)

    with pytest.raises(AppError) as error:
        service.activate("research_method", "1.0.0")
    assert error.value.code == "STRATEGY_LIFECYCLE_INVALID"

    service.transition("research_method", "1.0.0", StrategyLifecycle.RESEARCH)
    service.transition("research_method", "1.0.0", StrategyLifecycle.SHADOW)
    service.activate("research_method", "1.0.0")
    service.suspend("research_method", "1.0.0")
    service.retire("research_method", "1.0.0")

    events = service.repository.list_events("research_method")
    assert events[-1].to_status is StrategyLifecycle.RETIRED
    assert [event.to_status for event in events[-5:]] == [
        StrategyLifecycle.RESEARCH,
        StrategyLifecycle.SHADOW,
        StrategyLifecycle.ACTIVE,
        StrategyLifecycle.SUSPENDED,
        StrategyLifecycle.RETIRED,
    ]


def test_trade_plan_keeps_original_strategy_version_when_new_version_is_created(
    client, session
):
    account = create_account(client)
    seed_pattern(session)
    request = payload(account["id"])
    preview = client.post("/api/v1/trade-plan-generator/preview", json=request).json()
    saved = client.post(
        "/api/v1/trade-plan-generator/save",
        json={**request, "preview_hash": preview["preview_hash"]},
    ).json()
    plan = session.get(TradePlan, saved["id"])
    original_version_id = plan.strategy_version_id
    plan.strategy_id = None
    plan.strategy_version_id = None
    session.commit()

    service = StrategyManagementService(session)
    service.ensure_platform_breakout()
    session.refresh(plan)
    original_version_id = plan.strategy_version_id
    service.create_new_version("platform_breakout_pullback", "1.0.0", "1.1.0")
    session.refresh(plan)

    linked = session.get(StrategyVersionRecord, plan.strategy_version_id)
    assert plan.strategy_id == "platform_breakout_pullback"
    assert plan.strategy_version_id == original_version_id
    assert linked.version == "1.0.0"


def test_query_historical_versions(session):
    service = StrategyManagementService(session)
    create_strategy(service)
    create_version(service)
    service.create_new_version("research_method", "1.0.0", "1.1.0")
    service.create_new_version("research_method", "1.1.0", "1.2.0")

    assert [item.version for item in service.list_versions("research_method")] == [
        "1.0.0",
        "1.1.0",
        "1.2.0",
    ]
    records = session.scalars(
        select(StrategyVersionRecord).where(
            StrategyVersionRecord.strategy_id == "research_method"
        )
    ).all()
    assert len(records) == 3
