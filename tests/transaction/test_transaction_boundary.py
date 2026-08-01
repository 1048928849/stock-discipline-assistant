import pytest
from sqlalchemy import event, func, select

from app.models import Account, PreviewSnapshotRecord, RuleVersion, TradePlan, TradePlanCheck
from app.schemas_workflow import TradePlanPreviewRequest, TradePlanSaveRequest
from app.services.rule_version_manager import RuleVersionManager
from app.services.trade_plan.confirm_application import confirm_trade_plan
from app.services.trade_plan_generator import generate_trade_plan_preview
from app.services.transaction import transaction_scope
from tests.trade_plan.test_trade_plan_boundaries import create_account, payload, seed_pattern


def test_transaction_scope_commits_success(session):
    with transaction_scope(session):
        session.add(Account(name="事务提交", total_assets=1000, cash=800, available_cash=800))

    assert session.scalar(select(func.count()).select_from(Account)) == 1


def test_transaction_scope_rolls_back_exception(session):
    with pytest.raises(RuntimeError, match="rollback"), transaction_scope(session):
        session.add(Account(name="事务回滚", total_assets=1000, cash=800, available_cash=800))
        session.flush()
        raise RuntimeError("rollback")

    assert session.scalar(select(func.count()).select_from(Account)) == 0


def test_nested_transaction_scope_does_not_commit_outer_transaction(session):
    session.add(Account(name="外层事务", total_assets=1000, cash=800, available_cash=800))
    session.flush()

    with transaction_scope(session):
        session.add(Account(name="内层保存点", total_assets=1000, cash=800, available_cash=800))

    session.rollback()
    assert session.scalar(select(func.count()).select_from(Account)) == 0


def test_rule_version_manager_does_not_auto_commit_and_joins_scope(session):
    commits = 0

    def count_commit(_session):
        nonlocal commits
        commits += 1

    event.listen(session, "after_commit", count_commit)
    RuleVersionManager(session).ensure_active_version()
    assert commits == 0
    session.rollback()
    assert session.scalar(select(func.count()).select_from(RuleVersion)) == 0
    session.rollback()

    with transaction_scope(session):
        version = RuleVersionManager(session).ensure_active_version()
        assert version.id is not None

    assert commits == 1
    assert session.scalar(select(func.count()).select_from(RuleVersion)) == 2


def test_preview_snapshot_failure_rolls_back_rule_versions(client, session, monkeypatch):
    account = create_account(client)
    seed_pattern(session)
    request = TradePlanPreviewRequest(**payload(account["id"]))

    def fail_snapshot(*args, **kwargs):
        raise RuntimeError("snapshot write failed")

    monkeypatch.setattr(
        "app.services.trade_plan_generator.save_preview_snapshot", fail_snapshot
    )
    with pytest.raises(RuntimeError, match="snapshot write failed"):
        generate_trade_plan_preview(session, request)

    assert session.scalar(select(func.count()).select_from(PreviewSnapshotRecord)) == 0
    assert session.scalar(select(func.count()).select_from(RuleVersion)) == 0


def test_confirm_trade_plan_save_failure_rolls_back(client, session, monkeypatch):
    account = create_account(client)
    seed_pattern(session)
    request_payload = payload(account["id"])
    preview = client.post(
        "/api/v1/trade-plan-generator/preview", json=request_payload
    ).json()
    request = TradePlanSaveRequest(
        **request_payload, preview_hash=preview["preview_hash"]
    )

    def fail_save(*args, **kwargs):
        raise RuntimeError("plan write failed")

    monkeypatch.setattr(
        "app.services.persistence.trade_plan_repository.TradePlanRepository.save",
        fail_save,
    )
    with pytest.raises(RuntimeError, match="plan write failed"):
        confirm_trade_plan(session, request)

    assert session.scalar(select(func.count()).select_from(TradePlan)) == 0


def test_execution_initialization_failure_rolls_back_plan_and_checks(
    client, session, monkeypatch
):
    account = create_account(client)
    seed_pattern(session)
    request_payload = payload(account["id"])
    preview = client.post(
        "/api/v1/trade-plan-generator/preview", json=request_payload
    ).json()
    request = TradePlanSaveRequest(
        **request_payload, preview_hash=preview["preview_hash"]
    )

    def fail_execution(*args, **kwargs):
        raise RuntimeError("execution initialization failed")

    monkeypatch.setattr(
        "app.services.persistence.execution_initializer.ExecutionInitializer.initialize",
        fail_execution,
    )
    with pytest.raises(RuntimeError, match="execution initialization failed"):
        confirm_trade_plan(session, request)

    assert session.scalar(select(func.count()).select_from(TradePlan)) == 0
    assert session.scalar(select(func.count()).select_from(TradePlanCheck)) == 0
