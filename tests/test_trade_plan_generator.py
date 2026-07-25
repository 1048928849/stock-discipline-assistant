from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.data_hub.contracts import DailyBar, DataProvider, ProviderMetadata, Quote
from app.data_hub.market_subjects import stock_daily_subject
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import (
    SHANGHAI_TZ,
    get_trading_calendar,
    shanghai_now,
    shanghai_today,
)
from app.models import (
    CompanyProfile,
    DataQualityRecord,
    MarketDailyBar,
    MarketQuote,
)
from app.providers.llm_provider import OpenAICompatibleProvider
from app.services.market_cache import persist_market_quote, replace_market_series
from app.services.research_cache import (
    persist_announcement_catalog,
    persist_company_profile,
)
from app.services.trade_plan_ai import validate_ai_output
from app.services import trade_plan_generator as trade_plan_generator_service
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


class GeneratorMarketProvider(DataProvider):
    metadata = ProviderMetadata(
        provider_id="generator-fixture",
        supported_capabilities=(
            "market.daily.qfq",
            "market.quote.realtime",
        ),
        priority=1,
        realtime_supported=True,
    )

    def __init__(
        self,
        bars,
        quote,
        *,
        provider_id="generator-fixture",
        priority=1,
    ):
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=(
                "market.daily.qfq",
                "market.quote.realtime",
                "market.quote.latest_close",
            ),
            priority=priority,
            realtime_supported=True,
        )
        self.bars = bars
        self.quote = quote

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def get_history(self, symbol, start, end):
        return self.bars

    def get_quote(self, symbol):
        return self.quote


def seed_pattern(session, symbol="300502", state="ready", price=None):
    now = shanghai_now()
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
    calendar = get_trading_calendar()
    latest_session = calendar.latest_completed_session(now)
    trade_dates = []
    candidate = latest_session
    while len(trade_dates) < len(rows):
        if calendar.is_session(candidate):
            trade_dates.append(candidate)
        candidate -= timedelta(days=1)
    trade_dates.reverse()
    start = trade_dates[0]
    bars = [
        DailyBar(
            symbol=symbol,
            trade_date=trade_dates[index],
            open=Decimal(str(close - 0.03)),
            high=Decimal(str(high)),
            low=Decimal(str(low)),
            close=Decimal(str(close)),
            volume=Decimal(str(volume)),
            adjustment="qfq",
            price_unit="CNY",
            volume_unit="share",
            observed_at=calendar.session_close_at(trade_dates[index]),
            source="akshare_tencent_qfq",
            fetched_at=now,
        )
        for index, (close, high, low, volume) in enumerate(rows)
    ]
    latest = price if price is not None else rows[-1][0]
    quote = Quote(
        symbol=symbol,
        name="测试公司",
        price=Decimal(str(latest)),
        quote_type="realtime",
        observed_at=now,
        price_unit="CNY",
        source="akshare_sina",
        source_api="stock_zh_a_spot",
        fetched_at=now,
    )
    registry = ProviderRegistry()
    registry.register(GeneratorMarketProvider(bars, quote))
    router = DataHubRouter(session, registry)
    history_result = router.get_history(symbol, start, latest_session)
    replace_market_series(
        session,
        router,
        history_result,
        history_result.require_value(),
        subject=stock_daily_subject(symbol, "qfq", "CNY", "share"),
        min_rows=250,
    )
    quote_result = router.get_quote(symbol)
    persist_market_quote(session, router, quote_result)
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


def _preview_with_fixed_shanghai_clock(client, session, monkeypatch):
    fixed = datetime(2026, 7, 24, 10, 0, tzinfo=SHANGHAI_TZ)
    monkeypatch.setattr(__name__ + ".shanghai_now", lambda: fixed)
    monkeypatch.setattr("app.data_hub.router.shanghai_now", lambda: fixed)
    monkeypatch.setattr("app.data_hub.effective_quality.shanghai_now", lambda: fixed)
    monkeypatch.setattr(
        trade_plan_generator_service,
        "shanghai_now",
        lambda: fixed,
        raising=False,
    )
    monkeypatch.setattr(
        trade_plan_generator_service,
        "shanghai_today",
        lambda: fixed.date(),
        raising=False,
    )
    account = create_account(client)
    seed_pattern(session)
    response = client.post(
        "/api/v1/trade-plan-generator/preview",
        json=payload(account["id"]),
    )
    assert response.status_code == 200, response.text
    return fixed, response.json()


