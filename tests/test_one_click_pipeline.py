from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import CompanyProfile, MarketDailyBar, MarketQuote, TradePlan
from app.schemas_workflow import TradePlanPreviewRequest
from app.services.trade_plan_generator import generate_trade_plan_preview


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


def seed_pattern(session, symbol="300502"):
    now = datetime.now()
    rows = []
    for index in range(260):
        close = 5 + index * 0.019
        rows.append((close, close + 0.08, close - 0.08, 100.0))
    for index in range(25):
        close = 10 + (index % 3 - 1) * 0.03
        rows.append((close, close + 0.12, close - 0.12, 100.0))
    rows.extend([(10.55, 10.65, 10.15, 220.0), (10.32, 10.48, 10.12, 55.0)])
    rows.append((10.82, 10.9, 10.3, 180.0))
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
    session.add(
        MarketQuote(
            symbol=symbol,
            name="测试公司",
            price=Decimal("10.82"),
            source="akshare_sina",
            source_api="stock_zh_a_spot",
            fetched_at=now,
        )
    )
    session.commit()


def benchmark_rows(direction="up"):
    start = date.today() - timedelta(days=79)
    rows = []
    for index in range(80):
        close = 100 + index if direction == "up" else 200 - index * 1.2
        rows.append({"date": start + timedelta(days=index), "close": close, "volume": 1000})
    return {"rows": rows, "source": "测试指数", "fetched_at": datetime.now()}


def seed_profile(session):
    session.add(
        CompanyProfile(
            symbol="300502",
            name="测试公司",
            industry="测试行业",
            market="创业板",
            main_business="测试业务",
            business_scope=None,
            website=None,
            source="巨潮资讯",
            source_url="https://example.test/profile",
            raw_data={},
            fetched_at=datetime.now(),
        )
    )
    session.commit()


def patch_benchmarks(monkeypatch, market="up", sector="up"):
    monkeypatch.setattr(
        "app.providers.akshare_provider.AKShareProvider.get_index_history",
        lambda self, symbol, start, end: benchmark_rows(market),
    )
    monkeypatch.setattr(
        "app.providers.akshare_provider.AKShareProvider.get_sector_history",
        lambda self, industry, start, end: benchmark_rows(sector),
    )
    monkeypatch.setattr(
        "app.services.one_click_pipeline.refresh_company_research_if_needed",
        lambda db, symbol, **kwargs: {
            "symbol": symbol,
            "status": "fresh",
            "sections": {},
            "updated_at": datetime.now().isoformat(),
            "checked_at": datetime.now().isoformat(),
            "refreshed_sections": [],
            "missing_data": ["financials", "announcements", "valuation"],
            "freshness": [],
        },
    )


def test_one_click_empty_position_generates_and_confirms_plan(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    response = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["decision"]["label"] == "允许试仓"
    assert [item["name"] for item in data["steps"]][-7:] == [
        "行情数据",
        "公司与行业识别",
        "市场判断",
        "行业判断",
        "公司风险与公开信息",
        "个股分析",
        "风险计算",
        "AI解释",
        "生成计划",
    ][-7:]
    assert data["plan"]["account"]["max_position_pct"] == 30
    assert data["plan"]["position_calculation"]["trial_quantity"] % 100 == 0
    assert data["decision_package"]["schema_version"] == "1.0"
    assert data["decision_package"]["quality_status"] == "SINGLE_SOURCE"
    assert data["decision_package"]["strategy_decision"]["rule_status"] == "READY"
    assert data["decision_package"]["risk_decision"]["hard_stop"] == str(
        data["plan"]["buy_plan"]["hard_stop"]
    )
    saved = client.post(
        f"/api/v1/trade-plan-generator/analyze/{data['run_id']}/confirm"
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["user_confirmed"] is True
    assert saved.json()["plan_version"] == 1


def test_one_click_holding_mode_uses_inline_position(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="200000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    response = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "持仓",
            "account_id": account["id"],
            "holding_quantity": 1000,
            "holding_cost_price": 9,
            "enable_ai": False,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["plan"]["existing_position"]["exists"] is True
    assert data["plan"]["existing_position"]["quantity"] == 1000
    assert data["decision"]["label"] == "允许条件式加仓"
    assert "降低成本" in "；".join(data["plan"]["confirmation_add"]["prohibited"])


def test_one_click_market_down_prohibits_entry(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch, market="down", sector="down")
    data = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    assert data["decision"]["label"] == "禁止买入"
    assert data["plan"]["market_assessment"]["risk"] == "高"
    assert data["plan"]["account"]["max_total_position_pct"] == 30


def test_one_click_creates_default_research_account(client, session, monkeypatch):
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    data = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "plan_capital": 300000,
            "enable_ai": False,
        },
    ).json()
    assert data["account"]["auto_created"] is True
    account = client.get(f"/api/v1/accounts/{data['account']['id']}").json()
    assert float(account["total_assets"]) == 300000


def test_one_click_wrapper_preserves_rule_numbers_for_same_generator_input(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    data = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    direct = generate_trade_plan_preview(
        session, TradePlanPreviewRequest(**data["generator_request"])
    )
    assert data["plan"]["status"] == direct["status"]
    assert data["plan"]["buy_plan"]["hard_stop"] == direct["buy_plan"]["hard_stop"]
    assert (
        data["plan"]["position_calculation"]["final_allowed_quantity"]
        == direct["position_calculation"]["final_allowed_quantity"]
    )
    assert (
        data["plan"]["position_calculation"]["trial_quantity"]
        == direct["position_calculation"]["trial_quantity"]
    )


@pytest.mark.parametrize("quality_status", ["STALE", "CONFLICTED", "MISSING"])
def test_untrusted_execution_data_blocks_ready_and_plan_freeze(
    quality_status, client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)

    def degraded_stock(db, symbol, provider, refresh):
        return (
            {
                "code": "market_data",
                "name": "行情数据",
                "status": "success",
                "detail": "测试质量门禁",
                "fallback_used": quality_status == "STALE",
                "missing": [],
                "quality_status": quality_status,
                "source": "test",
                "data_time": datetime.now().isoformat(),
            },
            {"data_date": date.today().isoformat(), "source": "test"},
        )

    monkeypatch.setattr("app.services.one_click_pipeline._sync_stock", degraded_stock)
    response = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["plan"]["deterministic_rule_status"] == "READY"
    assert data["plan"]["status"] == "WAIT"
    assert data["plan"]["current_buy_allowed"] is False
    assert data["decision_package"]["quality_status"] == quality_status
    assert data["decision_package"]["ready_allowed"] is False
    assert data["decision_package"]["freeze_allowed"] is False
    assert data["can_save"] is False
    confirm = client.post(
        f"/api/v1/trade-plan-generator/analyze/{data['run_id']}/confirm"
    )
    assert confirm.status_code == 422
    assert confirm.json()["error"]["code"] == "ANALYSIS_QUALITY_BLOCKED"
    assert session.query(TradePlan).count() == 0
