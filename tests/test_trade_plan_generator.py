from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.models import CompanyProfile, MarketDailyBar, MarketQuote
from app.providers.llm_provider import OpenAICompatibleProvider
from app.services.trade_plan_ai import validate_ai_output
from app.services.trade_plan_generator import _floor_lot, ensure_generator_rule_version


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
    start = date.today() - timedelta(days=309)
    rows = []
    for index in range(260):
        close = 5 + index * 0.019
        rows.append((close, close + 0.08, close - 0.08, 100.0))
    for index in range(25):
        close = 10 + (index % 3 - 1) * 0.03
        low = 8.2 if state == "wide_stop" and index == 12 else close - 0.12
        rows.append((close, close + 0.12, low, 100.0))
    if state in {"ready", "pullback", "broken", "wide_stop"}:
        rows.append((10.55, 10.65, 10.15, 220.0))
        rows.append((10.32, 10.48, 10.12, 55.0))
        if state == "ready" or state == "wide_stop":
            rows.append((10.82, 10.9, 10.3, 180.0))
        elif state == "broken":
            rows.append((7.7, 10.0, 7.5, 240.0))
        else:
            rows.append((10.4, 10.5, 10.2, 70.0))
    else:
        rows.extend([(10.02, 10.15, 9.9, 100.0)] * 3)
    # 让最后一根始终落在今天，满足新鲜度检查。
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


