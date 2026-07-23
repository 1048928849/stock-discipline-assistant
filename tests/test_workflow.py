from datetime import date, datetime, timedelta
from decimal import Decimal

from app.models import MarketDailyBar, MarketQuote
from app.providers.akshare_provider import AKShareProvider
from app.providers.market import ProviderUnavailableError


def account_payload():
    return {
        "name": "主账户",
        "total_assets": "100000.0000",
        "cash": "60000.0000",
        "available_cash": "55000.0000",
    }


def holding_payload(account_id: int):
    return {
        "account_id": account_id,
        "symbol": "600519",
        "name": "贵州茅台",
        "quantity": 10,
        "cost_price": "1500.0000",
        "current_price": "1600.0000",
        "sector": "白酒",
        "buy_reason": "仅用于测试记录",
        "invalidation_condition": "基本面假设失效",
        "stop_loss_price": "1400.0000",
        "max_position_pct": "20.0000",
        "price_source": "manual",
    }


def ready_plan(account_id: int):
    return {
        "account_id": account_id,
        "symbol": "300502",
        "name": "新易盛",
        "trade_mode": "日线趋势波段",
        "decision_level": "日线",
        "market_state": "上升",
        "sector_state": "强",
        "large_cycle_direction": "向上",
        "industry_logic": "行业需求与订单需要用后续数据持续验证",
        "company_logic": "营收、利润和现金流需要按季度验证",
        "technical_structure": "平台放量突破后缩量回踩，再次转强才触发",
        "buy_zone_low": 19.5,
        "buy_zone_high": 20,
        "initial_stop": 18.8,
        "invalidation_condition": "放量跌破平台且触发硬止损",
        "target_plan": "趋势未破坏时移动止盈",
        "risk_pct": 1,
        "max_position_pct": 25,
        "add_condition": "回踩不破且浮盈覆盖新增风险",
        "reduce_condition": "跌破短期趋势线先减仓",
        "exit_condition": "硬止损、核心逻辑失效或趋势确认破坏",
        "no_trade_condition": "市场转弱、止损过远或高开追涨",
        "data_date": "2026-07-22",
    }


def test_trade_plan_calculates_a_share_lots_and_seven_gates(client):
    account = client.post("/api/v1/accounts", json=account_payload()).json()
    response = client.post("/api/v1/trade-plans", json=ready_plan(account["id"]))
    assert response.status_code == 201
    plan = response.json()
    assert plan["status"] == "READY"
    assert plan["planned_quantity"] == 800
    assert plan["planned_risk_amount"] == 960
    assert len(plan["checks"]) == 7
    assert all(item["status"] in {"通过", "警告"} for item in plan["checks"])
    assert plan["rule_version"] == "1.0.0"
    assert plan["source"] == "用户事前计划 + 规则化仓位计算"


def test_failed_gate_blocks_plan_and_missing_fields_stay_explicit(client):
    account = client.post("/api/v1/accounts", json=account_payload()).json()
    payload = ready_plan(account["id"])
    payload["market_state"] = "下降"
    payload["industry_logic"] = None
    response = client.post("/api/v1/trade-plans", json=payload)
    assert response.status_code == 201
    plan = response.json()
    assert plan["status"] == "BLOCKED"
    assert "不要执行买入" in plan["next_action"]
    logic = next(item for item in plan["checks"] if item["name"] == "行业与公司逻辑")
    assert logic["status"] == "无法判断"
    assert "产业逻辑" in logic["missing_data"]


def test_position_management_never_relaxes_triggered_hard_stop(client):
    account = client.post("/api/v1/accounts", json=account_payload()).json()
    holding = holding_payload(account["id"])
    holding["current_price"] = "1300"
    item = client.post("/api/v1/holdings", json=holding).json()
    managed = client.get("/api/v1/positions/management").json()[0]
    assert managed["hard_stop_triggered"] is True
    assert managed["allow_add"] is False
    assert "不得放宽止损" in managed["next_action"]
    assessment = client.post(
        f"/api/v1/positions/{item['id']}/assessment",
        json={
            "stage": "趋势",
            "logic_status": "成立",
            "invalidation_triggered": False,
            "supporting_evidence": ["用户认为逻辑仍在"],
            "opposing_evidence": [],
            "next_action": "继续加仓",
        },
    ).json()
    assert assessment["allow_add"] is False
    assert "不允许" in assessment["next_action"]


def test_dashboard_refuses_to_invent_market_state(client):
    data = client.get("/api/v1/dashboard/summary").json()
    assert data["market"]["state"] == "无法判断"
    assert data["market"]["status"] == "insufficient"
    assert data["market"]["missing_data"]
    assert data["priorities"]


def test_rule_parameters_are_versioned_without_rewriting_old_plan(client):
    account = client.post("/api/v1/accounts", json=account_payload()).json()
    plan = client.post("/api/v1/trade-plans", json=ready_plan(account["id"])).json()
    changed = client.post(
        "/api/v1/rule-versions",
        json={
            "single_trade_risk_pct": 0.5,
            "max_single_position_pct": 20,
            "max_account_drawdown_pct": 6,
            "beginner_min_holdings": 2,
            "beginner_max_holdings": 4,
        },
    ).json()
    assert changed["version"] == "1.0.1"
    assert client.get(f"/api/v1/trade-plans/{plan['id']}").json()["rule_version"] == "1.0.0"


def test_market_sync_explicitly_returns_last_successful_cache(client, session, monkeypatch):
    old_time = datetime.now() - timedelta(days=2)
    session.add(
        MarketQuote(
            symbol="300502",
            name="新易盛",
            price=Decimal("99.5"),
            source="akshare_sina",
            source_api="stock_zh_a_spot",
            fetched_at=old_time,
        )
    )
    session.add(
        MarketDailyBar(
            symbol="300502",
            trade_date=date.today() - timedelta(days=2),
            open=Decimal("98"),
            high=Decimal("101"),
            low=Decimal("97"),
            close=Decimal("99.5"),
            volume=Decimal("10000"),
            source="akshare_tencent_qfq",
            fetched_at=old_time,
        )
    )
    session.commit()

    def unavailable(*_):
        raise ProviderUnavailableError("网络断开")

    monkeypatch.setattr(AKShareProvider, "get_quote", unavailable)
    monkeypatch.setattr(AKShareProvider, "get_history", unavailable)
    result = client.post("/api/v1/market/sync?symbol=300502&days=365").json()
    assert result["status"] == "stale_fallback"
    assert result["quote"]["status"] == "stale"
    assert result["quote"]["price"] == "99.5000"
    assert "请勿视为当前成交价" in result["message"]
