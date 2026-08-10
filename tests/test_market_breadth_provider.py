from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings
from app.data_hub.contracts import ObservedRows
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.quality import canonical_digest, policy_for
from app.models import DataQualityRecord
from app.market_breadth.reconciliation import (
    CANONICAL_A_SHARE_SH_SZ_V1,
    MarketUniverseMembership,
    SecurityType,
    TradableStatus,
    classify_symbol,
)
from app.providers.market_breadth import (
    BreadthLimitEvidenceUnavailableError,
    BreadthProtocolError,
    BreadthTimeoutError,
    BreadthUniverseIncompleteError,
    FreeStockDBBreadthClient,
    FreeStockDBCrossSectionResponse,
    MarketBreadthEODProvider,
    MarketBreadthWorkerRunner,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 29, 9, 41, 15, 241225, tzinfo=SHANGHAI)
DAY = date(2026, 7, 28)


def _settings(**changes) -> Settings:
    return Settings(
        market_breadth_enabled=True,
        freestockdb_base_url="http://127.0.0.1:7899",
        **changes,
    )


def _row(
    code: str,
    close: str = "11",
    pre_close: str = "10",
    volume: str = "100",
    *,
    day: date = DAY,
) -> dict:
    return {
        "code": code,
        "date": day.strftime("%Y%m%d"),
        "close": close,
        "pre_close": pre_close,
        "volume": volume,
    }


def _cross_section(rows: list[dict]) -> FreeStockDBCrossSectionResponse:
    return FreeStockDBCrossSectionResponse(
        payload=rows,
        request_digest="a" * 64,
        response_digest="b" * 64,
        response_bytes=1234,
    )


def _worker_response(
    *,
    up: list[str] | None = None,
    down: list[str] | None = None,
    up_digest: str = "c" * 64,
    down_digest: str = "d" * 64,
) -> dict:
    up = up or []
    down = down or []
    return {
        "worker_protocol_version": "market-breadth-worker-v1",
        "akshare_package_version": "1.18.72",
        "operation": "limit_pools",
        "trade_date": DAY.isoformat(),
        "limit_up_symbols": up,
        "limit_down_symbols": down,
        "raw_limit_up_count": len(up),
        "raw_limit_down_count": len(down),
        "limit_up_response_digest": up_digest,
        "limit_down_response_digest": down_digest,
        "fetched_at": NOW.isoformat(),
    }


class Client:
    def __init__(self, rows):
        self.rows = rows

    def daily_cross_section(self, day):
        assert day == DAY
        return _cross_section(self.rows)


class Runner:
    def __init__(self, response):
        self.response = response

    def run(self, trade_date, *, timeout_seconds=None):
        assert trade_date == DAY
        assert timeout_seconds is not None
        return self.response


class MembershipProvider:
    def __init__(self, symbols):
        self.symbols = symbols

    def get_membership(self, trade_date):
        result = []
        for symbol in self.symbols:
            exchange, board = classify_symbol(symbol)
            result.append(
                MarketUniverseMembership.build(
                    spec=CANONICAL_A_SHARE_SH_SZ_V1,
                    trade_date=trade_date,
                    symbol=symbol,
                    exchange=exchange,
                    board=board,
                    security_type=SecurityType.COMMON_STOCK,
                    tradable_status=TradableStatus.ACTIVE,
                    listing_date=date(2000, 1, 1),
                    delisting_date=None,
                    source="fixture-membership",
                    source_reference=f"fixture:{symbol}",
                    observed_at=NOW,
                )
            )
        return result


class NoSupplement:
    def get_history(self, symbol, start, end):
        return []


def _provider(rows, response=None, *, membership_symbols=None):
    response = response or _worker_response()
    if membership_symbols is None:
        membership_symbols = []
        for row in rows:
            symbol = str(row.get("code", ""))
            if (
                symbol.startswith(
                    (
                        "600",
                        "601",
                        "603",
                        "605",
                        "688",
                        "689",
                        "000",
                        "001",
                        "002",
                        "003",
                        "300",
                        "301",
                    )
                )
                and _decimal_for_fixture(row.get("close"))
                and _decimal_for_fixture(row.get("pre_close"))
                and _decimal_for_fixture(row.get("volume"))
            ):
                membership_symbols.append(symbol)
    return MarketBreadthEODProvider(
        _settings(),
        client=Client(rows),
        runner=Runner(response),
        membership_provider=MembershipProvider(membership_symbols),
        supplementary_provider=NoSupplement(),
        now_fn=lambda: NOW,
        fetch_now_fn=lambda: NOW,
    )


def _decimal_for_fixture(value):
    try:
        return Decimal(str(value)).is_finite() and Decimal(str(value)) > 0
    except Exception:
        return False


