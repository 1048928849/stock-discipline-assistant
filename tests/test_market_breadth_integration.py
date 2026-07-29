from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest

from app.config import Settings
from app.providers.market_breadth import MarketBreadthEODProvider
from app.providers.market_breadth import (
    BreadthUniverseIncompleteError,
    _median,
    _normalize_symbol,
)


pytestmark = pytest.mark.market_breadth_integration


@pytest.mark.parametrize(
    ("trade_date", "universe", "directions", "median", "pools"),
    [
        (
            date(2026, 7, 24),
            5489,
            (548, 4909, 32),
            "-2.857143",
            (40, 39, 1, 25, 25, 0),
        ),
        (
            date(2026, 7, 28),
            5485,
            (2587, 2746, 152),
            "-0.040866",
            (61, 61, 0, 49, 48, 1),
        ),
    ],
)
def test_real_recent_sources_are_repeatable_and_strict_gate_blocks_incomplete_universe(
    trade_date, universe, directions, median, pools
):
    settings = Settings(
        market_breadth_enabled=True,
        freestockdb_base_url=os.environ.get(
            "FREESTOCKDB_BASE_URL", "http://127.0.0.1:7899"
        ),
    )
    provider = MarketBreadthEODProvider(settings)
    first_cross = provider.client.daily_cross_section(trade_date)
    first_worker = provider.runner.run(trade_date)
    second_cross = provider.client.daily_cross_section(trade_date)
    second_worker = provider.runner.run(trade_date)
    provider._validate_worker_response(first_worker, trade_date)
    valid, target_presence, _ = provider._cross_section(first_cross, trade_date)
    calculated_directions = (
        sum(close > previous for close, previous in valid.values()),
        sum(close < previous for close, previous in valid.values()),
        sum(close == previous for close, previous in valid.values()),
    )
    changes = [
        (close / previous - 1) * 100 for close, previous in valid.values()
    ]
    assert len(valid) == universe
    assert calculated_directions == directions
    assert format(_median(changes).quantize(Decimal("0.000001")), "f") == median
    up = {_normalize_symbol(item) for item in first_worker["limit_up_symbols"]}
    down = {_normalize_symbol(item) for item in first_worker["limit_down_symbols"]}
    unmatched_up = sorted(item for item in up - set(valid) if item is not None)
    unmatched_down = sorted(item for item in down - set(valid) if item is not None)
    assert (
        first_worker["raw_limit_up_count"],
        len(up & set(valid)),
        len(unmatched_up),
        first_worker["raw_limit_down_count"],
        len(down & set(valid)),
        len(unmatched_down),
    ) == pools
    assert first_cross.response_digest == second_cross.response_digest
    assert first_worker["limit_up_response_digest"] == second_worker[
        "limit_up_response_digest"
    ]
    assert first_worker["limit_down_response_digest"] == second_worker[
        "limit_down_response_digest"
    ]
    with pytest.raises(BreadthUniverseIncompleteError):
        provider._pool(
            first_worker[
                "limit_up_symbols" if unmatched_up else "limit_down_symbols"
            ],
            valid,
            target_presence,
        )