def test_preview_generated_at_is_shanghai_aware(client, session, monkeypatch):
    fixed, preview = _preview_with_fixed_shanghai_clock(
        client, session, monkeypatch
    )
    generated_at = datetime.fromisoformat(preview["generated_at"])
    assert generated_at == fixed
    assert generated_at.utcoffset() == timedelta(hours=8)


def test_preview_current_dates_use_shanghai_date(client, session, monkeypatch):
    fixed, preview = _preview_with_fixed_shanghai_clock(
        client, session, monkeypatch
    )
    current_context = {
        item["source_id"]: item["data_date"]
        for item in preview["sources"]
        if item["source_id"] in {"account", "market_sector_context"}
    }
    assert current_context == {
        "account": fixed.date().isoformat(),
        "market_sector_context": fixed.date().isoformat(),
    }


def test_preview_hash_is_stable_with_injected_shanghai_clock(
    client, session, monkeypatch
):
    fixed, first = _preview_with_fixed_shanghai_clock(client, session, monkeypatch)
    response = client.post(
        "/api/v1/trade-plan-generator/preview",
        json=payload(first["account"]["id"]),
    )
    assert response.status_code == 200, response.text
    second = response.json()
    assert first["generated_at"] == second["generated_at"] == fixed.isoformat()
    assert first["preview_hash"] == second["preview_hash"]


def _replace_quote_scenario(session, scenario: str, *, price="10.82"):
    stored = session.query(MarketQuote).filter_by(symbol="300502").one_or_none()
    if scenario == "stale":
        stale_at = datetime.now() - timedelta(minutes=31)
        stored.observed_at = stale_at
        session.get(DataQualityRecord, stored.quality_record_id).observed_at = stale_at
    elif scenario == "missing":
        session.delete(stored)
    elif scenario == "conflicted":
        now = shanghai_now()
        first = Quote(
            symbol="300502",
            name="test company",
            price=Decimal(price),
            quote_type="realtime",
            observed_at=now,
            price_unit="CNY",
            source="quote-a",
            source_api="test",
            fetched_at=now,
        )
        second = Quote(
            symbol="300502",
            name="test company",
            price=Decimal("11.82"),
            quote_type="realtime",
            observed_at=now,
            price_unit="CNY",
            source="quote-b",
            source_api="test",
            fetched_at=now,
        )
        registry = ProviderRegistry()
        registry.register(
            GeneratorMarketProvider([], first, provider_id="quote-a", priority=1)
        )
        registry.register(
            GeneratorMarketProvider([], second, provider_id="quote-b", priority=2)
        )
        DataHubRouter(session, registry).get_quote("300502")
    elif scenario == "latest_close":
        session.delete(stored)
        session.flush()
        calendar = get_trading_calendar()
        now = calendar.session_close_at(calendar.latest_completed_session())
        close = Quote(
            symbol="300502",
            name="test company",
            price=Decimal(price),
            quote_type="latest_close",
            observed_at=now,
            price_unit="CNY",
            source="latest-close",
            source_api="test",
            fetched_at=now,
        )
        registry = ProviderRegistry()
        registry.register(
            GeneratorMarketProvider([], close, provider_id="latest-close")
        )
        router = DataHubRouter(session, registry)
        persist_market_quote(session, router, router.get_latest_close("300502"))
    else:
        raise ValueError(scenario)
    session.commit()


def _holding_preview(client, session, scenario: str, **holding_changes):
    account = create_account(client)
    seed_pattern(session)
    holding = {
        "account_id": account["id"],
        "symbol": "300502",
        "name": "test company",
        "quantity": 100,
        "cost_price": 9,
        "current_price": 10.82,
        "sector": "test sector",
        "stop_loss_price": 9.5,
        "target_price": 10.7,
        "price_source": "manual",
        **holding_changes,
    }
    assert client.post("/api/v1/holdings", json=holding).status_code == 201
    _replace_quote_scenario(session, scenario)
    response = client.post(
        "/api/v1/trade-plan-generator/preview",
        json=payload(account["id"]),
    )
    assert response.status_code == 200, response.text
    return response.json()


