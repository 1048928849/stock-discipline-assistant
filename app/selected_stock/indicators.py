from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from math import sqrt
from typing import Any, Iterable


ZERO = Decimal("0")
ONE = Decimal("1")
Q6 = Decimal("0.000001")


def _d(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("indicator input must be finite")
    return result


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Q6, rounding=ROUND_HALF_UP) if value is not None else None


def _mean(values: Iterable[Decimal]) -> Decimal | None:
    rows = list(values)
    return sum(rows, ZERO) / Decimal(len(rows)) if rows else None


def _sma(values: list[Decimal], window: int) -> Decimal | None:
    return _mean(values[-window:]) if len(values) >= window else None


def _slope(values: list[Decimal], window: int) -> Decimal | None:
    if len(values) < window or values[-window] == 0:
        return None
    return values[-1] / values[-window] - ONE


def _ema_series(values: list[Decimal], window: int) -> list[Decimal]:
    if not values:
        return []
    alpha = Decimal(2) / Decimal(window + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (ONE - alpha) * output[-1])
    return output


def _compound_return(values: list[Decimal], window: int) -> Decimal | None:
    if len(values) <= window or values[-window - 1] <= 0:
        return None
    return values[-1] / values[-window - 1] - ONE


def _max_drawdown(values: list[Decimal], window: int) -> Decimal | None:
    rows = values[-window:]
    if len(rows) < 2:
        return None
    peak = rows[0]
    worst = ZERO
    for value in rows:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - ONE)
    return worst


def _std(values: list[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values))
    return Decimal(str(sqrt(float(variance))))


def _rsi(closes: list[Decimal], window: int = 14) -> Decimal | None:
    if len(closes) <= window:
        return None
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    gains = [max(value, ZERO) for value in changes[-window:]]
    losses = [max(-value, ZERO) for value in changes[-window:]]
    average_gain = _mean(gains)
    average_loss = _mean(losses)
    if average_loss == 0:
        return Decimal("100")
    rs = average_gain / average_loss
    return Decimal("100") - Decimal("100") / (ONE + rs)


def _true_ranges(rows: list[dict[str, Any]]) -> list[Decimal]:
    values: list[Decimal] = []
    previous_close: Decimal | None = None
    for row in rows:
        high = _d(row["high"])
        low = _d(row["low"])
        candidates = [high - low]
        if previous_close is not None:
            candidates.extend((abs(high - previous_close), abs(low - previous_close)))
        values.append(max(candidates))
        previous_close = _d(row["close"])
    return values


def _obv(rows: list[dict[str, Any]]) -> Decimal:
    value = ZERO
    for previous, current in zip(rows, rows[1:]):
        volume = _d(current["volume"])
        if _d(current["close"]) > _d(previous["close"]):
            value += volume
        elif _d(current["close"]) < _d(previous["close"]):
            value -= volume
    return value


def _aligned_relative_return(
    stock_rows: list[dict[str, Any]],
    reference_rows: list[dict[str, Any]],
    window: int,
) -> Decimal | None:
    stock = {row["trade_date"]: _d(row["close"]) for row in stock_rows}
    reference = {row["trade_date"]: _d(row["close"]) for row in reference_rows}
    common = sorted(set(stock) & set(reference))
    if len(common) <= window:
        return None
    start, end = common[-window - 1], common[-1]
    stock_return = stock[end] / stock[start] - ONE
    reference_return = reference[end] / reference[start] - ONE
    return stock_return - reference_return


def _daily_elasticity(
    stock_rows: list[dict[str, Any]], reference_rows: list[dict[str, Any]], *, up: bool
) -> Decimal | None:
    stock = {row["trade_date"]: _d(row["close"]) for row in stock_rows}
    reference = {row["trade_date"]: _d(row["close"]) for row in reference_rows}
    common = sorted(set(stock) & set(reference))[-61:]
    ratios = []
    for previous, current in zip(common, common[1:]):
        stock_return = stock[current] / stock[previous] - ONE
        ref_return = reference[current] / reference[previous] - ONE
        if ref_return == 0 or (ref_return > 0) != up:
            continue
        ratios.append(stock_return / abs(ref_return))
    return _mean(ratios)


