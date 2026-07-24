from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import (
    CompanyAnnouncement,
    CompanyProfile,
    MarketDailyBar,
    MarketQuote,
    PlanAnalysisRun,
    TradePlan,
)
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
    session.add(
        CompanyAnnouncement(
            symbol="300502",
            title="测试公司最新公告",
            announcement_category="其他公告",
            risk_level="无",
            published_date=date.today(),
            catalog_source="exchange_test",
            exchange="SZSE",
            url="https://example.test/announcement",
            source_document_url=None,
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
    assert data["decision_package"]["schema_version"] == "1.1"
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


def test_direct_save_cannot_bypass_blocked_decision_package(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)

    def missing_stock(db, symbol, provider, refresh):
        return (
            {
                "code": "market_data",
                "name": "market data",
                "status": "failed",
                "detail": "required market data missing",
                "fallback_used": False,
                "missing": ["stock_daily_bars"],
                "quality_status": "MISSING",
                "source": "test",
                "data_time": datetime.now().isoformat(),
            },
            {"data_date": date.today().isoformat(), "source": "test"},
        )

    monkeypatch.setattr("app.services.one_click_pipeline._sync_stock", missing_stock)
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    assert analyzed["can_save"] is False
    direct = client.post(
        "/api/v1/trade-plan-generator/save",
        json={
            **analyzed["generator_request"],
            "preview_hash": analyzed["plan"]["preview_hash"],
        },
    )
    assert direct.status_code == 422
    assert direct.json()["error"]["code"] == "DECISION_PACKAGE_REQUIRED"
    assert session.query(TradePlan).count() == 0


def test_confirm_rechecks_quality_after_analysis_age(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    run = session.get(PlanAnalysisRun, analyzed["run_id"])
    run.created_at = datetime.now() - timedelta(days=2)
    session.commit()
    confirmed = client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert confirmed.status_code == 422
    assert confirmed.json()["error"]["code"] == "DECISION_PACKAGE_EXPIRED"


def test_confirm_rejects_changed_quality_snapshot(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    run = session.get(PlanAnalysisRun, analyzed["run_id"])
    snapshot = dict(run.result_snapshot)
    package = dict(snapshot["decision_package"])
    package["quality_snapshot"] = {
        **package.get("quality_snapshot", {}),
        "market_data": {"quality_status": "CONFLICTED"},
    }
    snapshot["decision_package"] = package
    run.result_snapshot = snapshot
    session.commit()
    confirmed = client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert confirmed.status_code == 422
    assert confirmed.json()["error"]["code"] == "DECISION_PACKAGE_CHANGED"


def test_confirm_rejects_required_source_change(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    announcement = session.query(CompanyAnnouncement).one()
    announcement.fetched_at = datetime.now() + timedelta(seconds=1)
    session.commit()
    confirmed = client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert confirmed.status_code == 422
    assert confirmed.json()["error"]["code"] == "DECISION_PACKAGE_CHANGED"


def test_same_analysis_run_cannot_create_two_formal_plans(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    endpoint = (
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert client.post(endpoint).status_code == 201
    duplicate = client.post(endpoint)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "ANALYSIS_ALREADY_CONFIRMED"
    assert session.query(TradePlan).count() == 1


def test_legacy_analysis_without_decision_package_cannot_confirm(client, session):
    account = create_account(client, assets="300000", cash="300000")
    run = PlanAnalysisRun(
        symbol="300502",
        account_id=account["id"],
        position_mode="空仓",
        status="success",
        request_snapshot={},
        pipeline_steps=[],
        result_snapshot={"plan": {"preview_hash": "a" * 64}},
    )
    session.add(run)
    session.commit()
    response = client.post(f"/api/v1/trade-plan-generator/analyze/{run.id}/confirm")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DECISION_PACKAGE_REQUIRED"


def test_required_announcements_missing_blocks_freeze(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    session.query(CompanyAnnouncement).delete()
    session.commit()
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
    assert data["decision_package"]["strategy_decision"]["rule_status"] == "READY"
    assert data["decision_package"]["strategy_decision"]["executable_status"] == "WAIT"
    assert data["decision_package"]["freeze_allowed"] is False
    assert data["decision_package"]["quality_status"] == "MISSING"


def test_optional_financials_missing_does_not_change_rule_status(
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
    package = data["decision_package"]
    assert package["strategy_decision"]["rule_status"] == "READY"
    assert package["freeze_allowed"] is True
    assert "financials" in package["research_decision"]["missing_optional_evidence"]


def test_optional_financials_missing_reduces_research_completeness(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    package = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()["decision_package"]
    assert package["research_decision"]["research_completeness"] < 100


def test_strategy_can_promote_financials_to_required(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    package = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
            "required_research_capabilities": ["financials"],
        },
    ).json()["decision_package"]
    assert "financials" in package["required_capabilities"]
    assert package["quality_status"] == "MISSING"
    assert package["freeze_allowed"] is False


def test_stale_required_evidence_blocks_freeze(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    announcement = session.query(CompanyAnnouncement).one()
    announcement.fetched_at = datetime.now() - timedelta(days=2)
    session.commit()
    patch_benchmarks(monkeypatch)
    package = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()["decision_package"]
    assert package["quality_status"] == "STALE"
    assert package["freeze_allowed"] is False


def test_stale_optional_evidence_is_reported_but_not_blocking(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    monkeypatch.setattr(
        "app.services.one_click_pipeline.run_ai_analysis",
        lambda db, request: {
            "status": "success",
            "id": None,
            "sources": [
                {
                    "source_id": "financial:test",
                    "symbol": "300502",
                    "category": "financial",
                    "source_name": "test",
                    "stale": True,
                    "content": {"revenue": 100},
                }
            ],
            "result": {"missing_data": []},
        },
    )
    package = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": True,
        },
    ).json()["decision_package"]
    assert package["quality_status"] == "SINGLE_SOURCE"
    assert package["freeze_allowed"] is True
    assert "financials" in package["research_decision"]["missing_optional_evidence"]
