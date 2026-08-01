from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.domain.trade_plan import TradePlanLifecycle
from app.models import MarketDailyBar, MarketQuote
from app.schemas_workflow import TradePlanPreviewRequest
from app.services.decision_engine import evaluate_decision, preview_decision_context
from app.services.trade_plan.application import generate_trade_plan
from app.services.trade_plan.assembler import assemble_preview
from app.services.trade_plan.compatibility import legacy_gate, legacy_preview
from app.services.trade_plan.lifecycle import (
    confirm_preview,
    lifecycle_from_execution_status,
    next_version,
)


def create_account(client, assets="100000", cash="80000"):
    return client.post(
        "/api/v1/accounts",
        json={
            "name": f"账户{assets}",
            "total_assets": assets,
            "cash": cash,
            "available_cash": cash,
        },
    ).json()


def seed_pattern(session, symbol="300502", state="ready", price=None):
    now = datetime.now()
    rows = []
    for index in range(260):
        close = 5 + index * 0.019
        rows.append((close, close + 0.08, close - 0.08, 100.0))
    for index in range(25):
        close = 10 + (index % 3 - 1) * 0.03
        rows.append((close, close + 0.12, close - 0.12, 100.0))
    if state in {"ready", "broken"}:
        rows.append((10.55, 10.65, 10.15, 220.0))
        rows.append((10.32, 10.48, 10.12, 55.0))
        rows.append(
            (7.7, 10.0, 7.5, 240.0)
            if state == "broken"
            else (10.82, 10.9, 10.3, 180.0)
        )
    start = date.today() - timedelta(days=len(rows) - 1)
    for index, (close, high, low, volume) in enumerate(rows):
        session.add(
            MarketDailyBar(
                symbol=symbol,
                trade_date=start + timedelta(days=index),
                open=Decimal(str(close - 0.03)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=Decimal(str(volume)),
                source="akshare_tencent_qfq",
                fetched_at=now,
            )
        )
    latest = price if price is not None else rows[-1][0]
    session.add(
        MarketQuote(
            symbol=symbol,
            name="测试公司",
            price=Decimal(str(latest)),
            source="akshare_sina",
            source_api="stock_zh_a_spot",
            fetched_at=now,
        )
    )
    session.commit()


def payload(account_id, **changes):
    result = {
        "symbol": "300502",
        "account_id": account_id,
        "trade_mode": "日线趋势波段",
        "risk_pct": 0.5,
        "max_position_pct": 20,
        "max_total_position_pct": 80,
        "max_industry_position_pct": 35,
        "market_state": "上升",
        "sector_state": "强",
    }
    result.update(changes)
    return result


def test_application_generates_normal_preview(client, session):
    account = create_account(client)
    seed_pattern(session, state="ready")

    preview = generate_trade_plan(session, TradePlanPreviewRequest(**payload(account["id"])))

    assert preview["status"] == "READY"
    assert preview["preview_hash"]


def test_application_preserves_strategy_failure(client, session):
    account = create_account(client)
    seed_pattern(session, state="broken")

    preview = generate_trade_plan(session, TradePlanPreviewRequest(**payload(account["id"])))

    assert preview["status"] == "NO_TRADE"


def test_application_preserves_risk_failure(client, session):
    account = create_account(client)
    seed_pattern(session, state="ready")
    request = payload(account["id"], max_position_pct=Decimal("0.01"))

    preview = generate_trade_plan(session, TradePlanPreviewRequest(**request))

    assert preview["position_calculation"]["final_allowed_quantity"] == 0
    assert preview["status"] == "NO_TRADE"


def test_application_preview_can_drive_exit_decision(client, session):
    account = create_account(client)
    seed_pattern(session, state="ready", price=9.4)
    holding = {
        "account_id": account["id"],
        "symbol": "300502",
        "name": "测试公司",
        "quantity": 1000,
        "cost_price": 9,
        "current_price": 9.4,
        "sector": "测试行业",
        "stop_loss_price": 9.5,
        "target_price": 12,
        "price_source": "manual",
    }
    assert client.post("/api/v1/holdings", json=holding).status_code == 201
    request = payload(
        account["id"],
        position_mode="持仓",
        holding_quantity=1000,
        holding_cost_price=9,
    )

    preview = generate_trade_plan(session, TradePlanPreviewRequest(**request))
    decision = evaluate_decision(preview_decision_context(preview, "持仓"))

    assert decision.decision.value == "PLAN_INVALID_EXIT"


def test_persistence_save_history_and_compare(client, session):
    account = create_account(client)
    seed_pattern(session, state="ready")
    request = payload(account["id"])
    preview = client.post("/api/v1/trade-plan-generator/preview", json=request).json()
    first = client.post(
        "/api/v1/trade-plan-generator/save",
        json={**request, "preview_hash": preview["preview_hash"]},
    ).json()
    second = client.post(
        "/api/v1/trade-plan-generator/save",
        json={**request, "preview_hash": preview["preview_hash"]},
    ).json()

    history = client.get(
        f"/api/v1/trade-plan-generator/history?account_id={account['id']}&symbol=300502"
    ).json()
    comparison = client.get(
        f"/api/v1/trade-plan-generator/compare?first_id={first['id']}&second_id={second['id']}"
    ).json()

    assert [item["plan_version"] for item in history] == [2, 1]
    assert comparison["first"]["id"] == first["id"]
    assert comparison["second"]["id"] == second["id"]


def test_lifecycle_preview_confirm_and_execution_mapping():
    preview = assemble_preview("300502", {"symbol": "300502"})
    snapshot = confirm_preview(preview, "hash")

    assert preview.lifecycle is TradePlanLifecycle.PREVIEW
    assert snapshot.lifecycle is TradePlanLifecycle.CONFIRMED
    assert lifecycle_from_execution_status("executing") == "executing"
    assert lifecycle_from_execution_status("holding") == "holding"
    assert lifecycle_from_execution_status("closed") == "closed"


def test_lifecycle_versioning_keeps_parent():
    latest = type("Latest", (), {"id": 7, "plan_version": 2})()

    version = next_version(latest)

    assert version.version == 3
    assert version.parent_plan_id == 7


def test_compatibility_preserves_legacy_gate_and_hash():
    gate = legacy_gate("market", "市场环境", "通过", "证据", "来源", "时间")
    payload = {
        "symbol": "300502",
        "status": "WAIT",
        "rule": {},
        "account": {},
        "existing_position": {},
        "multi_timeframe": {},
        "pattern": None,
        "buy_plan": {},
        "position_calculation": {},
        "gates": [gate],
        "data_date": None,
    }

    result = legacy_preview(assemble_preview("300502", payload))

    assert result["gates"] == [gate]
    assert len(result["preview_hash"]) == 64
