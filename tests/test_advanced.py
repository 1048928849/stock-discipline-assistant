from datetime import date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from app.config import Settings
from app.models import Account, Holding, MarketDailyBar, XPost
from app.models import DisciplineRule
from app.providers.x_provider import CollectedPost
from app.providers.akshare_provider import AKShareProvider
from app.providers.llm_provider import OpenAICompatibleProvider
from app.providers.market import DailyBar, ProviderUnavailableError
from app.providers.registry import ProviderRegistry
from app.providers.x_social_provider import XSocialClueProvider
from app.providers.x_provider import TWScrapeProvider, XUnavailableError
from app.services.backtests import run_backtest
from app.services.discipline import check_discipline


def test_discipline_detects_concentration_stop_and_missing_fields(session):
    account = Account(
        name="纪律测试",
        total_assets=Decimal("100000"),
        cash=Decimal("10000"),
        available_cash=Decimal("10000"),
    )
    session.add(account)
    session.flush()
    session.add(
        Holding(
            account_id=account.id,
            symbol="000001",
            name="测试股",
            quantity=9000,
            cost_price=Decimal("10"),
            current_price=Decimal("9"),
            sector="银行",
            stop_loss_price=Decimal("9.5"),
            price_source="manual",
        )
    )
    session.commit()
    alerts = check_discipline(session, account.id, persist=False)
    rules = {item["rule"] for item in alerts}
    assert {
        "single_position",
        "stop_loss",
        "missing_reason",
        "missing_exit",
        "sector_concentration",
        "total_position",
    }.issubset(rules)


def test_csv_import_deduplicates(client):
    account = client.post(
        "/api/v1/accounts",
        json={
            "name": "CSV",
            "total_assets": "100000",
            "cash": "100000",
            "available_cash": "100000",
        },
    ).json()
    csv_data = "symbol,side,quantity,price,traded_at,reason\n000001,BUY,100,10.5,2026-07-22T10:00:00,计划交易\n"
    first = client.post(
        f"/api/v1/trades/import-csv?account_id={account['id']}",
        files={"file": ("trades.csv", csv_data.encode(), "text/csv")},
    )
    second = client.post(
        f"/api/v1/trades/import-csv?account_id={account['id']}",
        files={"file": ("trades.csv", csv_data.encode(), "text/csv")},
    )
    assert first.json()["inserted"] == 1
    assert second.json()["duplicates"] == 1


def test_csv_invalid_row_does_not_erase_valid_rows(client):
    account = client.post(
        "/api/v1/accounts",
        json={
            "name": "CSV混合",
            "total_assets": "100000",
            "cash": "100000",
            "available_cash": "100000",
        },
    ).json()
    csv_data = "symbol,side,quantity,price,traded_at\n000001,BUY,100,10.5,2026-07-22T10:00:00\nbad,BUY,nope,10,invalid\n"
    result = client.post(
        f"/api/v1/trades/import-csv?account_id={account['id']}",
        files={"file": ("trades.csv", csv_data.encode(), "text/csv")},
    ).json()
    assert result["inserted"] == 1
    assert len(result["errors"]) == 1
    assert len(client.get(f"/api/v1/trades?account_id={account['id']}").json()) == 1


def test_discipline_threshold_is_configurable(session):
    account = Account(
        name="规则配置",
        total_assets=Decimal("100000"),
        cash=Decimal("50000"),
        available_cash=Decimal("50000"),
    )
    session.add(account)
    session.flush()
    session.add(
        DisciplineRule(
            code="single_position_pct",
            name="单股上限",
            severity="WARNING",
            enabled=True,
            config={"value": 60},
        )
    )
    session.add(
        Holding(
            account_id=account.id,
            symbol="000002",
            name="测试",
            quantity=5000,
            cost_price=Decimal("10"),
            current_price=Decimal("10"),
            buy_reason="理由",
            invalidation_condition="条件",
            stop_loss_price=Decimal("8"),
            price_source="manual",
        )
    )
    session.commit()
    rules = {item["rule"] for item in check_discipline(session, account.id, persist=False)}
    assert "single_position" not in rules


