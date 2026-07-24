from datetime import date, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Lock, Thread

import pytest
from sqlalchemy import create_engine, insert, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.data_hub.contracts import DailyBar, DataProvider, ProviderMetadata, Quote
from app.data_hub.market_subjects import stock_daily_subject
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.database import Base
from app.errors import AppError
from app.models import (
    CompanyAnnouncement,
    CompanyProfile,
    CompanyResearchRefresh,
    DataQualityRecord,
    MarketQuote,
    PlanAnalysisRun,
    TradePlan,
)
from app.schemas_workflow import OneClickPlanRequest, TradePlanPreviewRequest
from app.services.one_click_pipeline import (
    confirm_one_click_plan,
    run_one_click_analysis,
)
from app.services.market_cache import persist_market_quote, replace_market_series
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
    bars = [
        DailyBar(
            symbol=symbol,
            trade_date=start + timedelta(days=index),
            open=Decimal(str(close - 0.03)),
            high=Decimal(str(high)),
            low=Decimal(str(low)),
            close=Decimal(str(close)),
            volume=Decimal(str(volume)),
            adjustment="qfq",
            price_unit="CNY",
            volume_unit="share",
            observed_at=datetime.combine(
                start + timedelta(days=index), datetime.min.time()
            ),
            source="akshare_tencent_qfq",
            fetched_at=now,
        )
        for index, (close, high, low, volume) in enumerate(rows)
    ]

    class PatternProvider(DataProvider):
        metadata = ProviderMetadata(
            provider_id="pattern-fixture",
            supported_capabilities=(
                "market.daily.qfq",
                "market.quote.realtime",
            ),
            priority=1,
            realtime_supported=True,
        )

        def health_check(self, probe: bool = False):
            return {"status": "healthy"}

        def get_history(self, requested_symbol, date_from, date_to):
            return bars

        def get_quote(self, requested_symbol):
            return Quote(
                symbol=requested_symbol,
                name="测试公司",
                price=Decimal("10.82"),
                quote_type="realtime",
                observed_at=now,
                price_unit="CNY",
                source="akshare_sina",
                source_api="stock_zh_a_spot",
                fetched_at=now,
            )

    registry = ProviderRegistry()
    registry.register(PatternProvider())
    router = DataHubRouter(session, registry)
    history = router.get_history(symbol, start, date.today())
    replace_market_series(
        session,
        router,
        history,
        history.require_value(),
        subject=stock_daily_subject(symbol, "qfq", "CNY", "share"),
        min_rows=250,
    )
    quote = router.get_quote(symbol)
    persist_market_quote(session, router, quote)
    session.commit()


def benchmark_rows(direction="up"):
    start = date.today() - timedelta(days=79)
    rows = []
    for index in range(80):
        close = 100 + index if direction == "up" else 200 - index * 1.2
        rows.append({"date": start + timedelta(days=index), "close": close, "volume": 1000})
    return {"rows": rows, "source": "测试指数", "fetched_at": datetime.now()}