def seed_governed_analysis(session, monkeypatch):
    class GovernedResearchProvider(DataProvider):
        metadata = ProviderMetadata(
            provider_id="governed-research",
            supported_capabilities=(
                "fundamental.profile",
                "announcement.catalog",
            ),
            priority=1,
        )

        def health_check(self, probe: bool = False):
            return {"status": "healthy"}

        def company_profile(self, symbol):
            return {
                "name": "测试公司",
                "industry": "测试行业",
                "market": "创业板",
                "main_business": "测试业务",
            }

        def company_announcements(self, symbol, start, end):
            return [
                {
                    "公告标题": "最新公告",
                    "公告日期": end.isoformat(),
                    "公告链接": "https://example.test/announcement",
                    "目录来源": "exchange_test",
                }
            ]

    registry = ProviderRegistry()
    registry.register(GovernedResearchProvider())
    router = DataHubRouter(session, registry)
    profile_result = router.company_profile("300502")
    persist_company_profile(session, router, profile_result)
    today = shanghai_today()
    start = today - timedelta(days=3 * 366)
    end = today
    announcement_result = router.company_announcements("300502", start, end)
    persist_announcement_catalog(
        session,
        router,
        announcement_result,
        start=start,
        end=end,
    )
    session.commit()
    now = shanghai_now()
    calendar = get_trading_calendar()
    latest_session = calendar.latest_completed_session(now)
    trade_dates = []
    candidate = latest_session
    while len(trade_dates) < 80:
        if calendar.is_session(candidate):
            trade_dates.append(candidate)
        candidate -= timedelta(days=1)
    trade_dates.reverse()
    rows = {
        "rows": [
            {
                "date": trade_date,
                "close": 100 + index,
                "volume": 1000 + index,
            }
            for index, trade_date in enumerate(trade_dates)
        ],
        "source": "test",
        "fetched_at": now,
    }
    monkeypatch.setattr(
        "app.providers.akshare_provider.AKShareProvider.get_index_history",
        lambda *_: rows,
    )
    monkeypatch.setattr(
        "app.providers.akshare_provider.AKShareProvider.get_sector_history",
        lambda *_: rows,
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
            "missing_data": ["financials", "valuation"],
            "freshness": [],
        },
    )


def analyze_and_confirm(client, account_id, **changes):
    request = {
        "symbol": "300502",
        "position_mode": "空仓",
        "account_id": account_id,
        "enable_ai": False,
        **changes,
    }
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze", json=request
    ).json()
    assert analyzed["can_save"] is True, analyzed
    saved = client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert saved.status_code == 201, saved.text
    return analyzed, saved.json()


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


def test_generator_golden_rule_numbers_match_main(client, session):
    account = create_account(client)
    seed_pattern(session)
    preview = client.post(
        "/api/v1/trade-plan-generator/preview",
        json=payload(account["id"]),
    ).json()
    assert preview["status"] == "READY"
    assert preview["buy_plan"]["buy_zone"] == [10.4209, 10.5391]
    assert preview["buy_plan"]["hard_stop"] == 9.7023
    assert preview["position_calculation"]["final_allowed_quantity"] == 600
    assert preview["position_calculation"]["trial_quantity"] == 100
    assert preview["position_calculation"]["per_share_risk"] == 0.7777
    assert preview["position_calculation"]["maximum_loss"] == 77.77


def test_save_freezes_versions_and_history(client, session, monkeypatch):
    account = create_account(client)
    seed_pattern(session)
    seed_governed_analysis(session, monkeypatch)
    _, saved = analyze_and_confirm(client, account["id"], max_position_pct=20)
    _, second = analyze_and_confirm(client, account["id"], max_position_pct=20)
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


def test_direct_preview_stale_quote_skips_price_triggers(client, session):
    preview = _holding_preview(client, session, "stale")
    assert preview["current_price_quality"] == "STALE"
    assert preview["current_price_executable"] is False
    assert preview["price_trigger_evaluation_skipped"] is True
    assert preview["existing_position"]["current_price"] is None


def test_direct_preview_conflicted_quote_skips_price_triggers(client, session):
    preview = _holding_preview(client, session, "conflicted")
    assert preview["current_price_quality"] == "CONFLICTED"
    assert preview["current_price_executable"] is False
    assert preview["price_trigger_evaluation_skipped"] is True


def test_direct_preview_missing_quote_skips_price_triggers(client, session):
    preview = _holding_preview(client, session, "missing")
    assert preview["current_price_quality"] == "MISSING"
    assert preview["current_price_available"] is False
    assert preview["price_trigger_evaluation_skipped"] is True


def test_direct_preview_latest_close_does_not_trigger_realtime_rules(client, session):
    preview = _holding_preview(client, session, "latest_close", stop_loss_price=11)
    assert preview["current_price_quality"] == "MISSING"
    assert preview["existing_position"]["current_price"] is None
    assert preview["existing_position"]["hard_stop_triggered"] is False


def test_holding_hard_stop_not_evaluated_with_untrusted_quote(client, session):
    preview = _holding_preview(client, session, "conflicted", stop_loss_price=11)
    assert preview["existing_position"]["hard_stop_triggered"] is False


def test_holding_reduction_not_evaluated_with_untrusted_quote(client, session):
    preview = _holding_preview(client, session, "stale", target_price=10)
    assert preview["existing_position"]["first_reduction_triggered"] is False


