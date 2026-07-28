from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.trading_calendar import SHANGHAI_TZ
from app.providers.freestockdb import FreeStockDBHttpClient, FreeStockDBProvider


NOW = datetime(2026, 7, 24, 18, 0, tzinfo=SHANGHAI_TZ)


def _settings(**updates) -> Settings:
    values = {
        "freestockdb_enabled": True,
        "freestockdb_base_url": "http://127.0.0.1:7899",
        "freestockdb_timeout_seconds": 1,
        "freestockdb_max_retries": 1,
        "freestockdb_max_response_bytes": 20_000,
        "freestockdb_history_lookback_sessions": 2,
    }
    values.update(updates)
    return Settings(**values)


def _rows():
    return [
        {
            "date": 20260724,
            "code": "600519",
            "name": "fixture",
            "open": "10.5",
            "high": "10.9",
            "low": "10.3",
            "close": "10.8",
            "pre_close": "10.4",
            "volume": "1200",
            "amount": "12800",
            "turnover": "1.4",
        },
        {
            "date": 20260723,
            "code": "600519",
            "name": "fixture",
            "open": "20.0",
            "high": "21.2",
            "low": "19.8",
            "close": "21.0",
            "pre_close": "19.6",
            "volume": "1000",
            "amount": "10200",
            "turnover": "1.2",
        },
    ]


def _factors():
    return [
        ["复权:600519:20260723", {"cum": "2"}],
        ["复权:600519:20260724", {"cum": "4"}],
    ]


def _provider(handler, **setting_updates):
    settings = _settings(**setting_updates)
    client = FreeStockDBHttpClient(
        settings,
        transport=httpx.MockTransport(handler),
    )
    return FreeStockDBProvider(settings, client=client, now_fn=lambda: NOW)


def _native_handler(request: httpx.Request):
    assert request.url.path == "/"
    assert request.url.params["k1"] == "key:600519"
    if request.url.params["t"] == "日k":
        assert request.url.params["cmd"] == "vals"
        assert request.url.params["k2"] == "fwd:20260723,20260724"
        payload = _rows()
    else:
        assert request.url.params["cmd"] == "get"
        assert request.url.params["t"] == "复权"
        assert request.url.params["k2"] == "all:"
        payload = _factors()
    return httpx.Response(200, json=payload, request=request)


def test_native_qfq_and_turnover_use_fixed_root_protocol():
    provider = _provider(_native_handler)

    daily = provider.get_history("600519", date(2026, 7, 23), date(2026, 7, 24))
    turnover = provider.get_turnover_daily(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )

    assert [row.trade_date for row in daily] == [date(2026, 7, 23), date(2026, 7, 24)]
    assert daily[0].close == Decimal("10.50")
    assert daily[0].volume == Decimal("1000")
    assert daily[-1].close == Decimal("10.8")
    assert daily[-1].adjustment == "qfq"
    assert turnover[-1].amount == Decimal("12800")
    assert turnover[-1].turnover_rate == Decimal("1.4")
    assert daily.provider_lineage["source_url"] == "http://127.0.0.1:7899/"


def test_qfq_lineage_binds_both_native_requests_and_responses():
    daily = _provider(_native_handler).get_history(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )

    lineage = daily.provider_lineage
    assert lineage["protocol_version"] == "native-root-v0.2.1"
    assert lineage["adapter_version"] == "1.0.0"
    assert lineage["row_count"] == 2
    assert lineage["requested_start"] == "2026-07-23"
    assert lineage["requested_end"] == "2026-07-24"
    assert lineage["requested_adjustment"] == "qfq"
    assert lineage["actual_fields"] == sorted(_rows()[0])
    for field in (
        "raw_daily_request_digest",
        "raw_daily_response_digest",
        "factor_request_digest",
        "factor_response_digest",
        "raw_response_digest",
    ):
        assert len(lineage[field]) == 64


@pytest.mark.parametrize("missing", ["amount", "turnover"])
def test_missing_native_amount_or_turnover_is_never_inferred(missing):
    rows = _rows()
    rows[0].pop(missing)

    def handler(request: httpx.Request):
        payload = rows if request.url.params["t"] == "日k" else _factors()
        return httpx.Response(200, json=payload, request=request)

    provider = _provider(handler)
    with pytest.raises(ProviderUnavailableError, match=missing):
        provider.get_history("600519", date(2026, 7, 23), date(2026, 7, 24))
    with pytest.raises(ProviderUnavailableError, match=missing):
        provider.get_turnover_daily(
            "600519", date(2026, 7, 23), date(2026, 7, 24)
        )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda rows: rows[0].update(close="bad"), "numeric"),
        (lambda rows: rows[0].update(date=rows[1]["date"]), "duplicate"),
        (lambda rows: rows[0].update(high="10.0"), "OHLC"),
        (lambda rows: rows[0].update(code="000001"), "scope"),
        (lambda rows: rows[0].update(date=20260725), "trading session"),
    ],
)
def test_native_daily_rejects_invalid_business_payload(mutate, match):
    rows = _rows()
    mutate(rows)

    def handler(request: httpx.Request):
        payload = rows if request.url.params["t"] == "日k" else _factors()
        return httpx.Response(200, json=payload, request=request)

    with pytest.raises(ProviderUnavailableError, match=match):
        _provider(handler).get_history(
            "600519", date(2026, 7, 23), date(2026, 7, 24)
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"rows": _rows()},
        [["复权:600519:bad", {"cum": "1"}]],
        [["复权:000001:20260724", {"cum": "1"}]],
        [["复权:600519:20260724", {"cum": "0"}]],
    ],
)
def test_native_protocol_and_factor_shapes_are_strict(payload):
    def handler(request: httpx.Request):
        response = _rows() if request.url.params["t"] == "日k" else payload
        if isinstance(payload, dict):
            response = payload
        return httpx.Response(200, json=response, request=request)

    with pytest.raises(ProviderUnavailableError, match="protocol|factor"):
        _provider(handler).get_history(
            "600519", date(2026, 7, 23), date(2026, 7, 24)
        )


