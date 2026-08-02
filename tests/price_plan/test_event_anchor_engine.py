from datetime import date, datetime, timedelta

import pandas as pd

from app.domain.event_anchor import AnchorRole, InstitutionalEvidence
from app.services.event_anchor_engine import calculate_event_anchors, detect_event_sessions


def _daily() -> pd.DataFrame:
    dates = [date(2026, 6, 19) + timedelta(days=index) for index in range(42)]
    close = [1000.0] * 39 + [908.0, 920.0, 950.0]
    volume = [100.0] * 39 + [300.0, 150.0, 130.0]
    return pd.DataFrame(
        {
            "Open": close,
            "High": [value + 10 for value in close],
            "Low": [value - 10 for value in close],
            "Close": close,
            "Volume": volume,
        },
        index=pd.to_datetime(dates),
    )


def _minute_session() -> pd.DataFrame:
    index = pd.to_datetime(
        [
            datetime(2026, 7, 28, 9, 30),
            datetime(2026, 7, 28, 10, 0),
            datetime(2026, 7, 28, 10, 30),
            datetime(2026, 7, 28, 11, 30),
            datetime(2026, 7, 28, 14, 0),
        ]
    )
    volume = [5_000_000, 5_000_000, 8_000_000, 9_953_400, 10_000_000]
    price = [982.0, 981.0, 981.5, 980.7, 920.0]
    return pd.DataFrame(
        {
            "Close": price,
            "High": price,
            "Low": price,
            "Volume": volume,
            "Amount": [value * qty for value, qty in zip(price, volume, strict=True)],
        },
        index=index,
    )


def test_extreme_sell_off_is_selected_instead_of_nearest_normal_day():
    events = detect_event_sessions(_daily())

    assert events[0].event_date == date(2026, 7, 28)
    assert events[0].event_type.value == "EXTREME_SELL_OFF"
    assert "abnormal_return" in events[0].reasons


def test_zjxc_multi_event_cost_cluster_is_derived_from_sources():
    evidence = InstitutionalEvidence(
        "block:20260717:979.46",
        date(2026, 7, 17),
        "BLOCK_TRADE",
        979.46,
        "block_trade_source",
        institutional=True,
    )

    result = calculate_event_anchors(
        _daily(),
        minute_sessions={date(2026, 7, 28): _minute_session()},
        institutional_evidence=(evidence,),
        atr=20,
    )

    morning = next(item for item in result.anchors if item.anchor_type == "MORNING_VWAP")
    assert 980 < morning.price < 982
    cluster = next(item for item in result.clusters if evidence.evidence_id in item.anchor_ids)
    assert 979 <= cluster.center <= 982
    assert (
        len(
            {
                next(a.event_date for a in result.anchors if a.anchor_id == anchor_id)
                for anchor_id in cluster.anchor_ids
            }
        )
        >= 2
    )
    assert cluster.role is AnchorRole.RESISTANCE_FROM_BELOW


def test_missing_minute_data_is_reported_and_not_invented():
    result = calculate_event_anchors(_daily(), atr=20)

    assert result.events
    assert result.anchors == ()
    assert any("分钟行情" in item for item in result.missing_data)


def test_role_turns_to_support_after_three_closes_above_cluster():
    daily = _daily()
    daily.iloc[-3:, daily.columns.get_loc("Close")] = [1010, 1012, 1015]
    evidence = InstitutionalEvidence("block", date(2026, 7, 17), "BLOCK_TRADE", 980, "source", True)

    result = calculate_event_anchors(daily, institutional_evidence=(evidence,), atr=10)

    assert result.clusters[0].role is AnchorRole.SUPPORT_AFTER_BREAKOUT
