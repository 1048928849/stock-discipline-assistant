from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date

import pandas as pd

from app.domain.price_plan import CapitalAnchor, MarketStructurePlan, StructuralZone
from app.domain.event_anchor import InstitutionalEvidence
from app.services.event_anchor_engine import calculate_event_anchors


def _column(frame: pd.DataFrame, name: str) -> pd.Series | None:
    for candidate in (name, name.lower(), name.upper(), name.capitalize()):
        if candidate in frame.columns:
            return pd.to_numeric(frame[candidate], errors="coerce")
    return None


def _vwap(price: pd.Series, volume: pd.Series, window: int) -> float | None:
    values = pd.DataFrame({"price": price, "volume": volume}).dropna().tail(window)
    total_volume = float(values["volume"].sum())
    if values.empty or total_volume <= 0:
        return None
    return float((values["price"] * values["volume"]).sum() / total_volume)


def _volume_profile_nodes(
    price: pd.Series, volume: pd.Series, *, bins: int = 24, nodes: int = 3
) -> list[tuple[float, float]]:
    values = pd.DataFrame({"price": price, "volume": volume}).dropna()
    if len(values) < 20 or float(values["volume"].sum()) <= 0:
        return []
    low, high = float(values["price"].min()), float(values["price"].max())
    if high <= low:
        return [(low, 1.0)]
    width = (high - low) / bins
    bucket = ((values["price"] - low) / width).clip(upper=bins - 1).astype(int)
    totals = values.groupby(bucket)["volume"].sum().sort_values(ascending=False).head(nodes)
    maximum = float(totals.max())
    return [
        (low + (int(index) + 0.5) * width, float(value) / maximum)
        for index, value in totals.items()
    ]


def _zones(
    anchors: list[CapitalAnchor], current_price: float, atr: float
) -> tuple[tuple[StructuralZone, ...], tuple[StructuralZone, ...]]:
    tolerance = max(atr * 0.6, current_price * 0.006)
    groups: list[list[CapitalAnchor]] = []
    for anchor in sorted(anchors, key=lambda item: item.price):
        if (
            groups
            and abs(anchor.price - sum(item.price for item in groups[-1]) / len(groups[-1]))
            <= tolerance
        ):
            groups[-1].append(anchor)
        else:
            groups.append([anchor])
    by_role: dict[str, list[StructuralZone]] = defaultdict(list)
    for group in groups:
        total_weight = sum(item.weight for item in group)
        center = sum(item.price * item.weight for item in group) / total_weight
        role = (
            "SUPPORT"
            if center < current_price - tolerance * 0.2
            else "RESISTANCE"
            if center > current_price + tolerance * 0.2
            else "CONTESTED"
        )
        zone = StructuralZone(
            role=role,
            low=round(min(item.price for item in group) - tolerance * 0.35, 4),
            high=round(max(item.price for item in group) + tolerance * 0.35, 4),
            center=round(center, 4),
            confidence=round(min(1.0, total_weight / 2.5), 4),
            anchor_ids=tuple(item.anchor_id for item in group),
        )
        if role != "CONTESTED":
            by_role[role].append(zone)
    supports = tuple(sorted(by_role["SUPPORT"], key=lambda item: item.center, reverse=True))
    resistance = tuple(sorted(by_role["RESISTANCE"], key=lambda item: item.center))
    return supports, resistance


def calculate_market_structure(
    market_data: pd.DataFrame | None,
    *,
    atr: float | None,
    platform_lower: float | None = None,
    platform_upper: float | None = None,
    extreme_stop_atr_multiple: float = 1.0,
    minute_sessions: Mapping[date, pd.DataFrame] | None = None,
    institutional_evidence: Sequence[InstitutionalEvidence] = (),
) -> MarketStructurePlan | None:
    """Build descriptive cost anchors and zones without issuing a trade decision."""
    if market_data is None or market_data.empty or atr is None or atr <= 0:
        return None
    close = _column(market_data, "Close")
    high = _column(market_data, "High")
    low = _column(market_data, "Low")
    volume = _column(market_data, "Volume")
    if close is None or high is None or low is None or volume is None:
        return None
    typical = (high + low + close) / 3
    valid_close = close.dropna()
    if valid_close.empty:
        return None
    current = float(valid_close.iloc[-1])
    anchors: list[CapitalAnchor] = []
    event_result = calculate_event_anchors(
        market_data,
        minute_sessions=minute_sessions,
        institutional_evidence=institutional_evidence,
        atr=float(atr),
    )
    for item in event_result.anchors:
        anchors.append(
            CapitalAnchor(
                item.anchor_id,
                item.anchor_type,
                item.price,
                item.reliability * 1.5,
                "event_anchor",
            )
        )
    for window, weight in ((20, 1.0), (60, 1.25), (120, 1.4)):
        value = _vwap(typical, volume, window)
        if value is not None:
            anchors.append(
                CapitalAnchor(
                    f"vwap_{window}",
                    f"{window}日成交成本",
                    round(value, 4),
                    weight,
                    "rolling_vwap",
                    window,
                )
            )
    for index, (value, strength) in enumerate(
        _volume_profile_nodes(typical.tail(120), volume.tail(120)), 1
    ):
        anchors.append(
            CapitalAnchor(
                f"volume_node_{index}",
                f"成交密集峰{index}",
                round(value, 4),
                0.8 + strength * 0.6,
                "volume_profile",
                120,
            )
        )
    if platform_lower is not None:
        anchors.append(
            CapitalAnchor(
                "platform_lower",
                "平台下沿",
                round(float(platform_lower), 4),
                1.35,
                "strategy_structure",
            )
        )
    if platform_upper is not None:
        anchors.append(
            CapitalAnchor(
                "platform_upper",
                "平台上沿",
                round(float(platform_upper), 4),
                1.15,
                "strategy_structure",
            )
        )
    supports, resistance = _zones(anchors, current, float(atr))
    extreme_stop = None
    basis = None
    if supports:
        core = max(supports, key=lambda item: (item.confidence, -item.center))
        extreme_stop = round(core.low - float(atr) * extreme_stop_atr_multiple, 4)
        basis = f"核心支撑区下沿 {core.low} - {extreme_stop_atr_multiple} ATR"
    warning_items = [] if len(anchors) >= 4 else ["有效成本锚较少，结构置信度有限"]
    if event_result.missing_data:
        warning_items.append("缺少事件日分钟或机构交易数据，事件成本锚已降级")
    return MarketStructurePlan(
        current,
        tuple(anchors),
        supports,
        resistance,
        extreme_stop,
        basis,
        tuple(warning_items),
        event_result,
    )
