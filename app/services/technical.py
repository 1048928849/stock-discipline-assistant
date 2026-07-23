from __future__ import annotations

from math import isfinite

import pandas as pd


RISK_NOTICE = (
    "技术指标基于历史价格和成交量，存在滞后、假突破与参数失效风险；"
    "支撑压力是区间而非保证，不构成买卖指令。"
)


def prepare_indicators(source: pd.DataFrame) -> pd.DataFrame:
    """用 pandas-ta-classic 计算指标；所有信号只使用当日及此前数据。"""
    import pandas_ta_classic as ta  # noqa: F401 - 注册 DataFrame.ta accessor

    frame = source.copy().sort_index()
    required = {"Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(frame.columns):
        raise ValueError(f"K线缺少字段：{', '.join(sorted(required - set(frame.columns)))}")
    if len(frame) < 80:
        raise ValueError("技术分析至少需要 80 个交易日的复权 K 线")
    for length in (5, 10, 20, 60, 120):
        frame.ta.sma(length=length, append=True)
    frame.ta.macd(fast=12, slow=26, signal=9, append=True)
    frame.ta.rsi(length=14, append=True)
    frame.ta.bbands(length=20, std=2, append=True)
    frame.ta.atr(length=14, append=True)
    frame.ta.adx(length=14, append=True)
    frame.ta.obv(append=True)

    frame["VOL_MA20"] = frame["Volume"].rolling(20).mean()
    frame["PRIOR_HIGH20"] = frame["High"].shift(1).rolling(20).max()
    frame["PRICE_NEW_LOW20"] = frame["Low"] <= frame["Low"].shift(1).rolling(20).min()
    frame["PRICE_NEW_HIGH20"] = frame["High"] >= frame["High"].shift(1).rolling(20).max()
    macd = frame["MACD_12_26_9"]
    frame["BULLISH_DIVERGENCE"] = frame["PRICE_NEW_LOW20"] & (
        macd > macd.shift(1).rolling(20).min()
    )
    frame["BEARISH_DIVERGENCE"] = frame["PRICE_NEW_HIGH20"] & (
        macd < macd.shift(1).rolling(20).max()
    )
    frame["VOLUME_BREAKOUT"] = (frame["Close"] > frame["PRIOR_HIGH20"]) & (
        frame["Volume"] > frame["VOL_MA20"] * 1.5
    )
    frame["LOW_VOLUME_PULLBACK"] = (
        (frame["Close"] > frame["SMA_20"])
        & (frame["SMA_20"] > frame["SMA_60"])
        & (frame["Close"] < frame["Close"].shift(1))
        & ((frame["Close"] / frame["SMA_20"] - 1).abs() <= 0.03)
        & (frame["Volume"] < frame["VOL_MA20"] * 0.7)
    )
    return frame


def _number(value, digits: int = 4):
    if value is None or pd.isna(value):
        return None
    numeric = float(value)
    return round(numeric, digits) if isfinite(numeric) else None


def _touch_groups(hit: pd.Series) -> list[int]:
    """返回每一组连续触碰的起点；连续多根 K 线只算一次测试。"""
    flags = hit.fillna(False).astype(bool).reset_index(drop=True)
    return [
        index for index, value in enumerate(flags) if value and (index == 0 or not flags[index - 1])
    ]


def _decayed_touch_weight(group_starts: list[int], total_bars: int) -> float:
    return sum(0.5 ** ((total_bars - 1 - index) / 90) for index in group_starts)


def _level_zones(
    frame: pd.DataFrame, current: float
) -> tuple[list[dict], list[dict], list[dict], dict]:
    recent = frame.tail(min(len(frame), 250))
    atr = float(_number(frame["ATRr_14"].iloc[-1]) or current * 0.02)
    # 单个区间总宽不超过 1 ATR 和现价的 2%，避免高波动时区间失去参考价值。
    half_width = min(max(atr * 0.35, current * 0.002), atr * 0.5, current * 0.01)
    merge_tolerance = half_width
    candidates: list[tuple[float, str]] = []
    for window, label in ((10, "近期摆动"), (20, "月度区间"), (60, "季度区间")):
        candidates.extend(
            [
                (float(recent["Low"].tail(window).min()), f"{label}低点"),
                (float(recent["High"].tail(window).max()), f"{label}高点"),
            ]
        )
    for column, label in (
        ("SMA_20", "MA20"),
        ("SMA_60", "MA60"),
        ("BBL_20_2.0", "布林下轨"),
        ("BBU_20_2.0", "布林上轨"),
    ):
        if column in frame and pd.notna(frame[column].iloc[-1]):
            candidates.append((float(frame[column].iloc[-1]), label))

    zones: list[dict] = []
    for level, basis in sorted(candidates):
        existing = next(
            (item for item in zones if abs(item["price"] - level) <= merge_tolerance), None
        )
        if existing:
            count = len(existing["basis"])
            existing["price"] = round((existing["price"] * count + level) / (count + 1), 4)
            existing["basis"].append(basis)
            continue
        zones.append(
            {
                "price": round(level, 4),
                "basis": [basis],
            }
        )

    for zone in zones:
        level = zone["price"]
        lower, upper = level - half_width, level + half_width
        hit = ((recent["Low"] <= upper) & (recent["High"] >= lower)).fillna(False)
        group_starts = _touch_groups(hit)
        # 约 90 个交易日半衰期，一年前的触碰权重约为近期的 15%。
        weighted_touches = _decayed_touch_weight(group_starts, len(recent))
        last_touch = recent.index[group_starts[-1]] if group_starts else None
        zone.update(
            {
                "lower": round(lower, 4),
                "upper": round(upper, 4),
                "width_atr": round((upper - lower) / atr, 3),
                "touches": len(group_starts),
                "weighted_touches": round(weighted_touches, 2),
                "last_touch_date": pd.Timestamp(last_touch).date().isoformat()
                if last_touch is not None
                else None,
                "strength": "高"
                if weighted_touches >= 3 or (weighted_touches >= 2 and len(zone["basis"]) >= 2)
                else "中"
                if weighted_touches >= 1.25
                else "低",
            }
        )

    supports = sorted(
        ({**item, "zone_type": "支撑区"} for item in zones if item["upper"] < current),
        key=lambda x: x["price"],
        reverse=True,
    )[:3]
    resistances = sorted(
        ({**item, "zone_type": "压力区"} for item in zones if item["lower"] > current),
        key=lambda x: x["price"],
    )[:3]
    contest = sorted(
        (
            {**item, "zone_type": "震荡/争夺区"}
            for item in zones
            if item["lower"] <= current <= item["upper"]
        ),
        key=lambda x: abs(x["price"] - current),
    )[:3]

    confirm_buffer = max(atr * 0.2, current * 0.003)
    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None
    conditions = {
        "breakout_confirmation": (
            {
                "price": round(nearest_resistance["upper"] + confirm_buffer, 4),
                "rule": (
                    f"日线收盘高于 {nearest_resistance['upper'] + confirm_buffer:.4f}，且连续 2 日站稳；"
                    "或单日放量至 20 日均量的 1.5 倍以上。"
                ),
                "signal_invalidation": (
                    f"确认突破后，日线收盘重新跌回压力区下沿 "
                    f"{nearest_resistance['lower']:.4f} 以下，突破信号失效。"
                ),
            }
            if nearest_resistance
            else {
                "price": None,
                "rule": "暂无位于现价上方的可靠压力区。",
                "signal_invalidation": None,
            }
        ),
        "breakdown_confirmation": (
            {
                "price": round(nearest_support["lower"] - confirm_buffer, 4),
                "rule": (
                    f"日线收盘低于 {nearest_support['lower'] - confirm_buffer:.4f}，且连续 2 日未收复；"
                    "或单日放量至 20 日均量的 1.5 倍以上。"
                ),
                "signal_invalidation": (
                    f"确认跌破后，日线收盘重新站上支撑区上沿 "
                    f"{nearest_support['upper']:.4f}，跌破信号失效。"
                ),
            }
            if nearest_support
            else {
                "price": None,
                "rule": "暂无位于现价下方的可靠支撑区。",
                "signal_invalidation": None,
            }
        ),
        "atr_buffer": round(confirm_buffer, 4),
    }
    return supports, resistances, contest, conditions


def analyze_frame(
    source: pd.DataFrame,
    symbol: str,
    data_source: str,
    current_price: float | None = None,
) -> dict:
    frame = prepare_indicators(source)
    last = frame.iloc[-1]
    previous = frame.iloc[-2]
    close = float(last["Close"])
    reference_price = float(current_price) if current_price is not None else close
    ma_values = [last[f"SMA_{length}"] for length in (5, 10, 20, 60)]
    if close > ma_values[0] > ma_values[1] > ma_values[2] > ma_values[3]:
        ma_alignment = "多头排列"
    elif close < ma_values[0] < ma_values[1] < ma_values[2] < ma_values[3]:
        ma_alignment = "空头排列"
    else:
        ma_alignment = "均线交织"

    macd = float(last["MACD_12_26_9"])
    macd_signal = float(last["MACDs_12_26_9"])
    rsi = float(last["RSI_14"])
    volume_ratio = float(last["Volume"] / last["VOL_MA20"]) if last["VOL_MA20"] else 0
    signals = {
        "ma_alignment": ma_alignment,
        "macd_state": "零轴上方" if macd > 0 else "零轴下方",
        "macd_cross": "金叉"
        if macd > macd_signal and previous["MACD_12_26_9"] <= previous["MACDs_12_26_9"]
        else "死叉"
        if macd < macd_signal and previous["MACD_12_26_9"] >= previous["MACDs_12_26_9"]
        else "无新交叉",
        "bullish_divergence": bool(last["BULLISH_DIVERGENCE"]),
        "bearish_divergence": bool(last["BEARISH_DIVERGENCE"]),
        "volume_breakout": bool(last["VOLUME_BREAKOUT"]),
        "low_volume_pullback": bool(last["LOW_VOLUME_PULLBACK"]),
        "rsi_state": "超买" if rsi >= 70 else "超卖" if rsi <= 30 else "中性",
    }
    conflicts: list[str] = []
    if ma_alignment == "多头排列" and macd < macd_signal:
        conflicts.append("均线保持多头，但 MACD 动能弱于信号线")
    if ma_alignment == "空头排列" and macd > macd_signal:
        conflicts.append("均线保持空头，但 MACD 出现短线修复")
    if ma_alignment == "多头排列" and rsi >= 70:
        conflicts.append("趋势偏强，但 RSI 已进入偏热区")
    if signals["volume_breakout"] and signals["bearish_divergence"]:
        conflicts.append("放量突破与 MACD 顶背离同时出现")

    score = 0
    score += 2 if ma_alignment == "多头排列" else -2 if ma_alignment == "空头排列" else 0
    score += 1 if macd > macd_signal else -1
    score += 2 if signals["volume_breakout"] else 0
    score += 1 if signals["low_volume_pullback"] else 0
    score += 2 if signals["bullish_divergence"] else 0
    score -= 2 if signals["bearish_divergence"] else 0
    score -= 1 if rsi >= 75 else 0
    tone = "偏强" if score >= 3 else "偏弱" if score <= -2 else "中性/信号混合"
    conclusion = f"技术状态为{tone}（评分 {score}）。{ma_alignment}，MACD {signals['macd_state']}，RSI {rsi:.1f}；"
    conclusion += (
        "指标存在矛盾，应等待价格与成交量确认。" if conflicts else "主要指标暂未出现明显冲突。"
    )
    supports, resistances, contest_zones, level_conditions = _level_zones(frame, reference_price)

    chart_columns = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "SMA_20",
        "SMA_60",
        "MACD_12_26_9",
        "MACDs_12_26_9",
        "RSI_14",
    ]
    chart = []
    for index, row in frame.tail(180).iterrows():
        chart.append(
            {
                "date": pd.Timestamp(index).date().isoformat(),
                **{column: _number(row[column]) for column in chart_columns},
            }
        )
    return {
        "symbol": symbol,
        "trade_date": pd.Timestamp(frame.index[-1]).date().isoformat(),
        "analysis_date": pd.Timestamp(frame.index[-1]).date().isoformat(),
        "current_price": round(reference_price, 4),
        "analysis_close": round(close, 4),
        "kline_period": {
            "frequency": "日线",
            "bars": len(frame),
            "start": pd.Timestamp(frame.index[0]).date().isoformat(),
            "end": pd.Timestamp(frame.index[-1]).date().isoformat(),
        },
        "data_source": data_source,
        "adjustment": "前复权(qfq)",
        "indicators": {
            "close": round(close, 4),
            "ma5": _number(last["SMA_5"]),
            "ma10": _number(last["SMA_10"]),
            "ma20": _number(last["SMA_20"]),
            "ma60": _number(last["SMA_60"]),
            "ma120": _number(last["SMA_120"]),
            "macd": round(macd, 4),
            "macd_signal": round(macd_signal, 4),
            "macd_histogram": _number(last["MACDh_12_26_9"]),
            "rsi14": round(rsi, 2),
            "atr14": _number(last["ATRr_14"]),
            "adx14": _number(last["ADX_14"]),
            "volume_ratio": round(volume_ratio, 2),
            "obv": _number(last["OBV"]),
        },
        "signals": signals,
        "support_levels": supports,
        "resistance_levels": resistances,
        "contest_zones": contest_zones,
        "level_conditions": level_conditions,
        "conflicts": conflicts,
        "score": score,
        "conclusion": conclusion,
        "risk_notice": RISK_NOTICE,
        "chart": chart,
    }