class QuoteScenarioProvider(DataProvider):
    def __init__(
        self,
        provider_id: str,
        *,
        price: str = "10.82",
        quote_type: str = "realtime",
        priority: int = 1,
    ):
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=(
                "market.quote.realtime",
                "market.quote.latest_close",
            ),
            priority=priority,
            realtime_supported=True,
        )
        self.price = Decimal(price)
        self.quote_type = quote_type

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def get_quote(self, symbol):
        now = datetime.now()
        return Quote(
            symbol=symbol,
            name="测试公司",
            price=self.price,
            quote_type=self.quote_type,
            observed_at=now,
            price_unit="CNY",
            source=self.provider_id,
            source_api="quote-fixture",
            fetched_at=now,
        )


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
    now = datetime.now()
    session.add(
        CompanyResearchRefresh(
            symbol="300502",
            section="announcements",
            status="success",
            provider_id="exchange_test",
            source_name="exchange_test",
            row_count=1,
            cache_used=False,
            last_attempt_at=now,
            last_success_at=now,
            data_date=date.today(),
            stale_after=now + timedelta(hours=24),
            quality_status="SINGLE_SOURCE",
            observed_at=now,
            fetched_at=now,
            checked_at=now,
            scan_start=now,
            scan_end=now,
            normalized_digest="a" * 64,
            provider_observations=[],
            conflict_fields=[],
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
    announcement_scan = session.query(CompanyResearchRefresh).filter_by(
        section="announcements"
    ).one()
    announcement_scan.checked_at = datetime.now() + timedelta(seconds=1)
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
    session.query(CompanyResearchRefresh).filter_by(section="announcements").delete()
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


def _holding_analysis_with_quote_quality(
    client, session, monkeypatch, quality_status: str | None, quote_type="realtime"
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    quote = session.query(MarketQuote).one()
    if quality_status is None:
        session.delete(quote)
    elif quality_status == "STALE":
        stale_at = datetime.now() - timedelta(minutes=31)
        quote.observed_at = stale_at
        record = session.get(DataQualityRecord, quote.quality_record_id)
        record.observed_at = stale_at
    elif quality_status == "CONFLICTED":
        registry = ProviderRegistry()
        registry.register(QuoteScenarioProvider("quote-a", price="10.82"))
        registry.register(
            QuoteScenarioProvider("quote-b", price="11.82", priority=2)
        )
        DataHubRouter(session, registry).get_quote("300502")
    elif quote_type == "latest_close":
        session.delete(quote)
        session.flush()
        registry = ProviderRegistry()
        registry.register(
            QuoteScenarioProvider("latest-close", quote_type="latest_close")
        )
        router = DataHubRouter(session, registry)
        result = router.get_latest_close("300502")
        persist_market_quote(session, router, result)
    session.commit()
    return client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "持仓",
            "account_id": account["id"],
            "holding_quantity": 1000,
            "holding_cost_price": "9.8",
            "enable_ai": False,
        },
    ).json()


def test_stale_quote_blocks_holding_decision(client, session, monkeypatch):
    data = _holding_analysis_with_quote_quality(
        client, session, monkeypatch, "STALE"
    )
    assert data["decision"]["status"] == "WAIT"
    assert data["decision_package"]["freeze_allowed"] is False


def test_conflicted_quote_blocks_holding_decision(client, session, monkeypatch):
    data = _holding_analysis_with_quote_quality(
        client, session, monkeypatch, "CONFLICTED"
    )
    assert data["decision"]["status"] == "WAIT"


def test_missing_quote_blocks_price_triggered_decision(client, session, monkeypatch):
    data = _holding_analysis_with_quote_quality(client, session, monkeypatch, None)
    assert data["decision"]["status"] == "WAIT"


def test_quote_fallback_close_is_not_treated_as_realtime(
    client, session, monkeypatch
):
    data = _holding_analysis_with_quote_quality(
        client, session, monkeypatch, "SINGLE_SOURCE", quote_type="latest_close"
    )
    evidence = next(
        item
        for item in data["decision_package"]["evidence"]
        if item["capability"] == "market_quote"
    )
    assert evidence["payload"]["quote_type"] == "latest_close"
    assert evidence["payload"]["fallback_used"] is True


def test_fresh_daily_bar_does_not_hide_stale_quote(client, session, monkeypatch):
    data = _holding_analysis_with_quote_quality(
        client, session, monkeypatch, "STALE"
    )
    qualities = {
        item["capability"]: item["quality_status"]
        for item in data["decision_package"]["evidence"]
    }
    assert qualities["stock_daily_bars"] == "SINGLE_SOURCE"
    assert qualities["market_quote"] == "STALE"


def test_market_quote_is_required_evidence(client, session, monkeypatch):
    data = _holding_analysis_with_quote_quality(
        client, session, monkeypatch, "SINGLE_SOURCE"
    )
    package = data["decision_package"]
    assert "market_quote" in package["required_capabilities"]
    assert any(
        item["capability"] == "market_quote" and item["required"]
        for item in package["evidence"]
    )


def test_old_latest_announcement_with_fresh_catalog_scan_can_confirm(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    session.query(CompanyAnnouncement).one().published_date = date.today() - timedelta(
        days=30
    )
    session.commit()
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    confirmed = client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert confirmed.status_code == 201, confirmed.text


def test_stale_announcement_catalog_blocks_confirm(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    scan = session.query(CompanyResearchRefresh).filter_by(
        section="announcements"
    ).one()
    scan.checked_at = datetime.now() - timedelta(days=2)
    scan.quality_status = "STALE"
    session.commit()
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    confirmed = client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert confirmed.status_code == 422


def test_announcement_published_date_does_not_control_catalog_freshness(
    client, session, monkeypatch
):
    test_old_latest_announcement_with_fresh_catalog_scan_can_confirm(
        client, session, monkeypatch
    )


def _concurrent_database(tmp_path, monkeypatch, run_count=1):
    database = tmp_path / f"confirm-{run_count}.db"
    engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    patch_benchmarks(monkeypatch)
    with SessionLocal() as db:
        seed_pattern(db)
        seed_profile(db)
        first = run_one_click_analysis(
            db,
            OneClickPlanRequest(
                symbol="300502",
                position_mode="空仓",
                enable_ai=False,
            ),
        )
        run_ids = [first["run_id"]]
        for _ in range(run_count - 1):
            run_ids.append(
                run_one_click_analysis(
                    db,
                    OneClickPlanRequest(
                        symbol="300502",
                        position_mode="空仓",
                        account_id=first["account"]["id"],
                        enable_ai=False,
                    ),
                )["run_id"]
            )
    return engine, SessionLocal, run_ids


def _confirm_concurrently(SessionLocal, run_ids):
    barrier = Barrier(len(run_ids))
    lock = Lock()
    results = []

    def worker(run_id):
        with SessionLocal() as db:
            try:
                barrier.wait()
                saved = confirm_one_click_plan(db, run_id)
                outcome = ("ok", saved["id"], saved["plan_version"])
            except AppError as exc:
                outcome = ("error", exc.code)
            with lock:
                results.append(outcome)

    threads = [Thread(target=worker, args=(run_id,)) for run_id in run_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    return results


def test_concurrent_confirm_sqlite_creates_exactly_one_plan(tmp_path, monkeypatch):
    _, SessionLocal, run_ids = _concurrent_database(tmp_path, monkeypatch)
    results = _confirm_concurrently(SessionLocal, [run_ids[0], run_ids[0]])
    assert [item[0] for item in results].count("ok") == 1
    assert ("error", "ANALYSIS_ALREADY_CONFIRMED") in results
    with SessionLocal() as db:
        assert db.query(TradePlan).count() == 1


def test_concurrent_confirm_two_sessions_is_idempotent(tmp_path, monkeypatch):
    test_concurrent_confirm_sqlite_creates_exactly_one_plan(tmp_path, monkeypatch)


def test_concurrent_plan_version_allocation_is_unique(tmp_path, monkeypatch):
    _, SessionLocal, run_ids = _concurrent_database(tmp_path, monkeypatch, run_count=2)
    results = _confirm_concurrently(SessionLocal, run_ids)
    assert all(item[0] == "ok" for item in results)
    assert {item[2] for item in results} == {1, 2}


def test_analysis_run_id_database_uniqueness(tmp_path, monkeypatch):
    engine, SessionLocal, run_ids = _concurrent_database(tmp_path, monkeypatch)
    with SessionLocal() as db:
        confirm_one_click_plan(db, run_ids[0])
        plan = db.query(TradePlan).one()
        values = {
            column.name: getattr(plan, column.name)
            for column in TradePlan.__table__.columns
            if column.name not in {"id", "created_at", "updated_at"}
        }
        values["plan_version"] += 1
        with pytest.raises(IntegrityError):
            db.execute(insert(TradePlan).values(**values))
            db.commit()
    names = {item["name"] for item in inspect(engine).get_unique_constraints("trade_plans")}
    assert "uq_trade_plan_analysis_run" in names


def test_plan_version_database_uniqueness(tmp_path, monkeypatch):
    engine, SessionLocal, run_ids = _concurrent_database(tmp_path, monkeypatch)
    with SessionLocal() as db:
        confirm_one_click_plan(db, run_ids[0])
        plan = db.query(TradePlan).one()
        values = {
            column.name: getattr(plan, column.name)
            for column in TradePlan.__table__.columns
            if column.name not in {"id", "created_at", "updated_at"}
        }
        values["analysis_run_id"] = None
        with pytest.raises(IntegrityError):
            db.execute(insert(TradePlan).values(**values))
            db.commit()
    names = {item["name"] for item in inspect(engine).get_unique_constraints("trade_plans")}
    assert "uq_trade_plan_account_symbol_version" in names


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
    announcement_scan = session.query(CompanyResearchRefresh).filter_by(
        section="announcements"
    ).one()
    announcement_scan.checked_at = datetime.now() - timedelta(days=2)
    announcement_scan.quality_status = "STALE"
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
