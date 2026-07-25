from __future__ import annotations

from decimal import Decimal

from app.analysis.contracts import TechnicalAnalysisInput, TechnicalContext


def _average(values) -> Decimal | None:
    rows = list(values)
    return sum(rows, Decimal("0")) / len(rows) if rows else None


def _trend(bars) -> tuple[str, Decimal | None, Decimal | None, Decimal | None]:
    if len(bars) < 20:
        return "INSUFFICIENT", None, None, None
    closes = [item.close for item in bars]
    ma5 = _average(closes[-5:])
    ma10 = _average(closes[-10:])
    ma20 = _average(closes[-20:])
    if ma5 > ma10 > ma20:
        value = "UP"
    elif ma5 < ma10 < ma20:
        value = "DOWN"
    else:
        value = "RANGE"
    return value, ma5, ma10, ma20


def analyze_intraday_turnover(value: TechnicalAnalysisInput) -> TechnicalContext:
    daily_trend, _, _, _ = _trend(value.daily_bars)
    intraday_trend, ma5, ma10, ma20 = _trend(value.intraday_bars)
    bars = value.intraday_bars
    enough = len(bars) >= 21
    prior = bars[-21:-1] if enough else ()
    latest = bars[-1] if bars else None
    effective_high = max((item.high for item in prior), default=None)
    effective_low = min((item.low for item in prior), default=None)
    average_volume = _average(item.volume for item in prior)
    breakout = bool(latest and effective_high is not None and latest.close > effective_high)
    pullback = bool(
        latest
        and intraday_trend == "UP"
        and ma10
        and ma20
        and abs(latest.close - ma10) / ma10 <= Decimal("0.03")
        and latest.close >= ma20
        and not breakout
    )
    volume_breakout = bool(
        breakout
        and average_volume
        and latest.volume >= average_volume * Decimal("1.5")
    )
    low_volume_pullback = bool(
        pullback
        and average_volume
        and latest.volume <= average_volume * Decimal("0.8")
    )
    false_breakout = bool(
        latest
        and effective_high
        and average_volume
        and latest.close < effective_high * Decimal("0.98")
        and latest.volume >= average_volume * Decimal("1.5")
    )
    spike_fade = bool(
        latest
        and len(bars) >= 2
        and latest.high >= bars[-2].close * Decimal("1.03")
        and latest.close <= latest.high * Decimal("0.98")
    )
    rates = [item.turnover_rate for item in value.turnover]
    current_turnover = rates[-1] if rates else None
    average_5 = _average(rates[-6:-1] if len(rates) >= 6 else rates[:-1])
    average_20 = _average(rates[-21:-1] if len(rates) >= 21 else rates[:-1])
    relative = (
        current_turnover / average_20
        if current_turnover is not None and average_20 and average_20 > 0
        else None
    )
    close_position = None
    daily_return = None
    if latest and effective_high is not None and effective_low is not None:
        span = effective_high - effective_low
        close_position = (latest.close - effective_low) / span if span > 0 else Decimal("0.5")
    if latest and len(bars) >= 2 and bars[-2].close:
        daily_return = latest.close / bars[-2].close - Decimal("1")
    completeness_parts = [bool(value.daily_bars), bool(value.intraday_bars), bool(rates)]
    completeness = Decimal(sum(completeness_parts)) / Decimal("3")
    return TechnicalContext(
        intraday_trend=intraday_trend,
        daily_trend=daily_trend,
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        effective_high=effective_high,
        effective_low=effective_low,
        breakout=breakout,
        pullback=pullback,
        false_breakout=false_breakout,
        volume_breakout=volume_breakout,
        low_volume_pullback=low_volume_pullback,
        spike_fade=spike_fade,
        daily_intraday_aligned=(
            daily_trend == intraday_trend and daily_trend in {"UP", "DOWN"}
        ),
        current_turnover=current_turnover,
        average_turnover_5d=average_5,
        average_turnover_20d=average_20,
        relative_turnover_20d=relative,
        high_position_abnormal_turnover=bool(
            close_position is not None
            and close_position >= Decimal("0.85")
            and relative is not None
            and relative >= Decimal("2")
        ),
        low_position_moderate_volume=bool(
            close_position is not None
            and close_position <= Decimal("0.3")
            and relative is not None
            and Decimal("1") <= relative <= Decimal("1.5")
        ),
        volume_without_price_gain=bool(
            relative is not None
            and relative >= Decimal("1.5")
            and daily_return is not None
            and daily_return <= Decimal("0.005")
        ),
        low_volume_rise=bool(
            relative is not None
            and relative <= Decimal("0.8")
            and daily_return is not None
            and daily_return > 0
        ),
        data_completeness=completeness,
    )


__all__ = ["analyze_intraday_turnover"]