def test_native_client_rejects_html_non_json_and_oversize_responses():
    responses = iter(
        [
            httpx.Response(200, text="<html>bad</html>"),
            httpx.Response(200, content=b"not-json"),
            httpx.Response(200, content=b"[" + b"0," * 100 + b"0]"),
        ]
    )

    def handler(request: httpx.Request):
        response = next(responses)
        response.request = request
        return response

    client = FreeStockDBHttpClient(
        _settings(freestockdb_max_response_bytes=32),
        transport=httpx.MockTransport(handler),
    )
    for expected in ("JSON", "JSON", "response size"):
        with pytest.raises(ProviderUnavailableError, match=expected):
            client.daily_history("600519", date(2026, 7, 23), date(2026, 7, 24))


def test_native_client_timeout_is_bounded_and_reported():
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("fixture timeout", request=request)

    client = FreeStockDBHttpClient(
        _settings(freestockdb_max_retries=2),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderUnavailableError, match="timeout"):
        client.daily_history("600519", date(2026, 7, 23), date(2026, 7, 24))
    assert calls == 3


def test_native_client_rejects_remote_base_and_redirects():
    with pytest.raises(ValueError, match="loopback"):
        FreeStockDBHttpClient(_settings(freestockdb_base_url="https://example.com"))

    def handler(request: httpx.Request):
        return httpx.Response(
            302,
            headers={"location": "https://example.com/history"},
            request=request,
        )

    client = FreeStockDBHttpClient(
        _settings(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ProviderUnavailableError, match="redirect"):
        client.daily_history("600519", date(2026, 7, 23), date(2026, 7, 24))


def test_native_canary_uses_one_fixed_bounded_request():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json=[_rows()[0]], request=request)

    health = _provider(handler).health_check(probe=True)
    assert health["status"] == "READY"
    assert len(requests) == 1
    assert requests[0].url.path == "/"
    assert dict(requests[0].url.params) == {
        "cmd": "vals",
        "t": "日k",
        "k1": "key:600519",
        "k2": "fwd:20260724,20260724",
    }


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(200, json=[]), "DATA_UNAVAILABLE"),
        (httpx.Response(200, json={"rows": []}), "PROTOCOL_UNSUPPORTED"),
        (httpx.Response(400, json={"error": "invalid request"}), "PROTOCOL_UNSUPPORTED"),
    ],
)
def test_native_canary_distinguishes_protocol_and_data(response, expected):
    def handler(request: httpx.Request):
        response.request = request
        return response

    assert _provider(handler).health_check(probe=True)["status"] == expected


def test_native_canary_distinguishes_unreachable_and_degraded():
    available = True

    def handler(request: httpx.Request):
        if available:
            payload = _rows() if request.url.params["t"] == "日k" else _factors()
            return httpx.Response(200, json=payload, request=request)
        raise httpx.ConnectError("offline", request=request)

    provider = _provider(handler)
    assert _provider(lambda request: (_ for _ in ()).throw(
        httpx.ConnectError("offline", request=request)
    )).health_check(probe=True)["status"] == "UNREACHABLE"
    provider.get_history("600519", date(2026, 7, 23), date(2026, 7, 24))
    available = False
    assert provider.health_check(probe=True)["status"] == "DEGRADED"


def test_csi300_is_not_advertised_or_guessed():
    provider = _provider(_native_handler)
    assert "market.index_daily" not in provider.metadata.supported_capabilities
    with pytest.raises(
        ProviderUnavailableError, match="CSI300_HTTP_CAPABILITY_UNAVAILABLE"
    ):
        provider.get_index_history(
            "CSI000300", date(2026, 7, 23), date(2026, 7, 24)
        )


def test_native_response_digests_are_stable_for_exact_bytes():
    daily_bytes = json.dumps(_rows(), separators=(",", ":")).encode()
    factor_bytes = json.dumps(_factors(), ensure_ascii=False, separators=(",", ":")).encode()

    def handler(request: httpx.Request):
        content = daily_bytes if request.url.params["t"] == "日k" else factor_bytes
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json"},
            request=request,
        )

    first = _provider(handler).get_history(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    second = _provider(handler).get_history(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    assert first.provider_lineage == second.provider_lineage
