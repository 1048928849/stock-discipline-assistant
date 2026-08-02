from __future__ import annotations

import json
import hashlib
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import CompanyProfile, Holding, MarketDailyBar, MarketQuote


GOLDEN_DIR = Path(__file__).parent
CASE_NAMES = (
    "case_01_insufficient_data",
    "case_02_no_breakout",
    "case_02_breakout_failure",
    "case_03_wait",
    "case_04_trade_allowed",
    "case_05a_holding",
    "case_05b_add",
    "case_05c_reduce",
    "case_05d_stop",
    "case_06_ai_disabled",
)


def _read_json(folder: str, case_name: str) -> dict:
    return json.loads((GOLDEN_DIR / folder / f"{case_name}.json").read_text(encoding="utf-8"))


def _create_account(client, account: dict) -> dict:
    return client.post(
        "/api/v1/accounts",
        json={
            "name": account["name"],
            "total_assets": account["total_assets"],
            "cash": account["cash"],
            "available_cash": account["available_cash"],
        },
    ).json()


def _pattern_rows(state: str) -> list[tuple[float, float, float, float]]:
    if state == "missing":
        return []
    rows = []
    for index in range(260):
        close = 5 + index * 0.019
        rows.append((close, close + 0.08, close - 0.08, 100.0))
    for index in range(25):
        close = 10 + (index % 3 - 1) * 0.03
        rows.append((close, close + 0.12, close - 0.12, 100.0))
    if state == "ready":
        rows.extend(
            [(10.55, 10.65, 10.15, 220.0), (10.32, 10.48, 10.12, 55.0)]
        )
        rows.append((10.82, 10.9, 10.3, 180.0))
    elif state == "pullback":
        rows.extend(
            [(10.55, 10.65, 10.15, 220.0), (10.32, 10.48, 10.12, 55.0)]
        )
        rows.append((10.4, 10.5, 10.2, 70.0))
    elif state == "weak_breakout":
        rows.extend(
            [(10.55, 10.65, 10.15, 110.0), (10.32, 10.48, 10.12, 55.0)]
        )
        rows.append((10.4, 10.5, 10.2, 70.0))
    elif state == "broken":
        rows.extend(
            [(10.55, 10.65, 10.15, 220.0), (10.32, 10.48, 10.12, 55.0)]
        )
        rows.append((7.7, 10.0, 7.5, 240.0))
    elif state == "flat":
        rows.extend([(10.02, 10.15, 9.9, 100.0)] * 3)
    else:
        raise AssertionError(f"unknown golden fixture state: {state}")
    return rows


def _seed_data(session, account_id: int, fixture: dict) -> None:
    symbol = fixture["symbol"]
    now = datetime.now()
    rows = _pattern_rows(fixture["pattern_state"])
    if rows:
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
        quote_price = fixture.get("quote_price", rows[-1][0])
        session.add(
            MarketQuote(
                symbol=symbol,
                name="Golden测试公司",
                price=Decimal(str(quote_price)),
                source="golden_quote",
                source_api="golden_fixture",
                fetched_at=now,
            )
        )
    if fixture.get("profile", True):
        session.add(
            CompanyProfile(
                symbol=symbol,
                name="Golden测试公司",
                industry="测试行业",
                market="创业板",
                main_business="Golden Master 固定业务描述",
                business_scope=None,
                website=None,
                source="golden_profile",
                source_url="https://example.test/golden-profile",
                raw_data={},
                fetched_at=now,
            )
        )
    holding = fixture.get("stored_holding")
    if holding:
        session.add(
            Holding(
                account_id=account_id,
                symbol=symbol,
                name="Golden测试公司",
                quantity=holding["quantity"],
                cost_price=Decimal(str(holding["cost_price"])),
                current_price=Decimal(str(fixture["quote_price"])),
                stop_loss_price=Decimal(str(holding["stop_loss_price"]))
                if holding.get("stop_loss_price") is not None
                else None,
                target_price=Decimal(str(holding["target_price"]))
                if holding.get("target_price") is not None
                else None,
                sector="测试行业",
                price_source="golden_fixture",
            )
        )
    session.commit()


def _patch_external_context(monkeypatch, fixture: dict) -> None:
    def stock_step(db, symbol, provider, refresh):
        latest = db.query(MarketDailyBar).filter(MarketDailyBar.symbol == symbol).first()
        if latest is None:
            return (
                {
                    "code": "market_data",
                    "name": "行情数据",
                    "status": "failed",
                    "detail": "Golden fixture：缺少行情与必要数据。",
                    "fallback_used": False,
                    "missing": ["前复权日线", "当前价格"],
                },
                {"data_date": None, "source": "数据不足"},
            )
        return (
            {
                "code": "market_data",
                "name": "行情数据",
                "status": "success",
                "detail": "Golden fixture：使用固定前复权日线。",
                "fallback_used": False,
                "missing": [],
                "source": latest.source,
                "data_time": latest.fetched_at.isoformat(),
            },
            {"data_date": latest.trade_date.isoformat(), "source": latest.source},
        )

    market = fixture.get("market", {"state": "上升", "risk": "低", "return_20d": 8.0})
    sector = fixture.get(
        "sector",
        {
            "state": "强",
            "industry": "测试行业",
            "relative_20d": 4.0,
            "is_mainline": True,
        },
    )
    monkeypatch.setattr("app.services.one_click_pipeline._sync_stock", stock_step)
    monkeypatch.setattr(
        "app.services.one_click_pipeline.refresh_company_research_if_needed",
        lambda db, symbol, **kwargs: {
            "symbol": symbol,
            "status": "fixture",
            "sections": {},
            "updated_at": datetime.now().isoformat(),
            "checked_at": datetime.now().isoformat(),
            "refreshed_sections": [],
            "missing_data": ["financials", "announcements", "valuation"],
            "freshness": [],
        },
    )
    monkeypatch.setattr(
        "app.services.one_click_pipeline._market_assessment",
        lambda db, provider: (
            market,
            {
                "code": "market_judgement",
                "name": "市场判断",
                "status": "success",
                "detail": "Golden fixture 市场状态。",
                "fallback_used": False,
                "missing": [],
                "source": "golden_market",
                "data_time": datetime.now().isoformat(),
            },
        ),
    )
    monkeypatch.setattr(
        "app.services.one_click_pipeline._sector_assessment",
        lambda db, provider, profile, market, refresh=False: (
            sector,
            {
                "code": "industry_judgement",
                "name": "行业判断",
                "status": "success",
                "detail": "Golden fixture 行业状态。",
                "fallback_used": False,
                "missing": [],
                "source": "golden_sector",
                "data_time": datetime.now().isoformat(),
            },
        ),
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"\d{4}-\d{2}-\d{2}(?:T[^\s；，。]*)?", "<DATE>", value)


