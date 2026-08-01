from datetime import date, timedelta

import pandas as pd

from app.domain.features import FeatureQuality
from app.features.platform import calculate_platform_facts
from app.features.technical import calculate_technical_facts
from app.features.timeframes import calculate_timeframe_facts
from app.services.features import FeaturePipeline
from app.services.trade_plan_generator import GENERATOR_PARAMETERS


def _frame(state: str = "ready") -> pd.DataFrame:
    rows = []
    for index in range(260):
        close = 5 + index * 0.019
        rows.append((close - 0.03, close + 0.08, close - 0.08, close, 100.0))
    for index in range(25):
        close = 10 + (index % 3 - 1) * 0.03
        rows.append((close - 0.03, close + 0.12, close - 0.12, close, 100.0))
    if state == "ready":
        rows.extend(
            [
                (10.52, 10.65, 10.15, 10.55, 220.0),
                (10.29, 10.48, 10.12, 10.32, 55.0),
                (10.79, 10.9, 10.3, 10.82, 180.0),
            ]
        )
    elif state == "flat":
        rows.extend([(9.99, 10.15, 9.9, 10.02, 100.0)] * 3)
    else:
        raise AssertionError(state)
    start = date(2025, 1, 1)
    return pd.DataFrame(
        [
            {
                "Date": start + timedelta(days=index),
                "Open": values[0],
                "High": values[1],
                "Low": values[2],
                "Close": values[3],
                "Volume": values[4],
            }
            for index, values in enumerate(rows)
        ]
    ).set_index(pd.to_datetime([start + timedelta(days=index) for index in range(len(rows))]))[
        ["Open", "High", "Low", "Close", "Volume"]
    ]


def test_normal_platform_facts_are_extracted_without_trade_status():
    facts = calculate_platform_facts(_frame(), GENERATOR_PARAMETERS)

    assert facts["valid_platform"] is True
    assert facts["platform_upper"] == 10.15
    assert facts["platform_lower"] == 9.85
    assert facts["turned_stronger"] is True
    assert not {"READY", "WAIT", "NO_TRADE"}.intersection(facts)


def test_wide_range_cannot_form_valid_platform():
    frame = _frame("flat")
    frame.iloc[-20:, frame.columns.get_loc("High")] = 13.0
    frame.iloc[-20:, frame.columns.get_loc("Low")] = 8.0

    facts = calculate_platform_facts(frame, GENERATOR_PARAMETERS)

    assert facts["valid_platform"] is False
    assert facts["platform_range_pct"] > 25


def test_missing_market_data_returns_missing_quality_snapshot():
    snapshot = FeaturePipeline().build(
        symbol="300502",
        as_of="2026-08-01",
        market_data=None,
        parameters=GENERATOR_PARAMETERS,
        missing_reason="没有前复权行情",
    )

    assert all(feature.quality is FeatureQuality.MISSING for feature in snapshot.features)
    assert snapshot.value("platform_structure") == {"missing_reason": "没有前复权行情"}
    assert snapshot.to_dict()["features"][0]["quality"] == "MISSING"


def test_breakout_volume_ratio_is_a_market_fact():
    facts = calculate_platform_facts(_frame(), GENERATOR_PARAMETERS)

    assert facts["breakout"]["volume_confirmed"] is True
    assert facts["breakout"]["volume_ratio"] == 2.2
    assert facts["latest_volume_ratio"] > 1


def test_moving_averages_match_input_series():
    frame = _frame()
    facts = calculate_technical_facts(frame)

    assert facts["ma"]["ma5"] == round(float(frame["Close"].rolling(5).mean().iloc[-1]), 4)
    assert facts["ma"]["ma20"] == round(float(frame["Close"].rolling(20).mean().iloc[-1]), 4)
    assert facts["ma"]["ma60"] == round(float(frame["Close"].rolling(60).mean().iloc[-1]), 4)


def test_atr_is_exposed_as_a_fact_and_matches_platform_calculation():
    frame = _frame()
    technical = calculate_technical_facts(frame)
    platform = calculate_platform_facts(frame, GENERATOR_PARAMETERS)

    assert technical["atr14"] > 0
    assert technical["atr14"] == platform["atr14"]


def test_multi_timeframe_states_are_descriptive_facts():
    facts = calculate_timeframe_facts(_frame())

    assert facts["daily_state"]["state"] in {"向上", "震荡", "向下"}
    assert facts["weekly_state"]["state"] == "向上"
    assert facts["monthly_state"]["state"] == "向上"
    assert facts["large_state"] == "向上"