def test_holding_add_not_evaluated_with_untrusted_quote(client, session):
    preview = _holding_preview(client, session, "missing", cost_price=8)
    assert preview["existing_position"]["confirmation_add_allowed"] is False


def test_preview_metadata_comes_from_selected_daily_lineage(client, session):
    account = create_account(client)
    seed_pattern(session)
    selected = session.query(MarketDailyBar).order_by(MarketDailyBar.trade_date).all()
    quality_record_id = selected[0].quality_record_id
    preview = client.post(
        "/api/v1/trade-plan-generator/preview", json=payload(account["id"])
    ).json()
    assert preview["market_data_quality_record_id"] == quality_record_id
    assert preview["data_date"] == selected[-1].trade_date.isoformat()
    assert preview["sources"][0]["name"] == selected[-1].source


def test_invalid_newer_bar_lineage_does_not_change_preview_metadata(client, session):
    account = create_account(client)
    seed_pattern(session)
    trusted = session.query(MarketDailyBar).order_by(MarketDailyBar.trade_date).all()
    trusted_record_id = trusted[0].quality_record_id
    session.add(
        MarketDailyBar(
            symbol="300502",
            trade_date=date.today() + timedelta(days=1),
            open=Decimal("20"),
            high=Decimal("20"),
            low=Decimal("20"),
            close=Decimal("20"),
            volume=Decimal("100"),
            adjustment="qfq",
            price_unit="CNY",
            volume_unit="share",
            observed_at=datetime.now(),
            quality_status="SINGLE_SOURCE",
            quality_record_id=None,
            source="invalid-newer-row",
            fetched_at=datetime.now(),
        )
    )
    session.commit()

    preview = client.post(
        "/api/v1/trade-plan-generator/preview", json=payload(account["id"])
    ).json()
    assert preview["market_data_quality_record_id"] == trusted_record_id
    assert preview["data_date"] == trusted[-1].trade_date.isoformat()
    assert preview["sources"][0]["name"] != "invalid-newer-row"


