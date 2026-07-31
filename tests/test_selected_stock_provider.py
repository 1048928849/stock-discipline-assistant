from __future__ import annotations

import json
import subprocess
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.composition.data_hub import build_selected_stock_data_hub
from app.config import Settings
from app.providers.selected_stock_history import (
    PublicHistoryProtocolError,
    PublicHistoryWorkerRunner,
    SelectedStockPublicHistoryProvider,
)
from app.providers.selected_stock_history_worker import (
    MAX_ERROR_BYTES,
    MAX_FIELD_CHARS,
    MAX_OUTPUT_BYTES,
    PROTOCOL_VERSION,
    WorkerError,
    _canonical_json,
    _error_payload,
    _frame_rows,
    process,
)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("300308", "sz300308"),
        ("600519", "sh600519"),
        ("430047", "bj430047"),
        ("830799", "bj830799"),
        ("920985", "bj920985"),
    ],
)
def test_selected_stock_worker_maps_exchange_prefixes(symbol, expected):
    from app.providers.selected_stock_history_worker import _prefixed

    assert _prefixed(symbol) == expected


SHANGHAI = ZoneInfo("Asia/Shanghai")
FETCHED = datetime(2026, 7, 31, 16, 0, tzinfo=SHANGHAI)


def _response(**updates):
    payload = {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "akshare_version": "1.18.72",
        "source": "akshare_tencent_qfq",
        "source_function": "stock_zh_a_hist_tx",
        "adjustment": "qfq",
        "price_unit": "CNY",
        "volume_unit": "share",
        "fetched_at": FETCHED.isoformat(),
        "rows": [
            {
                "symbol": "300308",
                "trade_date": "2026-07-31",
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10.5",
                "volume": "120000",
            }
        ],
        "row_count": 1,
        "attempts": ["stock_zh_a_hist_tx"],
    }
    payload.update(updates)
    return payload


class _Runner:
    def __init__(self, response):
        self.response = response

    def run(self, request):
        return self.response


def test_public_provider_preserves_qfq_dimensions_and_digests():
    settings = Settings(selected_stock_history_max_rows=800)
    provider = SelectedStockPublicHistoryProvider(
        settings,
        runner=_Runner(_response()),
        now_fn=lambda: FETCHED,
    )
    rows = provider.get_history("300308", date(2026, 7, 31), date(2026, 7, 31))
    assert len(rows) == 1
    assert rows[0].adjustment == "qfq"
    assert rows[0].volume == Decimal("120000")
    assert rows.provider_lineage["source_function"] == "stock_zh_a_hist_tx"
    assert len(rows.provider_lineage["request_digest"]) == 64
    assert len(rows.provider_lineage["response_digest"]) == 64


@pytest.mark.parametrize(
    "updates",
    [
        {"adjustment": "unadjusted"},
        {"volume_unit": "lot"},
        {"row_count": 2},
        {"protocol_version": "changed"},
        {"unexpected": True},
    ],
)
def test_public_provider_rejects_schema_and_dimension_changes(updates):
    provider = SelectedStockPublicHistoryProvider(
        Settings(),
        runner=_Runner(_response(**updates)),
    )
    with pytest.raises((PublicHistoryProtocolError, RuntimeError, ValueError)):
        provider.get_history("300308", date(2026, 7, 31), date(2026, 7, 31))


def test_tencent_volume_conversion_is_stable_decimal_not_float():
    frame = pd.DataFrame(
        [
            {
                "date": "2026-07-31",
                "open": "1.1",
                "high": "1.2",
                "low": "1.0",
                "close": "1.15",
                "amount": "0.29",
            }
        ]
    )
    rows = _frame_rows(frame, source="tencent", symbol="300308")
    assert rows[0]["volume"] == "29.00"


def test_worker_uses_sina_only_after_tencent_failure():
    class _AK:
        __version__ = "1.18.72"

        @staticmethod
        def stock_zh_a_hist_tx(**kwargs):
            raise ConnectionError("bounded failure")

        @staticmethod
        def stock_zh_a_daily(**kwargs):
            return pd.DataFrame(
                [
                    {
                        "date": "2026-07-31",
                        "open": "10",
                        "high": "11",
                        "low": "9",
                        "close": "10.5",
                        "volume": "1000",
                    }
                ]
            )

    result = process(
        {"symbol": "300308", "start": "2026-07-31", "end": "2026-07-31"},
        _AK(),
    )
    assert result["source_function"] == "stock_zh_a_daily"
    assert result["attempts"] == ["stock_zh_a_hist_tx", "stock_zh_a_daily"]


def test_worker_field_and_error_outputs_are_bounded():
    frame = pd.DataFrame(
        [
            {
                "date": "2026-07-31",
                "open": "1" * (MAX_FIELD_CHARS + 1),
                "high": "2",
                "low": "1",
                "close": "1",
                "volume": "1",
            }
        ]
    )
    with pytest.raises(WorkerError, match="invalid field"):
        _frame_rows(frame, source="sina", symbol="300308")
    error = _error_payload(RuntimeError("x" * 100_000))
    assert len(error) <= MAX_ERROR_BYTES
    assert json.loads(error)["ok"] is False
    assert MAX_OUTPUT_BYTES >= len(_canonical_json(_response()))


def test_parent_timeout_kills_and_reaps_worker(monkeypatch):
    class _Process:
        returncode = 0

        def __init__(self):
            self.calls = 0
            self.killed = False

        def communicate(self, payload=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("worker", timeout)
            return b"", b""

        def kill(self):
            self.killed = True

    process_instance = _Process()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process_instance)
    with pytest.raises(RuntimeError, match="timed out"):
        PublicHistoryWorkerRunner(Settings()).run(
            {"symbol": "300308", "start": "2026-01-01", "end": "2026-07-31"}
        )
    assert process_instance.killed is True
    assert process_instance.calls == 2


def test_parent_rejects_response_over_configured_limit(monkeypatch):
    response = b"x" * 2049
    process_instance = SimpleNamespace(
        returncode=0,
        communicate=lambda payload, timeout: (response, b""),
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process_instance)
    settings = Settings(selected_stock_history_max_response_bytes=2048)
    with pytest.raises(PublicHistoryProtocolError, match="parent limit"):
        PublicHistoryWorkerRunner(settings).run(
            {"symbol": "300308", "start": "2026-01-01", "end": "2026-07-31"}
        )


def test_selected_stock_provider_priority_is_bounded_and_deterministic(session):
    router = build_selected_stock_data_hub(session, now_fn=lambda: FETCHED)
    provider_ids = [
        item.provider_id for item in router.registry.providers_for("market.daily.qfq")
    ]
    assert provider_ids[:2] == ["freestockdb", "selected-stock-public-history"]
    assert "akshare-selected-context" not in provider_ids
    assert router.registry.providers_for("market.index_daily")[0].provider_id == "baostock-benchmark"