def _validate_rows(rows: list[dict[str, Any]]) -> None:
    dates = [row["trade_date"] for row in rows]
    if any(not isinstance(day, date) for day in dates):
        raise ValueError("trade_date is invalid")
    if dates != sorted(set(dates)):
        raise ValueError("trade dates must be unique and increasing")
    for row in rows:
        open_price, high, low, close = (
            _d(row[name]) for name in ("open", "high", "low", "close")
        )
        if min(open_price, high, low, close) <= 0:
            raise ValueError("OHLC must be positive")
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise ValueError("OHLC relationship is invalid")
        if _d(row["volume"]) < 0:
            raise ValueError("volume must be nonnegative")


def calculate_indicators(
    stock_rows: list[dict[str, Any]],
    *,
    benchmark_rows: list[dict[str, Any]],
    industry_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    _validate_rows(stock_rows)
    _validate_rows(benchmark_rows)
    if industry_rows:
        _validate_rows(industry_rows)
    closes = [_d(row["close"]) for row in stock_rows]
    highs = [_d(row["high"]) for row in stock_rows]
    lows = [_d(row["low"]) for row in stock_rows]
    volumes = [_d(row["volume"]) for row in stock_rows]
    latest = closes[-1]
    moving = {window: _sma(closes, window) for window in (5, 10, 20, 60, 120)}
    slopes = {window: _slope(closes, window) for window in (5, 10, 20, 60)}
    ema12, ema26 = _ema_series(closes, 12), _ema_series(closes, 26)
    macd_line = [fast - slow for fast, slow in zip(ema12, ema26)]
    signal = _ema_series(macd_line, 9)
    histogram = [line - sig for line, sig in zip(macd_line, signal)]
    ranges = _true_ranges(stock_rows)
    atr14 = _mean(ranges[-14:]) if len(ranges) >= 14 else None
    std20 = _std(closes[-20:]) if len(closes) >= 20 else None
    middle = moving[20]
    upper = middle + Decimal(2) * std20 if middle is not None and std20 else None
    lower = middle - Decimal(2) * std20 if middle is not None and std20 else None
    width = (upper - lower) / middle if upper and lower and middle else None
    volume5, volume20 = _sma(volumes, 5), _sma(volumes, 20)
    swing_low = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    swing_high = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    low60 = min(lows[-60:]) if len(lows) >= 60 else min(lows)
    high60 = max(highs[-60:]) if len(highs) >= 60 else max(highs)
    atr_buffer = (atr14 or ZERO) * Decimal("0.5")
    support_anchor = max(
        [value for value in (moving[20], moving[60], swing_low) if value is not None]
    )
    support_low = max(ZERO, support_anchor - atr_buffer)
    support_high = support_anchor + atr_buffer * Decimal("0.25")
    resistance_anchor = min(
        [value for value in (swing_high, high60) if value is not None and value >= latest]
        or [max(swing_high, high60)]
    )
    returns = {window: _compound_return(closes, window) for window in (5, 10, 20, 60)}
    negative_gaps = sorted(
        max(
            ZERO,
            ONE - _d(stock_rows[index]["open"]) / _d(stock_rows[index - 1]["close"]),
        )
        for index in range(max(1, len(stock_rows) - 120), len(stock_rows))
        if _d(stock_rows[index - 1]["close"]) > 0
    )
    gap_95 = (
        negative_gaps[min(len(negative_gaps) - 1, int(len(negative_gaps) * 0.95))]
        if negative_gaps
        else None
    )
    result = {
        "latest_close": latest,
        "sma": {str(key): _q(value) for key, value in moving.items()},
        "slope": {str(key): _q(value) for key, value in slopes.items()},
        "bullish_alignment": bool(
            all(moving[key] is not None for key in (5, 10, 20, 60))
            and moving[5] > moving[10] > moving[20] > moving[60]
        ),
        "bearish_alignment": bool(
            all(moving[key] is not None for key in (5, 10, 20, 60))
            and moving[5] < moving[10] < moving[20] < moving[60]
        ),
        "ma_entanglement": bool(
            moving[5]
            and moving[20]
            and (max(moving[5], moving[10], moving[20]) - min(moving[5], moving[10], moving[20]))
            / moving[20]
            <= Decimal("0.02")
        ),
        "rsi14": _q(_rsi(closes)),
        "macd": _q(macd_line[-1]),
        "macd_signal": _q(signal[-1]),
        "macd_histogram": _q(histogram[-1]),
        "macd_histogram_change": _q(
            histogram[-1] - histogram[-2] if len(histogram) >= 2 else ZERO
        ),
        "returns": {str(key): _q(value) for key, value in returns.items()},
        "momentum_accelerating": bool(
            returns[5] is not None
            and returns[20] is not None
            and returns[5] > returns[20] / Decimal(4)
        ),
        "volume_ratio_5": _q(volumes[-1] / volume5 if volume5 else None),
        "volume_ratio_20": _q(volumes[-1] / volume20 if volume20 else None),
        "up_on_volume": bool(
            len(closes) >= 2 and closes[-1] > closes[-2] and volume20 and volumes[-1] > volume20
        ),
        "down_on_volume": bool(
            len(closes) >= 2 and closes[-1] < closes[-2] and volume20 and volumes[-1] > volume20
        ),
        "pullback_on_low_volume": bool(
            moving[20]
            and latest >= moving[20]
            and len(closes) >= 2
            and closes[-1] <= closes[-2]
            and volume20
            and volumes[-1] <= volume20 * Decimal("0.8")
        ),
        "breakout_on_volume": bool(
            len(highs) >= 21
            and latest > max(highs[-21:-1])
            and volume20
            and volumes[-1] >= volume20 * Decimal("1.5")
        ),
        "obv": _q(_obv(stock_rows)),
        "atr14": _q(atr14),
        "atr_pct": _q(atr14 / latest if atr14 and latest else None),
        "std20": _q(std20),
        "bollinger": {
            "middle": _q(middle),
            "upper": _q(upper),
            "lower": _q(lower),
            "width": _q(width),
        },
        "drawdown": {
            str(window): _q(_max_drawdown(closes, window))
            for window in (20, 60, 120)
        },
        "gap_risk": bool(
            len(stock_rows) >= 2
            and abs(_d(stock_rows[-1]["open"]) / _d(stock_rows[-2]["close"]) - ONE)
            >= Decimal("0.05")
        ),
        "overnight_gap": {
            "negative_gap_95": _q(gap_95),
            "recent_max_negative_gap": _q(max(negative_gaps))
            if negative_gaps
            else None,
            "sample_count": len(negative_gaps),
        },
        "large_bearish_streak": bool(
            len(stock_rows) >= 2
            and all(
                _d(row["close"]) < _d(row["open"])
                and (_d(row["open"]) - _d(row["close"])) / _d(row["open"])
                >= Decimal("0.04")
                for row in stock_rows[-2:]
            )
        ),
        "support_zone": [_q(support_low), _q(support_high)],
        "resistance_zone": [
            _q(max(ZERO, resistance_anchor - atr_buffer * Decimal("0.25"))),
            _q(resistance_anchor + atr_buffer * Decimal("0.25")),
        ],
        "high_low": {
            "low20": _q(swing_low),
            "high20": _q(swing_high),
            "low60": _q(low60),
            "high60": _q(high60),
        },
        "relative_strength": {
            "stock_vs_csi300_20": _q(
                _aligned_relative_return(stock_rows, benchmark_rows, 20)
            ),
            "stock_vs_csi300_60": _q(
                _aligned_relative_return(stock_rows, benchmark_rows, 60)
            ),
            "stock_vs_industry_20": _q(
                _aligned_relative_return(stock_rows, industry_rows, 20)
                if industry_rows
                else None
            ),
            "stock_vs_industry_60": _q(
                _aligned_relative_return(stock_rows, industry_rows, 60)
                if industry_rows
                else None
            ),
            "industry_vs_csi300_20": _q(
                _aligned_relative_return(industry_rows, benchmark_rows, 20)
                if industry_rows
                else None
            ),
            "industry_vs_csi300_60": _q(
                _aligned_relative_return(industry_rows, benchmark_rows, 60)
                if industry_rows
                else None
            ),
            "up_day_elasticity": _q(
                _daily_elasticity(stock_rows, benchmark_rows, up=True)
            ),
            "down_day_resilience": _q(
                _daily_elasticity(stock_rows, benchmark_rows, up=False)
            ),
        },
    }
    return result


__all__ = ["calculate_indicators"]