def test_akshare_adapter_maps_fixed_frames(monkeypatch):
    class FakeAK:
        @staticmethod
        def stock_zh_a_spot_em():
            return pd.DataFrame([{"代码": "000001", "名称": "平安银行", "最新价": 12.34}])

        @staticmethod
        def stock_zh_a_hist(**_):
            return pd.DataFrame(
                [
                    {
                        "日期": "2026-07-21",
                        "开盘": 12,
                        "收盘": 12.3,
                        "最高": 12.5,
                        "最低": 11.9,
                        "成交量": 1000,
                    }
                ]
            )

    monkeypatch.setattr(AKShareProvider, "_ak", staticmethod(lambda: FakeAK))
    provider = AKShareProvider()
    assert provider.get_quote("000001").price == Decimal("12.34")
    assert provider.get_history("000001", date(2026, 7, 1), date(2026, 7, 22))[0].close == Decimal(
        "12.3"
    )


def test_akshare_structure_change_is_explicit(monkeypatch):
    class BrokenAK:
        @staticmethod
        def stock_zh_a_spot_em():
            return pd.DataFrame([{"unexpected": 1}])

    monkeypatch.setattr(AKShareProvider, "_ak", staticmethod(lambda: BrokenAK))
    try:
        AKShareProvider().get_quote("000001")
        assert False, "结构变化必须报错"
    except ProviderUnavailableError as exc:
        assert "结构变化" in str(exc)


def test_akshare_history_falls_back_to_tencent(monkeypatch):
    class FakeAK:
        @staticmethod
        def stock_zh_a_hist(**_):
            raise ConnectionError("Eastmoney disconnected")

        @staticmethod
        def stock_zh_a_hist_tx(**_):
            return pd.DataFrame(
                [
                    {
                        "date": "2026-07-21",
                        "open": 12,
                        "close": 12.3,
                        "high": 12.5,
                        "low": 11.9,
                        "amount": 1000,
                    }
                ]
            )

    monkeypatch.setattr(AKShareProvider, "_ak", staticmethod(lambda: FakeAK))
    bars = AKShareProvider(retries=1).get_history("300502", date(2026, 7, 1), date(2026, 7, 22))
    assert bars[0].source == "akshare_tencent_qfq"
    assert bars[0].volume == Decimal("100000")


def test_akshare_quote_falls_back_to_sina(monkeypatch):
    class FakeAK:
        @staticmethod
        def stock_zh_a_spot_em():
            raise ConnectionError("Eastmoney disconnected")

        @staticmethod
        def stock_zh_a_spot():
            return pd.DataFrame([{"代码": "sz300502", "名称": "新易盛", "最新价": 123.45}])

    monkeypatch.setattr(AKShareProvider, "_ak", staticmethod(lambda: FakeAK))
    quote = AKShareProvider(retries=1).get_quote("300502")
    assert quote.source == "akshare_sina"
    assert quote.price == Decimal("123.45")


def test_market_sync_uses_latest_history_when_all_quotes_fail(client, monkeypatch):
    monkeypatch.setattr(
        AKShareProvider,
        "get_quote",
        lambda *_: (_ for _ in ()).throw(ProviderUnavailableError("quote unavailable")),
    )
    monkeypatch.setattr(
        AKShareProvider,
        "get_history",
        lambda *args: [
            DailyBar(
                symbol="300502",
                trade_date=date(2026, 7, 21),
                open=Decimal("500"),
                high=Decimal("560"),
                low=Decimal("490"),
                close=Decimal("551.77"),
                volume=Decimal("100000"),
                source="akshare_tencent_qfq",
                fetched_at=datetime.now(),
            )
        ],
    )
    response = client.post("/api/v1/market/sync?symbol=300502&days=365")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["fallback_used"] is True
    assert result["quote"]["price"] == "551.77"
    assert result["history_source"] == "akshare_tencent_qfq"


def test_x_cookie_missing_is_safe():
    provider = TWScrapeProvider(Settings(x_cookie=""))
    assert not provider.configured
    try:
        provider.collect(["from:example"])
        assert False, "缺少 Cookie 时必须显式禁用"
    except XUnavailableError as exc:
        assert "未配置" in str(exc)


def test_llm_structured_result_validation(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"translation_zh":"译文","summary":"摘要","category":"PCB","companies":[],"industry_chain":[],"information_type":"媒体报道","potential_positive":[],"potential_negative":[],"verification_items":["核对原文"]}'
                        }
                    }
                ]
            }

    monkeypatch.setattr("app.providers.llm_provider.httpx.post", lambda *args, **kwargs: Response())
    provider = OpenAICompatibleProvider(
        Settings(llm_base_url="https://example.invalid/v1", llm_api_key="secret", llm_model="test")
    )
    assert provider.analyze("source").category == "PCB"


