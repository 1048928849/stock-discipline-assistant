from sqlalchemy import func, select

from app.domain.preview.models import thaw
from app.models import PreviewSnapshotRecord, RuleVersion, TradePlan
from app.schemas_workflow import TradePlanPreviewRequest
from app.services.rule_version_manager import RuleVersionManager
from app.services.trade_plan.application import generate_trade_plan
from tests.trade_plan.test_trade_plan_boundaries import create_account, payload, seed_pattern


def test_confirm_uses_frozen_snapshot_without_recalculation(client, session, monkeypatch):
    account = create_account(client)
    seed_pattern(session)
    request = payload(account["id"])
    preview = client.post("/api/v1/trade-plan-generator/preview", json=request).json()

    def fail_recalculation(*args, **kwargs):
        raise AssertionError("confirm must not recalculate an existing preview snapshot")

    monkeypatch.setattr(
        "app.services.trade_plan.application.generate_trade_plan", fail_recalculation
    )
    response = client.post(
        "/api/v1/trade-plan-generator/save",
        json={**request, "preview_hash": preview["preview_hash"]},
    )

    assert response.status_code == 201, response.text
    plan = session.get(TradePlan, response.json()["id"])
    assert plan.engine_snapshot["_confirmation"]["legacy_recalculate_confirm"] is False


def test_confirm_without_snapshot_uses_legacy_recalculation(client, session):
    account = create_account(client)
    seed_pattern(session)
    request = payload(account["id"])
    preview = generate_trade_plan(session, TradePlanPreviewRequest(**request))

    response = client.post(
        "/api/v1/trade-plan-generator/save",
        json={**request, "preview_hash": preview["preview_hash"]},
    )

    assert response.status_code == 201, response.text
    plan = session.get(TradePlan, response.json()["id"])
    assert plan.engine_snapshot["_confirmation"]["legacy_recalculate_confirm"] is True


def test_confirm_rejects_snapshot_with_invalid_integrity_hash(client, session):
    account = create_account(client)
    seed_pattern(session)
    request = payload(account["id"])
    preview = client.post("/api/v1/trade-plan-generator/preview", json=request).json()
    record = session.scalar(
        select(PreviewSnapshotRecord).where(
            PreviewSnapshotRecord.preview_hash == preview["preview_hash"]
        )
    )
    record.snapshot_hash = "0" * 64
    session.commit()

    response = client.post(
        "/api/v1/trade-plan-generator/save",
        json={**request, "preview_hash": preview["preview_hash"]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PREVIEW_SNAPSHOT_INVALID"


def test_rule_version_manager_creates_reads_and_does_not_duplicate(session):
    manager = RuleVersionManager(session)
    first = manager.ensure_active_version()
    second = manager.ensure_active_version()
    frozen = manager.create_snapshot_version(first)

    assert first.id == second.id
    assert session.scalar(select(func.count()).select_from(RuleVersion)) == 2
    assert frozen.version == first.version
    assert thaw(frozen.parameters) == first.parameters