def test_freestockdb_client_uses_only_fixed_cross_section_protocol():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = request.url
        return httpx.Response(200, json=[_row("600001")])

    client = FreeStockDBBreadthClient(_settings(), transport=httpx.MockTransport(handler))
    response = client.daily_cross_section(DAY)
    params = dict(seen["url"].params.multi_items())
    assert seen["url"].path == "/"
    assert params == {
        "cmd": "vals",
        "t": "\u65e5k",
        "k1": "all:",
        "k2": "key:20260728",
    }
    assert response.payload == [_row("600001")]


def test_provider_filters_non_stocks_and_computes_decimal_median():
    provider = _provider(
        [
            _row("600001", "11", "10"),
            _row("000001", "9", "10"),
            _row("920001", "10", "10"),
            _row("510300", "5", "4"),
            _row("600002", "", "10"),
            _row("600003", "10", ""),
            _row("600004", "10", "9", "0"),
        ],
        _worker_response(up=["600001"], down=["000001"]),
    )
    result = provider.get_market_breadth(DAY)
    assert isinstance(result, ObservedRows)
    assert len(result) == 1
    row = result[0]
    assert (row.advancing, row.declining, row.unchanged) == (1, 1, 0)
    assert row.median_change_pct == Decimal("0")
    assert (row.limit_up, row.limit_down) == (1, 1)
    assert row.observed_at == datetime(2026, 7, 28, 15, 0, tzinfo=SHANGHAI)
    lineage = result.provider_lineage
    assert lineage["raw_cross_section_rows"] == 7
    assert lineage["valid_universe_rows"] == 2
    assert lineage["excluded_rows_by_reason"] == {
        "invalid_close": 1,
        "invalid_pre_close": 1,
        "no_valid_volume": 1,
        "non_target_instrument": 1,
    }


def test_duplicate_cross_section_symbol_is_rejected():
    provider = _provider([_row("600001"), _row("600001")])
    with pytest.raises(BreadthProtocolError, match="duplicate cross-section symbol"):
        provider.get_market_breadth(DAY)


@pytest.mark.parametrize(
    ("close", "pre_close", "volume", "reason"),
    [
        ("NaN", "10", "100", "invalid_close"),
        ("10", "Infinity", "100", "invalid_pre_close"),
        ("10", "9", "0", "no_valid_volume"),
    ],
)
def test_invalid_business_rows_are_excluded(close, pre_close, volume, reason):
    provider = _provider([_row("600001", close, pre_close, volume), _row("000001")])
    result = provider.get_market_breadth(DAY)
    assert result.provider_lineage["excluded_rows_by_reason"][reason] == 1
    row = result[0]
    assert row.advancing + row.declining + row.unchanged == 1


def test_pool_order_and_duplicate_codes_do_not_change_normalized_result():
    rows = [_row("600001"), _row("000001", "9", "10")]
    first = _provider(rows, _worker_response(up=["600001"], down=["000001"])).get_market_breadth(
        DAY
    )
    second = _provider(
        list(reversed(rows)), _worker_response(up=["600001"], down=["000001"])
    ).get_market_breadth(DAY)
    assert (
        first.provider_lineage["normalized_result_digest"]
        == second.provider_lineage["normalized_result_digest"]
    )
    assert first[0] == second[0]


def test_non_target_pool_symbol_is_recorded_but_allowed():
    result = _provider(
        [_row("600001")],
        _worker_response(up=["600001", "510300"]),
    ).get_market_breadth(DAY)
    assert result[0].limit_up == 1
    assert result.provider_lineage["unmatched_limit_up_symbols"] == [
        {"classification": "OUTSIDE_UNIVERSE", "symbol": "510300"}
    ]


def test_target_stock_missing_from_cross_section_blocks_persistence_input():
    provider = _provider(
        [_row("600001")], _worker_response(up=["600002"]), membership_symbols=["600001", "600002"]
    )
    with pytest.raises(
        BreadthUniverseIncompleteError,
        match="BREADTH_UNIVERSE_INCOMPLETE.*600002",
    ):
        provider.get_market_breadth(DAY)


def test_unknown_pool_symbol_blocks_persistence_input():
    provider = _provider(
        [_row("600001")],
        _worker_response(up=["999999"]),
    )
    with pytest.raises(BreadthUniverseIncompleteError, match="BREADTH_UNIVERSE_INCOMPLETE.*999999"):
        provider.get_market_breadth(DAY)


def test_future_unfinished_and_out_of_window_requests_are_rejected():
    provider = _provider([_row("600001")])
    with pytest.raises(BreadthProtocolError, match="completed trading session"):
        provider.get_market_breadth(date(2026, 7, 29))
    with pytest.raises(BreadthLimitEvidenceUnavailableError, match="BREADTH_LIMIT"):
        provider.get_market_breadth(date(2026, 6, 1))