def payload(account_id, symbol="300502", **changes):
    result = {
        "symbol": symbol,
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


@pytest.mark.parametrize(
    ("pattern_state", "expected"),
    [("flat", "WAIT"), ("pullback", "WAIT"), ("ready", "READY"), ("broken", "NO_TRADE")],
)
def test_plan_pattern_states(client, session, pattern_state, expected):
    account = create_account(client)
    seed_pattern(session, state=pattern_state)
    result = client.post("/api/v1/trade-plan-generator/preview", json=payload(account["id"]))
    assert result.status_code == 200, result.text
    assert result.json()["status"] == expected, [
        (item["code"], item["status"], item["evidence"])
        for item in result.json()["gates"]
        if item["status"] != "通过"
    ]
    assert len(result.json()["gates"]) == 12


def test_market_down_and_missing_data_are_explicit(client, session):
    account = create_account(client)
    seed_pattern(session)
    blocked = client.post(
        "/api/v1/trade-plan-generator/preview",
        json=payload(account["id"], market_state="下降"),
    ).json()
    assert blocked["status"] == "NO_TRADE"
    missing = client.post(
        "/api/v1/trade-plan-generator/preview",
        json=payload(account["id"], symbol="000001"),
    ).json()
    assert missing["status"] == "INSUFFICIENT_DATA"
    assert missing["missing_conditions"]


def test_stop_too_wide_and_under_one_lot(client, session):
    account = create_account(client)
    seed_pattern(session, state="wide_stop")
    rule = ensure_generator_rule_version(session)
    rule.parameters = {**rule.parameters, "maximum_stop_distance_pct": 1.0}
    session.commit()
    wide = client.post("/api/v1/trade-plan-generator/preview", json=payload(account["id"])).json()
    assert wide["status"] == "NO_TRADE"
    assert next(x for x in wide["gates"] if x["code"] == "stop")["status"] == "不通过"
    assert _floor_lot(199.9) == 100
    assert _floor_lot(99.9) == 0


def test_position_limits_and_risk_setting_change_quantity(client, session):
    account = create_account(client)
    seed_pattern(session)
    low = client.post(
        "/api/v1/trade-plan-generator/preview", json=payload(account["id"], risk_pct=0.2)
    ).json()
    high = client.post(
        "/api/v1/trade-plan-generator/preview", json=payload(account["id"], risk_pct=1)
    ).json()
    assert (
        low["position_calculation"]["final_allowed_quantity"]
        < high["position_calculation"]["final_allowed_quantity"]
    )
    limited = client.post(
        "/api/v1/trade-plan-generator/preview",
        json=payload(account["id"], max_position_pct=0.01),
    )
    assert limited.status_code == 200
    assert limited.json()["position_calculation"]["final_allowed_quantity"] == 0
    assert next(x for x in limited.json()["gates"] if x["code"] == "position")["status"] == "不通过"


def test_save_freezes_versions_and_history(client, session):
    account = create_account(client)
    seed_pattern(session)
    request = payload(account["id"])
    preview = client.post("/api/v1/trade-plan-generator/preview", json=request).json()
    saved = client.post(
        "/api/v1/trade-plan-generator/save",
        json={**request, "preview_hash": preview["preview_hash"]},
    )
    assert saved.status_code == 201, saved.text
    second = client.post(
        "/api/v1/trade-plan-generator/save",
        json={**request, "preview_hash": preview["preview_hash"]},
    ).json()
    history = client.get(
        f"/api/v1/trade-plan-generator/history?account_id={account['id']}&symbol=300502"
    ).json()
    assert second["plan_version"] == 2
    assert [item["plan_version"] for item in history] == [2, 1]
    assert history[0]["rule_version"] == history[1]["rule_version"]


def test_holding_confirmation_add_stop_reduction_and_industry_limit(client, session):
    account = create_account(client)
    seed_pattern(session)
    session.add(
        CompanyProfile(
            symbol="300502",
            name="测试公司",
            industry="光通信",
            market="创业板",
            main_business="测试业务",
            business_scope=None,
            website=None,
            source="巨潮资讯",
            source_url="https://example.test/profile",
            raw_data=None,
            fetched_at=datetime.now(),
        )
    )
    session.commit()
    holding = {
        "account_id": account["id"],
        "symbol": "300502",
        "name": "测试公司",
        "quantity": 100,
        "cost_price": 9,
        "current_price": 10.82,
        "sector": "光通信",
        "stop_loss_price": 9.5,
        "target_price": 10.7,
        "price_source": "manual",
    }
    assert client.post("/api/v1/holdings", json=holding).status_code == 201
    result = client.post("/api/v1/trade-plan-generator/preview", json=payload(account["id"])).json()
    assert result["existing_position"]["confirmation_add_allowed"] is True
    assert result["existing_position"]["first_reduction_triggered"] is True
    holding["cost_price"] = 12
    assert client.put("/api/v1/holdings/1", json=holding).status_code == 200
    loss = client.post("/api/v1/trade-plan-generator/preview", json=payload(account["id"])).json()
    assert loss["existing_position"]["confirmation_add_allowed"] is False
    holding["stop_loss_price"] = 11
    assert client.put("/api/v1/holdings/1", json=holding).status_code == 200
    stopped = client.post(
        "/api/v1/trade-plan-generator/holding-check", json=payload(account["id"])
    ).json()
    assert stopped["existing_position"]["hard_stop_triggered"] is True


def evidence_package(symbol="300502"):
    return {
        "symbol": symbol,
        "sources": [
            {
                "source_id": "financial:1",
                "symbol": symbol,
                "stale": False,
                "content": {"revenue": 100, "report_date": "2026-03-31"},
            }
        ],
    }


def valid_ai_result():
    return {
        "company_summary": "营收为100，仍需结合现金流验证。",
        "business_drivers": [],
        "financial_findings": ["营收为100"],
        "industry_findings": [],
        "valuation_findings": [],
        "supporting_evidence": [
            {"claim": "营收为100", "source_ids": ["financial:1"], "confidence": "high"}
        ],
        "counter_evidence": [],
        "risk_events": [],
        "logic_invalidation_conditions": [],
        "missing_information": ["产业数据缺失"],
        "conflicting_information": [],
        "questions_to_verify": [],
        "plain_language_summary": "资料有限。",
    }


def test_ai_schema_and_source_validation():
    output, validation = validate_ai_output(valid_ai_result(), evidence_package())
    assert output["financial_findings"]
    assert validation["valid"] is True
    bad = valid_ai_result()
    bad["supporting_evidence"][0]["source_ids"] = ["missing:9"]
    with pytest.raises(ValueError, match="不存在"):
        validate_ai_output(bad, evidence_package())


def test_ai_rejects_other_symbol_numbers_and_rule_override():
    package = evidence_package()
    other = evidence_package("600000")["sources"][0]
    package["sources"].append(other | {"source_id": "financial:2"})
    bad_symbol = valid_ai_result()
    bad_symbol["supporting_evidence"][0]["source_ids"] = ["financial:2"]
    with pytest.raises(ValueError, match="其他股票"):
        validate_ai_output(bad_symbol, package)
    invented = valid_ai_result()
    invented["financial_findings"] = ["利润增长999"]
    with pytest.raises(ValueError, match="不存在的数字"):
        validate_ai_output(invented, evidence_package())
    override = valid_ai_result() | {"final_status": "READY"}
    with pytest.raises(ValueError, match="专属字段"):
        validate_ai_output(override, evidence_package())


def test_ai_not_configured_does_not_break_rule_engine(client, session):
    account = create_account(client)
    seed_pattern(session)
    request = payload(account["id"])
    preview = client.post("/api/v1/trade-plan-generator/preview", json=request).json()
    response = client.post(
        "/api/v1/trade-plan-generator/ai",
        json={**request, "preview_hash": preview["preview_hash"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "not_configured"
    assert preview["status"] == "READY"


def test_ai_cache_hit_and_evidence_change_invalidates_cache(client, session, monkeypatch):
    account = create_account(client)
    seed_pattern(session)
    profile = CompanyProfile(
        symbol="300502",
        name="测试公司",
        industry="测试行业",
        market="创业板",
        main_business="初始可核验证据",
        business_scope=None,
        website=None,
        source="巨潮资讯",
        source_url="https://example.test/profile",
        raw_data=None,
        fetched_at=datetime.now(),
    )
    session.add(profile)
    session.commit()
    request = payload(account["id"])
    settings = Settings(
        database_url="sqlite://",
        scheduler_enabled=False,
        llm_enabled=True,
        llm_base_url="https://llm.example.test/v1",
        llm_api_key="secret",
        llm_model="test-model",
    )
    monkeypatch.setattr("app.services.trade_plan_ai.get_settings", lambda: settings)
    calls = []

    def fake_analysis(self, evidence_package, correction=None):
        calls.append(evidence_package["evidence_hash"])
        result = valid_ai_result()
        result["company_summary"] = "公司业务来自已提供证据。"
        result["financial_findings"] = []
        result["supporting_evidence"] = [
            {
                "claim": "公司业务来自已提供证据",
                "source_ids": [f"profile:{profile.id}"],
                "confidence": "high",
            }
        ]
        return {"output": result, "usage": {"total_tokens": 10}, "duration_ms": 5}

    monkeypatch.setattr(OpenAICompatibleProvider, "analyze_trade_plan", fake_analysis)
    preview = client.post("/api/v1/trade-plan-generator/preview", json=request).json()
    first = client.post(
        "/api/v1/trade-plan-generator/ai",
        json={**request, "preview_hash": preview["preview_hash"]},
    ).json()
    second = client.post(
        "/api/v1/trade-plan-generator/ai",
        json={**request, "preview_hash": preview["preview_hash"]},
    ).json()
    assert first["status"] == "success"
    assert second["cache_hit"] is True
    assert len(calls) == 1
    profile.main_business = "新增可核验证据"
    profile.fetched_at = datetime.now() + timedelta(seconds=1)
    session.commit()
    changed_preview = client.post("/api/v1/trade-plan-generator/preview", json=request).json()
    changed = client.post(
        "/api/v1/trade-plan-generator/ai",
        json={**request, "preview_hash": changed_preview["preview_hash"]},
    ).json()
    assert changed["cache_hit"] is False
    assert len(calls) == 2