def evidence_package(symbol="300502"):
    package = {
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
    package["canonical_backend_fields"] = {
        "schema_version": "2.0",
        "computed_results": {"final_status": "WAIT", "quantity": 100},
        "raw_facts": [
            {
                "fact": "营收为100",
                "source_ids": ["financial:1"],
                "as_of": "2026-03-31",
                "confidence": "high",
            }
        ],
        "rule_conclusions": [
            {"code": "market", "status": "警告", "conclusion": "市场环境", "basis": "震荡"}
        ],
        "data_freshness": [
            {
                "category": "financial",
                "latest_at": "2026-03-31",
                "stale": False,
                "source_ids": ["financial:1"],
            }
        ],
        "provider_status": [],
    }
    return package


def valid_ai_result(package=None):
    package = package or evidence_package()
    return {
        **package["canonical_backend_fields"],
        "ai_summaries": [
            {
                "topic": "财务",
                "content": "营收为100，仍需结合现金流验证。",
                "source_ids": ["financial:1"],
                "confidence": "high",
            }
        ],
        "ai_inferences": [],
        "supporting_evidence": [
            {"claim": "营收为100", "source_ids": ["financial:1"], "confidence": "high"}
        ],
        "opposing_evidence": [],
        "conflicts": [],
        "missing_data": ["产业数据缺失"],
        "risk_events": [],
        "invalidation_conditions": [],
    }


def test_ai_schema_and_source_validation():
    output, validation = validate_ai_output(valid_ai_result(), evidence_package())
    assert output["ai_summaries"]
    assert output["ai_summaries"][0]["evidence_ids"] == ["financial:1"]
    assert validation["valid"] is True
    assert validation["cited_evidence_ids"] == ["financial:1"]
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
    invented["ai_summaries"][0]["content"] = "利润增长999"
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
        result = valid_ai_result(evidence_package)
        result["ai_summaries"] = [
            {
                "topic": "公司业务",
                "content": "公司业务来自已提供证据。",
                "source_ids": [f"profile:{profile.id}"],
                "confidence": "high",
            }
        ]
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


def test_ai_invalid_schema_retries_once_and_rule_plan_survives(client, session, monkeypatch):
    account = create_account(client)
    seed_pattern(session)
    request = payload(account["id"])
    settings = Settings(
        database_url="sqlite://",
        scheduler_enabled=False,
        llm_enabled=True,
        llm_base_url="https://llm.example.test/v1",
        llm_api_key="test-only",
        llm_model="test-model",
        llm_max_retries=1,
    )
    monkeypatch.setattr("app.services.trade_plan_ai.get_settings", lambda: settings)
    attempts = []

    def invalid_then_valid(self, package, correction=None):
        attempts.append(correction)
        if len(attempts) == 1:
            return {"output": {"invalid": True}, "usage": {}, "duration_ms": 1}
        output = {
            **package["canonical_backend_fields"],
            "ai_summaries": [],
            "ai_inferences": [],
            "supporting_evidence": [],
            "opposing_evidence": [],
            "conflicts": [],
            "missing_data": ["没有公司研究证据"],
            "risk_events": [],
            "invalidation_conditions": [],
        }
        return {
            "output": output,
            "usage": {"total_tokens": 20},
            "duration_ms": 2,
        }

    monkeypatch.setattr(
        OpenAICompatibleProvider, "analyze_trade_plan", invalid_then_valid
    )
    preview = client.post("/api/v1/trade-plan-generator/preview", json=request).json()
    result = client.post(
        "/api/v1/trade-plan-generator/ai",
        json={**request, "preview_hash": preview["preview_hash"]},
    ).json()
    assert result["status"] == "success"
    assert len(attempts) == 2
    assert attempts[1]
    assert preview["status"] == "READY"


def test_ai_timeout_is_audited_without_breaking_rule_plan(client, session, monkeypatch):
    account = create_account(client)
    seed_pattern(session)
    request = payload(account["id"])
    settings = Settings(
        database_url="sqlite://",
        scheduler_enabled=False,
        llm_enabled=True,
        llm_base_url="https://llm.example.test/v1",
        llm_api_key="test-only",
        llm_model="timeout-model",
        llm_max_retries=1,
    )
    monkeypatch.setattr("app.services.trade_plan_ai.get_settings", lambda: settings)

    def timeout(self, package, correction=None):
        raise TimeoutError("test timeout")

    monkeypatch.setattr(OpenAICompatibleProvider, "analyze_trade_plan", timeout)
    preview = client.post("/api/v1/trade-plan-generator/preview", json=request).json()
    result = client.post(
        "/api/v1/trade-plan-generator/ai",
        json={**request, "preview_hash": preview["preview_hash"]},
    ).json()
    assert result["status"] == "failed"
    assert "TimeoutError" in result["error"]
    assert preview["status"] == "READY"


def test_confirmed_plan_manual_fills_and_execution_deviations(
    client, session, monkeypatch
):
    account = create_account(client)
    seed_pattern(session)
    seed_governed_analysis(session, monkeypatch)
    analyzed, saved = analyze_and_confirm(client, account["id"])
    preview = analyzed["plan"]
    plan_id = saved["id"]
    assert saved["execution_status"] in {"entry_triggered", "waiting_entry"}
    buy_price = preview["buy_plan"]["buy_zone"][0]
    first = client.post(
        f"/api/v1/trade-plans/{plan_id}/execution/fills",
        json={
            "side": "买入",
            "quantity": 100,
            "price": buy_price,
            "fee": 0,
            "executed_at": datetime.now().isoformat(),
            "reason": "首次试仓",
            "trigger_confirmed": True,
            "is_test": True,
        },
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/v1/trade-plans/{plan_id}/execution/fills",
        json={
            "side": "买入",
            "quantity": 100,
            "price": round(buy_price * 0.95, 4),
            "fee": 0,
            "executed_at": datetime.now().isoformat(),
            "reason": "确认加仓",
            "trigger_confirmed": False,
            "is_test": True,
        },
    ).json()
    codes = {item["code"] for item in second["summary"]["violations"]}
    assert {"loss_averaging", "entry_without_trigger"}.issubset(codes)
    stopped = client.post(
        f"/api/v1/trade-plans/{plan_id}/execution/evaluate",
        json={
            "current_price": float(preview["buy_plan"]["hard_stop"]) - 0.01,
            "stop_triggered": True,
            "evidence": "测试检查硬止损",
        },
    ).json()
    assert stopped["execution_status"] == "stop_triggered"
    assert "missed_stop" in {
        item["code"] for item in stopped["summary"]["violations"]
    }
    final = client.post(
        f"/api/v1/trade-plans/{plan_id}/execution/fills",
        json={
            "side": "卖出",
            "quantity": 200,
            "price": float(preview["buy_plan"]["hard_stop"]),
            "fee": 0,
            "executed_at": datetime.now().isoformat(),
            "reason": "硬止损",
            "trigger_confirmed": True,
            "is_test": True,
        },
    ).json()
    assert final["execution_status"] == "closed"
    assert final["summary"]["realized_r"] is not None
