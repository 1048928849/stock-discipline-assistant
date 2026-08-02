from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pandas as pd

from app.domain.event_anchor import (
    AnchorRole,
    EventAnchorCluster,
    EventAnchorResult,
    EventPriceAnchor,
    EventSession,
    EventType,
    InstitutionalEvidence,
)


def _series(frame: pd.DataFrame, name: str) -> pd.Series | None:
    for candidate in (name, name.lower(), name.upper(), name.capitalize()):
        if candidate in frame.columns:
            return pd.to_numeric(frame[candidate], errors="coerce")
    return None


def detect_event_sessions(frame: pd.DataFrame, *, lookback: int = 60) -> tuple[EventSession, ...]:
    close, volume = _series(frame, "Close"), _series(frame, "Volume")
    if close is None or volume is None or len(frame) < 20:
        return ()
    returns = close.pct_change() * 100
    volume_ratio = volume / volume.rolling(20).mean()
    candidates: list[EventSession] = []
    for position in range(max(19, len(frame) - lookback), len(frame)):
        change = float(returns.iloc[position])
        ratio = float(volume_ratio.iloc[position])
        if pd.isna(change) or pd.isna(ratio):
            continue
        reasons: list[str] = []
        score = 0.0
        if abs(change) >= 7:
            reasons.append("abnormal_return")
            score += min(0.55, abs(change) / 20 * 0.55)
        if ratio >= 1.5:
            reasons.append("abnormal_volume")
            score += min(0.35, ratio / 4 * 0.35)
        if not reasons or score < 0.35:
            continue
        event_type = (
            EventType.EXTREME_SELL_OFF
            if change <= -7
            else EventType.EXTREME_RALLY
            if change >= 7
            else EventType.ABNORMAL_TURNOVER
        )
        raw_date = frame.index[position]
        event_date = pd.Timestamp(raw_date).date()
        candidates.append(EventSession(event_date, event_type, round(score, 4), tuple(reasons)))
    return tuple(sorted(candidates, key=lambda item: (item.score, item.event_date), reverse=True))


def _session_vwap(frame: pd.DataFrame) -> float | None:
    volume = _series(frame, "Volume")
    if volume is None or float(volume.sum()) <= 0:
        return None
    amount = _series(frame, "Amount")
    if amount is not None and amount.notna().any():
        return float(amount.sum() / volume.sum())
    high, low, close = _series(frame, "High"), _series(frame, "Low"), _series(frame, "Close")
    if high is None or low is None or close is None:
        return None
    typical = (high + low + close) / 3
    return float((typical * volume).sum() / volume.sum())


def _phase_frames(frame: pd.DataFrame) -> tuple[tuple[str, pd.DataFrame], ...]:
    if not isinstance(frame.index, pd.DatetimeIndex):
        return (("FULL_SESSION_VWAP", frame),)
    return (
        ("OPEN_TO_1000_VWAP", frame.between_time("09:30", "10:00")),
        ("OPEN_TO_1030_VWAP", frame.between_time("09:30", "10:30")),
        ("MORNING_VWAP", frame.between_time("09:30", "11:30")),
        ("AFTERNOON_VWAP", frame.between_time("13:00", "15:00")),
        ("FULL_SESSION_VWAP", frame),
    )


def _role(center: float, width: float, current: float, recent_close: Sequence[float]) -> AnchorRole:
    if current < center - width:
        return AnchorRole.RESISTANCE_FROM_BELOW
    if current > center + width:
        if len(recent_close) >= 3 and all(value > center + width for value in recent_close[-3:]):
            return AnchorRole.SUPPORT_AFTER_BREAKOUT
        return AnchorRole.SUPPORT_FROM_ABOVE
    if len(recent_close) >= 3 and all(value < center - width for value in recent_close[-3:]):
        return AnchorRole.RESISTANCE_AFTER_BREAKDOWN
    return AnchorRole.CONTESTED


def calculate_event_anchors(
    daily_data: pd.DataFrame,
    *,
    minute_sessions: Mapping[date, pd.DataFrame] | None = None,
    institutional_evidence: Sequence[InstitutionalEvidence] = (),
    atr: float,
) -> EventAnchorResult:
    events = detect_event_sessions(daily_data)
    minute_sessions = minute_sessions or {}
    anchors: list[EventPriceAnchor] = []
    missing: list[str] = []
    for event in events[:8]:
        minute = minute_sessions.get(event.event_date)
        if minute is None or minute.empty:
            missing.append(f"{event.event_date.isoformat()}分钟行情")
            continue
        for anchor_type, phase in _phase_frames(minute):
            value = _session_vwap(phase)
            if value is not None:
                anchors.append(
                    EventPriceAnchor(
                        f"{event.event_date}:{anchor_type}",
                        event.event_date,
                        anchor_type,
                        round(value, 4),
                        0.8 if anchor_type == "FULL_SESSION_VWAP" else 0.9,
                        (f"minute:{event.event_date}",),
                    )
                )
    for evidence in institutional_evidence:
        if evidence.price is None:
            continue
        reliability = 0.95 if evidence.evidence_type == "BLOCK_TRADE" else 0.85
        anchors.append(
            EventPriceAnchor(
                evidence.evidence_id,
                evidence.event_date,
                evidence.evidence_type,
                round(evidence.price, 4),
                reliability,
                (evidence.source_id,),
            )
        )
    close = _series(daily_data, "Close")
    if close is None or close.dropna().empty:
        return EventAnchorResult(events, tuple(anchors), (), tuple(sorted(set(missing))))
    current = float(close.dropna().iloc[-1])
    recent = [float(value) for value in close.dropna().tail(3)]
    tolerance = max(float(atr) * 0.6, current * 0.006)
    groups: list[list[EventPriceAnchor]] = []
    for anchor in sorted(anchors, key=lambda item: item.price):
        center = (
            sum(item.price * item.reliability for item in groups[-1])
            / sum(item.reliability for item in groups[-1])
            if groups
            else None
        )
        if groups and center is not None and abs(anchor.price - center) <= tolerance:
            groups[-1].append(anchor)
        else:
            groups.append([anchor])
    clusters: list[EventAnchorCluster] = []
    for group in groups:
        weight = sum(item.reliability for item in group)
        center = sum(item.price * item.reliability for item in group) / weight
        width = tolerance * 0.5
        unique_dates = len({item.event_date for item in group})
        confidence = min(1.0, weight / 3 + max(0, unique_dates - 1) * 0.15)
        clusters.append(
            EventAnchorCluster(
                round(center, 4),
                round(min(item.price for item in group) - width, 4),
                round(max(item.price for item in group) + width, 4),
                round(confidence, 4),
                _role(center, width, current, recent),
                tuple(item.anchor_id for item in group),
            )
        )
    return EventAnchorResult(
        events,
        tuple(anchors),
        tuple(sorted(clusters, key=lambda item: item.confidence, reverse=True)),
        tuple(sorted(set(missing))),
    )