def test_gemini_connection_uses_safe_headers_and_structured_output(monkeypatch):
    calls = []

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    def fake_get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        return Response({"id": "gemini-2.5-flash"})

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        return Response(
            {
                "choices": [{"message": {"content": '{"ok":true,"purpose":"connection_test"}'}}],
                "usage": {"total_tokens": 8},
            }
        )

    monkeypatch.setattr("app.providers.llm_provider.httpx.get", fake_get)
    monkeypatch.setattr("app.providers.llm_provider.httpx.post", fake_post)
    provider = OpenAICompatibleProvider(
        Settings(
            llm_provider="gemini",
            llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            llm_api_key="secret",
            llm_model="gemini-2.5-flash",
        )
    )
    result = provider.test_connection()
    assert result["status"] == "success"
    assert result["structured_output"] is True
    assert calls[0][2]["headers"]["x-goog-api-client"].startswith("stock-discipline-assistant")
    assert all("secret" not in str(call[1]) for call in calls)


def test_backtest_is_reproducible_and_costs_reduce_result():
    dates = pd.date_range("2025-01-01", periods=80, freq="B")
    closes = [10 + i * 0.05 for i in range(80)]
    frame = pd.DataFrame(
        {
            "Open": closes,
            "High": [x + 0.1 for x in closes],
            "Low": [x - 0.1 for x in closes],
            "Close": closes,
            "Volume": [10000] * 80,
        },
        index=dates,
    )
    free = run_backtest(frame, "fixed_stop", 100000, 0, 0, {})
    costly = run_backtest(frame, "fixed_stop", 100000, 0.001, 0.002, {})
    assert costly["disciplined"]["total_return_pct"] < free["disciplined"]["total_return_pct"]
    assert costly["commission"] == 0.001
    assert costly["slippage"] == 0.002
    assert "T+1" in costly["warning"]


def test_backtest_fixed_dataset_metrics_match_curves_and_trades():
    periods = 900
    dates = pd.date_range("2022-01-03", periods=periods, freq="B")
    rng = np.random.default_rng(3)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, periods))), index=dates)
    frame = pd.DataFrame(
        {
            "Open": close,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Volume": 100000,
        },
        index=dates,
    )
    result = run_backtest(
        frame,
        "moving_average",
        100000,
        0.0003,
        0.001,
        {"fast": 5, "slow": 20, "max_position": 0.2, "stop_pct": 0.08},
    )
    metrics = result["disciplined"]["metrics"]
    curve = result["curves"]["strategy"]
    trades = result["trades"]
    assert result["baseline"]["name"] == "买入并持有"
    assert result["strategy_rules"]["buy"]
    assert result["strategy_rules"]["sell"]
    assert result["strategy_rules"]["reentry"]
    assert result["sample"] == {
        "trade_count": len(trades),
        "minimum_required": 30,
        "sufficient": True,
        "label": "样本充足",
    }
    assert metrics["trade_count"] == len(trades)
    assert metrics["total_return_pct"] == round((curve[-1]["equity"] / 100000 - 1) * 100, 4)
    assert metrics["max_drawdown_pct"] == max(item["drawdown_pct"] for item in curve)
    assert metrics["win_rate_pct"] == round(
        sum(item["pnl"] > 0 for item in trades) / len(trades) * 100, 4
    )
    wins = [item["pnl"] for item in trades if item["pnl"] > 0]
    losses = [abs(item["pnl"]) for item in trades if item["pnl"] < 0]
    assert metrics["profit_loss_ratio"] == round(
        (sum(wins) / len(wins)) / (sum(losses) / len(losses)), 4
    )
    assert metrics["total_holding_days"] == sum(item["holding_days"] for item in trades)
    assert all(
        {
            "entry_date",
            "entry_price",
            "exit_date",
            "exit_price",
            "direction",
            "entry_reason",
            "exit_reason",
            "return_pct",
        }.issubset(item)
        for item in trades
    )