def compare_states(previous: dict | None, current: dict) -> list[str]:
    if not previous:
        return ["首次生成技术状态快照"]
    changes: list[str] = []
    previous_signals = previous.get("signals", {})
    for key, label in (
        ("ma_alignment", "均线排列"),
        ("macd_cross", "MACD交叉"),
        ("rsi_state", "RSI状态"),
        ("volume_breakout", "放量突破"),
        ("low_volume_pullback", "缩量回调"),
        ("bullish_divergence", "底背离"),
        ("bearish_divergence", "顶背离"),
    ):
        before, after = previous_signals.get(key), current["signals"].get(key)
        if before != after:
            changes.append(f"{label}：{before} → {after}")
    if not changes:
        changes.append("主要技术状态较上一快照无变化")
    return changes


def backtest_signals(
    source: pd.DataFrame, holding_days: int = 10, fees: float = 0.0003, slippage: float = 0.001
) -> dict:
    import vectorbt as vbt

    frame = prepare_indicators(source)
    close = frame["Close"]
    ma_bull = (
        (close > frame["SMA_5"])
        & (frame["SMA_5"] > frame["SMA_10"])
        & (frame["SMA_10"] > frame["SMA_20"])
        & (frame["SMA_20"] > frame["SMA_60"])
    )
    entries = {
        "均线多头形成": ma_bull & ~ma_bull.shift(1, fill_value=False),
        "MACD金叉": (frame["MACD_12_26_9"] > frame["MACDs_12_26_9"])
        & (frame["MACD_12_26_9"].shift(1) <= frame["MACDs_12_26_9"].shift(1)),
        "放量突破": frame["VOLUME_BREAKOUT"].fillna(False),
        "缩量回调": frame["LOW_VOLUME_PULLBACK"].fillna(False),
        "MACD底背离": frame["BULLISH_DIVERGENCE"].fillna(False),
    }
    benchmark = float(close.iloc[-1] / close.iloc[0] - 1)
    result = {}
    for name, signal in entries.items():
        exits = signal.shift(holding_days, fill_value=False)
        portfolio = vbt.Portfolio.from_signals(
            close,
            entries=signal,
            exits=exits,
            init_cash=100000,
            fees=fees,
            slippage=slippage,
            freq="1D",
        )
        forward = (close.shift(-holding_days) / close - 1)[signal].dropna()
        trade_count = int(portfolio.trades.count())
        win_rate = float(portfolio.trades.win_rate()) if trade_count else 0
        result[name] = {
            "signal_count": int(signal.sum()),
            "completed_samples": int(len(forward)),
            "forward_win_rate_pct": round(float((forward > 0).mean() * 100), 2)
            if len(forward)
            else 0,
            "average_forward_return_pct": round(float(forward.mean() * 100), 4)
            if len(forward)
            else 0,
            "median_forward_return_pct": round(float(forward.median() * 100), 4)
            if len(forward)
            else 0,
            "portfolio_return_pct": round(float(portfolio.total_return() * 100), 4),
            "portfolio_max_drawdown_pct": round(abs(float(portfolio.max_drawdown() * 100)), 4),
            "portfolio_trade_count": trade_count,
            "portfolio_win_rate_pct": round(win_rate * 100, 2),
            "benchmark_return_pct": round(benchmark * 100, 4),
            "holding_days": holding_days,
        }
    return {
        "engine": "vectorbt",
        "data_start": pd.Timestamp(frame.index[0]).date().isoformat(),
        "data_end": pd.Timestamp(frame.index[-1]).date().isoformat(),
        "fees": fees,
        "slippage": slippage,
        "signals": result,
        "warning": "历史表现不代表未来；信号样本较少时统计结论不可靠。回测未完整模拟 T+1、涨跌停和停牌。",
    }
