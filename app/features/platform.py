from __future__ import annotations

import pandas as pd

from app.services.technical import prepare_indicators


def calculate_platform_facts(frame: pd.DataFrame, parameters: dict) -> dict:
    """Calculate platform, breakout, pullback and strengthening facts only."""
    prepared = prepare_indicators(frame)
    window = int(parameters["platform_min_days"])
    pct = float(parameters["breakout_pct"]) / 100
    volume_multiple = float(parameters["breakout_volume_multiple"])
    recent_start = max(window, len(prepared) - 45)
    candidates = []
    for index in range(recent_start, len(prepared)):
        prior = prepared.iloc[index - window : index]
        upper = float(prior["High"].max())
        lower = float(prior["Low"].min())
        volume_base = float(prior["Volume"].mean())
        row = prepared.iloc[index]
        if float(row["Close"]) >= upper * (1 + pct):
            candidates.append(
                {
                    "index": index,
                    "upper": upper,
                    "lower": lower,
                    "price": float(row["Close"]),
                    "date": pd.Timestamp(prepared.index[index]).date().isoformat(),
                    "volume_ratio": float(row["Volume"]) / volume_base if volume_base else 0,
                    "volume_confirmed": bool(
                        volume_base and float(row["Volume"]) >= volume_base * volume_multiple
                    ),
                }
            )
    confirmed_candidates = [item for item in candidates if item["volume_confirmed"]]
    if candidates:
        # 保留本轮结构中最早的有效突破，避免把“再次转强”误识别成新的首次突破。
        breakout = (confirmed_candidates or candidates)[0]
        upper, lower = breakout["upper"], breakout["lower"]
    else:
        prior = prepared.tail(window)
        upper, lower = float(prior["High"].max()), float(prior["Low"].min())
        breakout = None
    platform_range_pct = (upper / lower - 1) * 100 if lower else 999
    valid_platform = platform_range_pct <= 25
    post = prepared.iloc[breakout["index"] + 1 :] if breakout else prepared.iloc[0:0]
    pullback_sample = post.iloc[:-1] if len(post) > 1 else post
    tolerance = float(parameters["pullback_tolerance_pct"]) / 100
    pullback_rows = pullback_sample[
        (pullback_sample["Low"] <= upper * (1 + tolerance))
        & (pullback_sample["Close"] <= upper * (1 + tolerance))
    ]
    pullback_seen = not pullback_rows.empty
    platform_broken = bool(
        not post.empty and ((post["Close"] < lower) | (post["Low"] < lower * (1 - tolerance))).any()
    )
    pullback_volume_ratio = None
    pullback_shrinking = False
    pullback_low = None
    if pullback_seen:
        pullback_volume_ratio = float(
            pullback_rows["Volume"].mean() / prepared["VOL_MA20"].iloc[-1]
        )
        pullback_shrinking = pullback_volume_ratio <= float(parameters["pullback_volume_ratio"])
        pullback_low = float(pullback_rows["Low"].min())
    last, previous = prepared.iloc[-1], prepared.iloc[-2]
    turn_trigger = max(upper, float(post["High"].iloc[:-1].max()) if len(post) > 1 else upper)
    turned_stronger = bool(
        breakout
        and pullback_seen
        and float(last["Close"]) > turn_trigger
        and float(last["Close"]) > float(previous["High"])
        and float(last["Volume"]) >= float(last["VOL_MA20"])
    )
    return {
        "platform_days": window,
        "platform_start": pd.Timestamp(prepared.index[-window]).date().isoformat(),
        "platform_end": pd.Timestamp(prepared.index[-1]).date().isoformat(),
        "platform_upper": round(upper, 4),
        "platform_lower": round(lower, 4),
        "platform_range_pct": round(platform_range_pct, 2),
        "valid_platform": valid_platform,
        "breakout": breakout,
        "pullback_seen": pullback_seen,
        "pullback_range": [round(pullback_low, 4), round(upper * (1 + tolerance), 4)]
        if pullback_low
        else None,
        "pullback_volume_ratio": round(pullback_volume_ratio, 2)
        if pullback_volume_ratio is not None
        else None,
        "pullback_shrinking": pullback_shrinking,
        "platform_broken": platform_broken,
        "turn_trigger_price": round(turn_trigger, 4),
        "turned_stronger": turned_stronger,
        "latest_close": round(float(last["Close"]), 4),
        "latest_volume_ratio": round(
            float(last["Volume"] / last["VOL_MA20"]) if last["VOL_MA20"] else 0, 2
        ),
        "atr14": round(float(last["ATRr_14"]), 4),
        "ma": {
            "ma5": round(float(last["SMA_5"]), 4),
            "ma20": round(float(last["SMA_20"]), 4),
            "ma60": round(float(last["SMA_60"]), 4),
            "ma250": round(float(prepared["Close"].rolling(250).mean().iloc[-1]), 4)
            if len(prepared) >= 250
            else None,
        },
        "macd": round(float(last["MACD_12_26_9"]), 4),
        "rsi14": round(float(last["RSI_14"]), 2),
    }
