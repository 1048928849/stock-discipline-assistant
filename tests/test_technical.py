from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from app.data_hub.contracts import DailyBar, DataProvider, ProviderMetadata
from app.data_hub.market_subjects import stock_daily_subject
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import get_trading_calendar, shanghai_now
from app.models import Account, Holding
from app.services.market_cache import replace_market_series
from app.services.technical import (
    _decayed_touch_weight,
    _touch_groups,
    analyze_frame,
    backtest_signals,
    compare_states,
    prepare_indicators,
)


def price_frame(periods: int = 180) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=periods, freq="B")
    close = pd.Series(10 + np.linspace(0, 6, periods) + np.sin(np.arange(periods) / 7), index=index)
    return pd.DataFrame(
        {
            "Open": close - 0.05,
            "High": close + 0.2,
            "Low": close - 0.2,
            "Close": close,
            "Volume": 100000 + np.arange(periods) * 100,
        },
        index=index,
    )


class TechnicalHistoryProvider(DataProvider):
    metadata = ProviderMetadata(
        provider_id="technical-fixture",
        supported_capabilities=("market.daily.qfq",),
        priority=1,
    )

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def get_history(self, symbol, start, end):
        now = shanghai_now()
        calendar = get_trading_calendar()
        latest = min(end, calendar.latest_completed_session(now))
        trade_dates = []
        candidate = latest
        while len(trade_dates) < 260:
            if calendar.is_session(candidate):
                trade_dates.append(candidate)
            candidate -= timedelta(days=1)
        trade_dates.reverse()
        rows = []
        for index, trade_date in enumerate(trade_dates):
            price = Decimal("10") + Decimal(index) / Decimal("50")
            rows.append(
                DailyBar(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=price,
                    high=price + Decimal("0.2"),
                    low=price - Decimal("0.2"),
                    close=price,
                    volume=Decimal(100000 + index * 100),
                    adjustment="qfq",
                    price_unit="CNY",
                    volume_unit="share",
                    observed_at=calendar.session_close_at(trade_date),
                    source=self.provider_id,
                    fetched_at=now,
                )
            )
        return rows


def test_pandas_ta_analysis_produces_indicators_and_levels():
    result = analyze_frame(price_frame(), "000001", "fixture_qfq")
    assert result["adjustment"] == "前复权(qfq)"
    assert result["indicators"]["ma20"] is not None
    assert result["indicators"]["macd"] is not None
    assert result["indicators"]["rsi14"] is not None
    assert result["signals"]["ma_alignment"] in {"多头排列", "空头排列", "均线交织"}
    assert result["support_levels"] or result["resistance_levels"]
    assert len(result["chart"]) == 180
    assert "不构成买卖指令" in result["risk_notice"]
    assert result["current_price"] == result["indicators"]["close"]
    assert result["analysis_date"] == result["trade_date"]
    assert result["kline_period"]["frequency"] == "日线"
    assert result["kline_period"]["bars"] == 180
    assert all(item["upper"] < result["current_price"] for item in result["support_levels"])
    assert all(item["lower"] > result["current_price"] for item in result["resistance_levels"])
    assert all(
        item["lower"] <= result["current_price"] <= item["upper"]
        for item in result["contest_zones"]
    )
    for item in result["support_levels"] + result["resistance_levels"] + result["contest_zones"]:
        assert item["width_atr"] <= 1
        assert item["weighted_touches"] <= item["touches"]
    assert "breakout_confirmation" in result["level_conditions"]
    assert "breakdown_confirmation" in result["level_conditions"]


def test_level_touches_merge_consecutive_bars_and_decay_old_tests():
    hits = pd.Series([False, True, True, True, False, True, True, False])
    assert _touch_groups(hits) == [1, 5]
    old_weight = _decayed_touch_weight([0], 251)
    recent_weight = _decayed_touch_weight([250], 251)
    assert recent_weight > old_weight
    assert old_weight < 0.2


