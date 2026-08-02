from __future__ import annotations

import pandas as pd

from app.services.technical import prepare_indicators


def calculate_technical_facts(frame: pd.DataFrame) -> dict:
    """Return indicator facts without emitting a trade decision."""
    prepared = prepare_indicators(frame)
    last = prepared.iloc[-1]
    close = float(last["Close"])
    ma5 = float(last["SMA_5"])
    ma20 = float(last["SMA_20"])
    ma60 = float(last["SMA_60"])
    trend_state = "向上" if close > ma5 > ma20 else "向下" if close < ma5 < ma20 else "震荡"
    return {
        "ma": {
            "ma5": round(ma5, 4),
            "ma20": round(ma20, 4),
            "ma60": round(ma60, 4),
            "ma250": round(float(prepared["Close"].rolling(250).mean().iloc[-1]), 4)
            if len(prepared) >= 250
            else None,
        },
        "macd": round(float(last["MACD_12_26_9"]), 4),
        "rsi14": round(float(last["RSI_14"]), 2),
        "atr14": round(float(last["ATRr_14"]), 4),
        "volume_ratio": round(
            float(last["Volume"] / last["VOL_MA20"]) if last["VOL_MA20"] else 0, 2
        ),
        "trend_state": trend_state,
    }