def test_fixed_stop_is_explicit_enters_without_ma_warmup_and_warns_on_small_sample():
    dates = pd.date_range("2026-01-01", periods=80, freq="B")
    close = pd.Series(np.linspace(10, 12, 80), index=dates)
    frame = pd.DataFrame(
        {"Open": close, "High": close + 0.1, "Low": close - 0.1, "Close": close, "Volume": 1e5},
        index=dates,
    )
    result = run_backtest(
        frame,
        "fixed_stop",
        100000,
        0,
        0,
        {"stop_pct": 0.12, "max_position": 0.25},
    )
    assert result["configuration"]["parameters"]["stop_pct"] == 0.12
    assert "12.00%" in result["strategy_rules"]["sell"]
    assert result["trades"][0]["entry_date"] <= dates[3].date().isoformat()
    assert result["sample"]["label"] == "样本不足"
    assert any("不足 3 年" in item for item in result["warnings"])
    assert "不能证明规则有效" in result["conclusion"]


def test_batch_strategy_uses_percentage_points_correctly():
    dates = pd.date_range("2024-01-01", periods=60, freq="B")
    close = pd.Series([100.0] * 60, index=dates)
    frame = pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1e5},
        index=dates,
    )
    result = run_backtest(
        frame,
        "batch_buy",
        100000,
        0,
        0,
        {"stop_pct": 0.08, "max_position": 0.3, "batch_drop": 0.03, "take_profit": 0.1},
    )
    # 价格不动时不能把 0.1% 错当成 10% 止盈而频繁退出。
    assert result["disciplined"]["trade_count"] == 1
    assert result["trades"][0]["exit_reason"] == "回测区间结束，强制平仓"

    batch_prices = ([100, 100, 96, 96, 92, 92, 110, 110] * 8)[:60]
    batch_close = pd.Series(batch_prices, index=dates, dtype=float)
    batch_frame = pd.DataFrame(
        {
            "Open": batch_close,
            "High": batch_close,
            "Low": batch_close,
            "Close": batch_close,
            "Volume": 1e5,
        },
        index=dates,
    )
    grouped = run_backtest(
        batch_frame,
        "batch_buy",
        100000,
        0,
        0,
        {"stop_pct": 0.2, "max_position": 0.3, "batch_drop": 0.03, "take_profit": 0.1},
    )
    # 同一次分批建仓的多个成交批次应合并为一笔完整交易样本。
    assert grouped["disciplined"]["trade_count"] == 8
    assert "第一批建仓" in grouped["trades"][0]["entry_reason"]
    assert "第 3 批加仓" in grouped["trades"][0]["entry_reason"]


def test_backtest_api_with_database_bars(client, session):
    start = date(2025, 1, 1)
    for index in range(40):
        price = Decimal("10") + Decimal(index) / 10
        session.add(
            MarketDailyBar(
                symbol="000001",
                trade_date=start + timedelta(days=index),
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=10000,
                source="fixture",
                fetched_at=datetime.now(),
            )
        )
    session.commit()
    response = client.post(
        "/api/v1/backtests",
        json={
            "symbol": "000001",
            "strategy": "fixed_stop",
            "date_from": str(start),
            "date_to": str(start + timedelta(days=39)),
            "commission": 0.001,
            "slippage": 0.001,
            "parameters": {"stop_pct": 0.08},
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["metrics"]["warning"]


def test_unconfigured_advanced_status_and_ai_error(client, session):
    status_result = client.get("/api/v1/settings/status").json()
    assert status_result["x"] == "not_configured"
    assert status_result["secrets"]["x_cookie"] == ""
    post = XPost(
        platform="x",
        post_id="1",
        author="a",
        content="content",
        published_at=datetime.now(),
        metrics={},
        url="https://x.com/a/status/1",
    )
    session.add(post)
    session.commit()
    response = client.post(f"/api/v1/ai/analyze/{post.id}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LLM_UNAVAILABLE"


def test_x_sync_deduplicates_posts(client, monkeypatch):
    client.post(
        "/api/v1/x/watch-queries",
        json={"name": "测试", "expression": "semiconductor", "enabled": True},
    )
    post = CollectedPost(
        post_id="123",
        author="author",
        content="public post",
        published_at=datetime.now(),
        metrics={"like": 1},
        url="https://x.com/a/status/123",
    )
    monkeypatch.setattr(
        "app.providers.x_social_provider.XSocialClueProvider.search",
        lambda *_args, **_kwargs: [post.__dict__],
    )
    registry = ProviderRegistry()
    registry.register(XSocialClueProvider(Settings(x_cookie="test-cookie")))
    monkeypatch.setattr(
        "app.services.data_sources.build_provider_registry", lambda: registry
    )
    first = client.post("/api/v1/x/sync").json()
    second = client.post("/api/v1/x/sync").json()
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert len(client.get("/api/v1/x/posts").json()) == 1