def test_live_price_controls_support_and_resistance_classification():
    result = analyze_frame(price_frame(), "000001", "fixture_qfq", current_price=12.0)
    assert result["current_price"] == 12.0
    assert result["analysis_close"] != result["current_price"]
    assert all(item["upper"] < 12.0 for item in result["support_levels"])
    assert all(item["lower"] > 12.0 for item in result["resistance_levels"])
    assert all(item["lower"] <= 12.0 <= item["upper"] for item in result["contest_zones"])


def test_volume_breakout_is_causal_and_detected():
    frame = price_frame()
    frame.iloc[-1, frame.columns.get_loc("Close")] = frame["High"].iloc[:-1].max() + 1
    frame.iloc[-1, frame.columns.get_loc("High")] = frame["Close"].iloc[-1] + 0.2
    frame.iloc[-1, frame.columns.get_loc("Volume")] = frame["Volume"].iloc[-21:-1].mean() * 2
    indicators = prepare_indicators(frame)
    assert bool(indicators["VOLUME_BREAKOUT"].iloc[-1])
    # 前一日信号不应被最后一日的数据反向改写。
    original = prepare_indicators(price_frame())
    assert bool(indicators["VOLUME_BREAKOUT"].iloc[-2]) == bool(
        original["VOLUME_BREAKOUT"].iloc[-2]
    )


def test_vectorbt_reports_each_signal_history():
    result = backtest_signals(price_frame(), holding_days=10, fees=0.001, slippage=0.001)
    assert result["engine"] == "vectorbt"
    assert set(result["signals"]) == {
        "均线多头形成",
        "MACD金叉",
        "放量突破",
        "缩量回调",
        "MACD底背离",
    }
    for metrics in result["signals"].values():
        assert metrics["holding_days"] == 10
        assert metrics["signal_count"] >= metrics["completed_samples"]
        assert "benchmark_return_pct" in metrics


def test_state_change_explains_conflicting_transitions():
    previous = {"signals": {"ma_alignment": "均线交织", "macd_cross": "无新交叉"}}
    current = {"signals": {"ma_alignment": "多头排列", "macd_cross": "金叉"}}
    changes = compare_states(previous, current)
    assert any("均线排列" in item for item in changes)
    assert any("MACD交叉" in item for item in changes)


def test_holding_snapshot_api_persists_daily_state(client, session):
    account = Account(
        name="技术快照",
        total_assets=Decimal("100000"),
        cash=Decimal("50000"),
        available_cash=Decimal("50000"),
    )
    session.add(account)
    session.flush()
    holding = Holding(
        account_id=account.id,
        symbol="000001",
        name="平安银行",
        quantity=1000,
        cost_price=Decimal("10"),
        current_price=Decimal("12"),
        price_source="manual",
    )
    session.add(holding)
    registry = ProviderRegistry()
    registry.register(TechnicalHistoryProvider())
    router = DataHubRouter(session, registry)
    history = router.get_history(
        "000001", date.today() - timedelta(days=365), date.today()
    )
    replace_market_series(
        session,
        router,
        history,
        history.require_value(),
        subject=stock_daily_subject("000001", "qfq", "CNY", "share"),
        min_rows=250,
    )
    session.commit()
    analysis_response = client.get("/api/v1/technical/000001")
    assert analysis_response.status_code == 200
    assert analysis_response.json()["adjustment"] == "前复权(qfq)"
    backtest_response = client.post(
        "/api/v1/technical/000001/backtest?holding_days=10&fees=0.001&slippage=0.001"
    )
    assert backtest_response.status_code == 200, backtest_response.text
    assert backtest_response.json()["engine"] == "vectorbt"
    response = client.post("/api/v1/technical/holdings/snapshots")
    assert response.status_code == 200, response.text
    assert response.json()["processed"] == 1
    changes = client.get(f"/api/v1/technical/holdings/changes?holding_id={holding.id}").json()
    assert len(changes) == 1
    assert changes[0]["changes"] == ["首次生成技术状态快照"]
