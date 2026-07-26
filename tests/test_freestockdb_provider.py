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
        "freestockdb_csi300_symbol": "fixture-csi300",
    }
    values.update(updates)
    return Settings(**values)


def _payload(*, symbol="600519", adjustment="qfq", rows=None):
    return {
        "schema_version": "1.0",
        "data": {
            "symbol": symbol,
            "adjustment": adjustment,
            "price_unit": "CNY",
            "volume_unit": "share",
            "amount_unit": "CNY",
            "turnover_rate_unit": "percent",
            "rows": rows
            or [
                {
                    "trade_date": "2026-07-23",
                    "open": "10.0",
                    "high": "10.6",
                    "low": "9.9",
                    "close": "10.5",
                    "volume": "1000",
                    "amount": "10200",
                    "turnover_rate": "1.2",
                },
                {
                    "trade_date": "2026-07-24",
                    "open": "10.5",
                    "high": "10.9",
                    "low": "10.3",
                    "close": "10.8",
                    "volume": "1200",
                    "amount": "12800",
                    "turnover_rate": "1.4",
                },
            ],
        },
    }


def _provider(handler, **settings_updates):
    transport = httpx.MockTransport(handler)
    settings = _settings(**settings_updates)
    client = FreeStockDBHttpClient(settings, transport=transport)
    return FreeStockDBProvider(settings, client=client, now_fn=lambda: NOW)


def test_freestockdb_qfq_and_turnover_normalize_fixed_fixture_contract():
    def handler(request: httpx.Request):
        assert request.url.path == "/api/v1/history/daily"
        return httpx.Response(200, json=_payload(), request=request)

    provider = _provider(handler)
    daily = provider.get_history("600519", date(2026, 7, 23), date(2026, 7, 24))
    turnover = provider.get_turnover_daily(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )

    assert [row.trade_date for row in daily] == [date(2026, 7, 23), date(2026, 7, 24)]
    assert daily[-1].close == Decimal("10.8")
    assert daily[-1].adjustment == "qfq"
    assert daily[-1].volume_unit == "share"
    assert turnover[-1].amount == Decimal("12800")
    assert turnover[-1].turnover_rate == Decimal("1.4")
    assert daily.provider_lineage["adapter_version"] == "1.0.0"
    assert daily.provider_lineage["source_url"].endswith("/api/v1/history/daily")
    assert "?" not in daily.provider_lineage["source_url"]


def test_freestockdb_index_uses_configured_mapping_and_full_ohlcv():
    def handler(request: httpx.Request):
        assert request.url.params["symbol"] == "fixture-csi300"
        return httpx.Response(
            200,
            json=_payload(symbol="fixture-csi300", adjustment="unadjusted"),
            request=request,
        )

    result = _provider(handler).get_index_history(
        "CSI000300", date(2026, 7, 23), date(2026, 7, 24)
    )
    assert result["rows"][-1] == {
        "date": date(2026, 7, 24),
        "open": Decimal("10.5"),
        "high": Decimal("10.9"),
        "low": Decimal("10.3"),
        "close": Decimal("10.8"),
        "volume": Decimal("1200"),
        "amount": Decimal("12800"),
    }
    assert result["adjustment"] == "unadjusted"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda payload: payload["data"].update(rows=[]), "empty"),
        (lambda payload: payload.pop("data"), "schema"),
        (
            lambda payload: payload["data"]["rows"][0].update(close="bad"),
            "numeric",
        ),
        (
            lambda payload: payload["data"]["rows"][1].update(
                trade_date=payload["data"]["rows"][0]["trade_date"]
            ),
            "strictly increasing",
        ),
        (
            lambda payload: payload["data"]["rows"][1].update(high="10.0"),
            "OHLC",
        ),
        (lambda payload: payload["data"].update(volume_unit="lot"), "unit"),
    ],
)
def test_freestockdb_rejects_invalid_business_payload(mutate, match):
    payload = _payload()
    mutate(payload)

    def handler(request: httpx.Request):
        return httpx.Response(200, json=payload, request=request)

    with pytest.raises(ProviderUnavailableError, match=match):
        _provider(handler).get_history(
            "600519", date(2026, 7, 23), date(2026, 7, 24)
        )


def test_freestockdb_missing_native_turnover_is_not_filled_with_zero():
    payload = _payload()
    payload["data"]["rows"][0].pop("turnover_rate")

    def handler(request: httpx.Request):
        return httpx.Response(200, json=payload, request=request)

    with pytest.raises(ProviderUnavailableError, match="turnover_rate"):
        _provider(handler).get_turnover_daily(
            "600519", date(2026, 7, 23), date(2026, 7, 24)
        )


def test_freestockdb_rejects_html_non_json_and_oversize_responses():
    responses = iter(
        [
            httpx.Response(200, text="<html>bad</html>"),
            httpx.Response(200, content=b"not-json"),
            httpx.Response(200, content=b"{" + b"x" * 100 + b"}"),
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
            client.daily_history(
                kind="stock",
                symbol="600519",
                start=date(2026, 7, 23),
                end=date(2026, 7, 24),
                adjustment="qfq",
            )


def test_freestockdb_timeout_is_bounded_and_reported():
    def handler(request: httpx.Request):
        raise httpx.ReadTimeout("fixture timeout", request=request)

    client = FreeStockDBHttpClient(
        _settings(freestockdb_max_retries=2),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderUnavailableError, match="timeout"):
        client.daily_history(
            kind="stock",
            symbol="600519",
            start=date(2026, 7, 23),
            end=date(2026, 7, 24),
            adjustment="qfq",
        )


def test_freestockdb_rejects_remote_base_and_non_loopback_redirect():
    with pytest.raises(ValueError, match="loopback"):
        FreeStockDBHttpClient(_settings(freestockdb_base_url="https://example.com"))

    def handler(request: httpx.Request):
        return httpx.Response(
            302,
            headers={"location": "https://example.com/history"},
            request=request,
        )

    client = FreeStockDBHttpClient(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailableError, match="redirect"):
        client.daily_history(
            kind="stock",
            symbol="600519",
            start=date(2026, 7, 23),
            end=date(2026, 7, 24),
            adjustment="qfq",
        )


def test_freestockdb_health_does_not_download_history():
    paths = []

    def handler(request: httpx.Request):
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "service": "free-stockdb",
                "version": "fixture",
                "status": "ok",
            },
            request=request,
        )

    health = _provider(handler).health_check(probe=True)
    assert health["status"] == "READY"
    assert paths == ["/health"]


def test_freestockdb_raw_digest_is_stable_for_exact_response():
    encoded = json.dumps(_payload(), separators=(",", ":")).encode()

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            content=encoded,
            headers={"content-type": "application/json"},
            request=request,
        )

    first = _provider(handler).get_history(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    second = _provider(handler).get_history(
        "600519", date(2026, 7, 23), date(2026, 7, 24)
    )
    assert first.provider_lineage["raw_response_digest"] == second.provider_lineage[
        "raw_response_digest"
    ]