def _golden_view(data: dict) -> dict:
    plan = data["plan"]
    pattern = plan.get("pattern")
    return {
        "decision": {
            "status": data["decision"]["status"],
            "label": data["decision"]["label"],
            "next_action": _normalize_text(data["decision"]["next_action"]),
        },
        "plan": {
            "status": plan["status"],
            "status_reason": [_normalize_text(item) for item in plan["status_reason"]],
            "missing_data": plan["missing_conditions"],
            "pattern": None
            if pattern is None
            else {
                "platform_range_pct": pattern["platform_range_pct"],
                "valid_platform": pattern["valid_platform"],
                "breakout": None
                if pattern["breakout"] is None
                else {
                    "volume_ratio": pattern["breakout"]["volume_ratio"],
                    "volume_confirmed": pattern["breakout"]["volume_confirmed"],
                },
                "pullback_seen": pattern["pullback_seen"],
                "pullback_volume_ratio": pattern["pullback_volume_ratio"],
                "pullback_shrinking": pattern["pullback_shrinking"],
                "platform_broken": pattern["platform_broken"],
                "turn_trigger_price": pattern["turn_trigger_price"],
                "turned_stronger": pattern["turned_stronger"],
            },
            "gates": {item["code"]: item["status"] for item in plan["gates"]},
            "gate_missing": {
                item["code"]: item["missing_conditions"]
                for item in plan["gates"]
                if item["missing_conditions"]
            },
            "buy_plan": {
                key: plan["buy_plan"].get(key)
                for key in ("buy_zone", "turn_trigger_price", "hard_stop")
            },
            "position": {
                key: plan["position_calculation"].get(key)
                for key in (
                    "risk_budget",
                    "per_share_risk",
                    "final_allowed_quantity",
                    "trial_quantity",
                    "trial_amount",
                    "maximum_loss",
                )
            },
            "holding": {
                key: plan["existing_position"].get(key)
                for key in (
                    "exists",
                    "quantity",
                    "cost_price",
                    "current_price",
                    "floating_profit",
                    "hard_stop_triggered",
                    "first_reduction_triggered",
                    "confirmation_add_allowed",
                )
            },
            "confirmation_add": {
                "allowed": plan["confirmation_add"]["allowed"],
                "prohibited": plan["confirmation_add"]["prohibited"],
            },
        },
        "ai": {"status": data["ai"]["status"], "error": data["ai"].get("error")},
    }


def _digest(value: dict) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _contract(value: dict) -> dict:
    plan = value["plan"]
    result = {
        "decision": value["decision"],
        "plan_status": plan["status"],
        "missing_data": plan["missing_data"],
        "gates": plan["gates"],
        "buy_plan": plan["buy_plan"],
        "position": plan["position"],
        "holding": plan["holding"],
        "ai_status": value["ai"]["status"],
    }
    if "ai_invariance" in value:
        result["ai_invariance"] = value["ai_invariance"]
    return result


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_strategy_engine_golden_master(case_name, client, session, monkeypatch):
    request = _read_json("inputs", case_name)
    fixture = _read_json("data", case_name)
    account = _create_account(client, fixture["account"])
    _seed_data(session, account["id"], fixture)
    _patch_external_context(monkeypatch, fixture)
    payload = {**request, "account_id": account["id"]}

    response = client.post("/api/v1/trade-plan-generator/analyze", json=payload)
    assert response.status_code == 200, response.text
    actual = _golden_view(response.json())

    if fixture.get("compare_ai_disabled"):
        enabled_response = client.post(
            "/api/v1/trade-plan-generator/analyze",
            json={**payload, "enable_ai": True},
        )
        assert enabled_response.status_code == 200, enabled_response.text
        enabled = _golden_view(enabled_response.json())
        actual["ai_invariance"] = {
            "decision_equal": actual["decision"] == enabled["decision"],
            "plan_equal": actual["plan"] == enabled["plan"],
            "enabled_ai_status": enabled["ai"]["status"],
        }

    if os.environ.get("GOLDEN_CAPTURE") == "1":
        captured = {"contract": _contract(actual), "sha256": _digest(actual)}
        print(f"GOLDEN_SNAPSHOT::{case_name}::{json.dumps(captured, ensure_ascii=False)}")
        return

    expected = _read_json("snapshots", case_name)
    assert _contract(actual) == expected["contract"]
    assert _digest(actual) == expected["sha256"]