def test_worker_runner_uses_fixed_command_and_reaps_after_timeout(monkeypatch):
    process = SimpleNamespace(poll=lambda: -9, returncode=-9, kill=lambda: None)

    def communicate(*, input=None, timeout=None):
        if timeout is not None:
            raise subprocess.TimeoutExpired("worker", timeout)
        return b"", b""

    process.communicate = communicate
    seen = {}

    def popen(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    runner = MarketBreadthWorkerRunner(_settings())
    with pytest.raises(BreadthTimeoutError):
        runner.run(DAY)
    assert seen["command"][-2:] == ["-m", "app.providers.market_breadth_worker"]
    assert seen["kwargs"]["shell"] is False


@pytest.mark.parametrize(
    ("stdout", "returncode", "message"),
    [
        (b"not-json", 0, "invalid JSON"),
        (b"{}\n{}", 0, "invalid JSON"),
        (b"{}", 1, "invalid error response"),
    ],
)
def test_worker_runner_rejects_protocol_failures(monkeypatch, stdout, returncode, message):
    process = SimpleNamespace(
        poll=lambda: returncode,
        returncode=returncode,
        communicate=lambda **kwargs: (stdout, b"diagnostic"),
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(BreadthProtocolError, match=message):
        MarketBreadthWorkerRunner(_settings()).run(DAY)


def test_worker_runner_rejects_oversized_stdout(monkeypatch):
    process = SimpleNamespace(
        poll=lambda: 0,
        returncode=0,
        communicate=lambda **kwargs: (b"x" * 2001, b""),
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    runner = MarketBreadthWorkerRunner(_settings(market_breadth_max_response_bytes=2000))
    with pytest.raises(BreadthProtocolError, match="size limit"):
        runner.run(DAY)


def test_worker_response_digest_is_stable_for_business_rows():
    business_rows = [{"\u4ee3\u7801": "600001", "\u6da8\u8dcc\u5e45": 10}]
    expected = hashlib.sha256(
        json.dumps(
            business_rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert len(expected) == 64


def test_router_breadth_digest_ignores_fetch_completion_time():
    first = _provider([_row("600001")], _worker_response(up=["600001"])).get_market_breadth(DAY)
    later = datetime(2026, 7, 29, 9, 42, tzinfo=SHANGHAI)
    changed = MarketBreadthEODProvider(
        _settings(),
        client=Client([_row("600001")]),
        runner=Runner(
            {
                **_worker_response(up=["600001"]),
                "fetched_at": later.isoformat(),
            }
        ),
        membership_provider=MembershipProvider(["600001"]),
        supplementary_provider=NoSupplement(),
        now_fn=lambda: later,
        fetch_now_fn=lambda: later,
    ).get_market_breadth(DAY)
    policy = policy_for("market.breadth.daily")
    assert canonical_digest(first, policy) == canonical_digest(changed, policy)


def test_fetch_completion_time_can_follow_fixed_business_evaluation_time():
    worker_fetched_at = datetime(2026, 7, 29, 9, 42, tzinfo=SHANGHAI)
    request_times = iter(
        (
            datetime(2026, 7, 29, 9, 41, 30, tzinfo=SHANGHAI),
            datetime(2026, 7, 29, 9, 42, 30, tzinfo=SHANGHAI),
        )
    )
    provider = MarketBreadthEODProvider(
        _settings(),
        client=Client([_row("600001")]),
        runner=Runner(
            {
                **_worker_response(up=["600001"]),
                "fetched_at": worker_fetched_at.isoformat(),
            }
        ),
        membership_provider=MembershipProvider(["600001"]),
        supplementary_provider=NoSupplement(),
        now_fn=lambda: NOW,
        fetch_now_fn=lambda: next(request_times),
    )

    result = provider.get_market_breadth(DAY)

    assert result.fetched_at == datetime(2026, 7, 29, 9, 42, 30, tzinfo=SHANGHAI)


def test_router_audit_preserves_bounded_breadth_lineage(session):
    provider = _provider(
        [_row("600001"), _row("000001", "9", "10")],
        _worker_response(up=["600001"], down=["000001"]),
    )
    registry = ProviderRegistry()
    registry.register(provider)
    result = DataHubRouter(session, registry, now_fn=lambda: NOW).get_market_breadth(DAY)
    assert result.quality_status.value == "SINGLE_SOURCE"
    record = session.get(DataQualityRecord, result.quality_record_id)
    audit = record.provider_observations[0]
    assert audit["raw_cross_section_rows"] == 2
    assert audit["valid_universe_rows"] == 2
    assert audit["matched_limit_up_count"] == 1
    assert audit["matched_limit_down_count"] == 1
    assert "universe" not in audit
