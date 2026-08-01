from __future__ import annotations

import pandas as pd


def calculate_direction(close: pd.Series, fast: int, slow: int) -> dict:
    if len(close) < slow:
        return {"state": "无法判断", "evidence": f"需要至少 {slow} 根，当前 {len(close)} 根"}
    fast_ma = float(close.rolling(fast).mean().iloc[-1])
    slow_ma = float(close.rolling(slow).mean().iloc[-1])
    current = float(close.iloc[-1])
    state = (
        "向上" if current > fast_ma > slow_ma else "向下" if current < fast_ma < slow_ma else "震荡"
    )
    return {
        "state": state,
        "close": round(current, 4),
        f"ma{fast}": round(fast_ma, 4),
        f"ma{slow}": round(slow_ma, 4),
        "evidence": f"收盘 {current:.2f}，MA{fast} {fast_ma:.2f}，MA{slow} {slow_ma:.2f}",
    }


def calculate_timeframe_facts(frame: pd.DataFrame) -> dict:
    close = frame["Close"]
    weekly = calculate_direction(close.resample("W-FRI").last().dropna(), 10, 30)
    monthly = calculate_direction(close.resample("ME").last().dropna(), 3, 6)
    daily = calculate_direction(close, 20, min(60, len(frame)))
    large_state = (
        "向下"
        if "向下" in {weekly["state"], monthly["state"]}
        else "向上"
        if weekly["state"] == monthly["state"] == "向上"
        else "震荡"
    )
    return {
        "daily_state": daily,
        "weekly_state": weekly,
        "monthly_state": monthly,
        "large_state": large_state,
    }
