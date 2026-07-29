from __future__ import annotations

import builtins
import hashlib
import io
import json
import subprocess
import sys
import time
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.composition.data_hub import build_history_data_hub, build_provider_registry
from app.config import Settings
from app.data_hub.contracts import DailyBar, ObservedRows, ProviderMetadata, TurnoverDaily
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import SHANGHAI_TZ, get_trading_calendar
from app.domain.quality import DataQualityStatus
from app.history.contracts import HistoryRequirementPlan
from app.history.service import HistoricalDataBootstrapService
from app.models import DataQualityRecord, MarketDailyBar
from app.providers import baostock_worker
from app.providers.baostock_provider import (
    BaoStockAuthenticationError,
    BaoStockBenchmarkProvider,
    BaoStockDataUnavailableError,
    BaoStockProtocolError,
    BaoStockStaleDataError,
    BaoStockTimeoutError,
    BaoStockWorkerRunner,
    _canonical_json,
    _communicate_with_hard_timeout,
)
from app.services.history_persistence import persist_index_history_window


NOW = datetime(2026, 7, 28, 18, 0, tzinfo=SHANGHAI_TZ)
CALENDAR = get_trading_calendar()
END = date(2026, 7, 28)


def _session_dates(count: int = 80) -> list[date]:
    current = END
    dates = []
    while len(dates) < count:
        if CALENDAR.is_session(current):
            dates.append(current)
        current -= timedelta(days=1)
    return sorted(dates)


DATES = _session_dates()
START = DATES[0]


def _settings(**updates) -> Settings:
    values = {
        "baostock_enabled": True,
        "baostock_worker_timeout_seconds": 1,
        "baostock_max_response_bytes": 500_000,
        "baostock_max_rows": 500,
    }
    values.update(updates)
    return Settings(**values)


def _raw_rows() -> list[dict[str, str]]:
    return [
        {
            "date": day.isoformat(),
            "code": "sh.000300",
            "open": "4000.1",
            "high": "4020.2",
            "low": "3990.3",
            "close": "4010.4",
            "preclose": "3999.9",
            "volume": str(1_000_000 + index),
            "amount": str(10_000_000_000 + index),
            "pctChg": "0.2625",
        }
        for index, day in enumerate(DATES)
    ]


def _response(rows=None) -> dict:
    rows = deepcopy(rows if rows is not None else _raw_rows())
    return {
        "worker_protocol_version": "1.0.0",
        "baostock_package_version": "00.9.30",
        "operation": "index_daily",
        "external_symbol": "sh.000300",
        "fields": list(baostock_worker.FIELDS),
        "rows": rows,
        "row_count": len(rows),
        "fetched_at": NOW.isoformat(),
        "query_digest": hashlib.sha256(_canonical_json(rows)).hexdigest(),
    }


class StubRunner:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response or _response()
        self.error = error
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return deepcopy(self.response)


def _provider(response=None, **setting_updates) -> BaoStockBenchmarkProvider:
    return BaoStockBenchmarkProvider(
        _settings(**setting_updates),
        runner=StubRunner(response),
        now_fn=lambda: NOW,
    )


def test_exact_baostock_index_series_is_accepted_with_complete_lineage():
    provider = _provider()
    result = provider.get_index_history("CSI000300", START, END)

    assert len(result["rows"]) == 80
    assert result["rows"][-1]["date"] == END
    assert result["rows"][-1]["close"].as_tuple()
    assert result["source"] == "baostock"
    assert result["fetched_at"] == NOW
    lineage = result["provider_lineage"]
    assert lineage["provider_id"] == "baostock-benchmark"
    assert lineage["worker_protocol_version"] == "1.0.0"
    assert lineage["baostock_package_version"] == "00.9.30"
    assert lineage["internal_symbol"] == "CSI000300"
    assert lineage["external_symbol"] == "sh.000300"
    assert lineage["adjustment"] == "unadjusted"
    assert lineage["price_unit"] == "CNY"
    assert lineage["volume_unit"] == "share"
    assert lineage["amount_unit"] == "CNY"
    assert lineage["process_isolation"] == "hard-timeout-subprocess"
    assert len(lineage["request_digest"]) == 64
    assert len(lineage["response_digest"]) == 64
    assert len(lineage["schema_fingerprint"]) == 64


def test_provider_only_advertises_index_daily_and_rejects_other_symbols():
    provider = _provider()
    assert provider.metadata.supported_capabilities == ("market.index_daily",)
    with pytest.raises(BaoStockDataUnavailableError, match="CSI000300"):
        provider.get_index_history("510300", START, END)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda response: response.update(fields=["date"]), "fields"),
        (lambda response: response["rows"][0].update(code="sz.000300"), "symbol"),
        (lambda response: response["rows"][0].update(close=""), "empty"),
        (lambda response: response["rows"][0].update(close="NaN"), "finite"),
        (lambda response: response["rows"][0].update(high="1"), "OHLC"),
        (lambda response: response["rows"][0].update(volume="-1"), "nonnegative"),
        (lambda response: response["rows"][0].update(date="2025-01-01"), "scope"),
        (lambda response: response["rows"].reverse(), "increasing"),
        (lambda response: response["rows"].__setitem__(1, response["rows"][0]), "increasing"),
        (lambda response: response["rows"].pop(), "insufficient"),
    ],
)
def test_provider_rejects_invalid_business_or_protocol_payload(mutate, match):
    response = _response()
    mutate(response)
    response["row_count"] = len(response["rows"])
    response["query_digest"] = hashlib.sha256(
        _canonical_json(response["rows"])
    ).hexdigest()
    with pytest.raises((BaoStockProtocolError, BaoStockDataUnavailableError), match=match):
        _provider(response).get_index_history("CSI000300", START, END)


def test_provider_rejects_digest_row_count_and_partial_window_tampering():
    response = _response()
    response["query_digest"] = "0" * 64
    with pytest.raises(BaoStockProtocolError, match="digest"):
        _provider(response).get_index_history("CSI000300", START, END)

    response = _response()
    response["row_count"] += 1
    with pytest.raises(BaoStockProtocolError, match="row count"):
        _provider(response).get_index_history("CSI000300", START, END)

    rows = _raw_rows()[1:]
    extra_day = START - timedelta(days=1)
    while not CALENDAR.is_session(extra_day):
        extra_day -= timedelta(days=1)
    rows.insert(0, {**_raw_rows()[0], "date": extra_day.isoformat()})
    with pytest.raises(BaoStockProtocolError, match="scope"):
        _provider(_response(rows)).get_index_history("CSI000300", START, END)


def test_provider_rejects_non_latest_request_end():
    with pytest.raises(BaoStockStaleDataError, match="latest completed"):
        _provider().get_index_history("CSI000300", START, DATES[-2])


def test_provider_rejects_minimum_sized_partial_window():
    start = START - timedelta(days=1)
    while not CALENDAR.is_session(start):
        start -= timedelta(days=1)
    with pytest.raises(BaoStockStaleDataError, match="complete window"):
        _provider().get_index_history("CSI000300", start, END)


class FakeProcess:
    def __init__(self, outputs=(b"{}", b""), returncode=0, timeout=False):
        self.outputs = outputs
        self.returncode = returncode
        self.timeout = timeout
        self.killed = False
        self.communicate_calls = 0

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        if self.timeout and self.communicate_calls == 1:
            raise subprocess.TimeoutExpired("worker", timeout)
        if self.killed:
            self.returncode = -9
        return self.outputs

    def kill(self):
        self.killed = True

    def poll(self):
        return self.returncode

    def wait(self):
        self.returncode = -9
        return self.returncode


def test_runner_uses_fixed_command_shell_false_and_stdin_json(monkeypatch):
    captured = {}
    process = FakeProcess((_canonical_json(_response()), b""))

    def popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        original_communicate = process.communicate

        def communicate(input=None, timeout=None):
            captured["input"] = input
            return original_communicate(input=input, timeout=timeout)

        process.communicate = communicate
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    request = {
        "operation": "index_daily",
        "symbol": "CSI000300",
        "start": START.isoformat(),
        "end": END.isoformat(),
    }
    result = BaoStockWorkerRunner(_settings()).run(request)

    assert result["row_count"] == 80
    assert captured["command"] == [
        sys.executable,
        "-m",
        "app.providers.baostock_worker",
    ]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] is subprocess.PIPE
    assert json.loads(captured["input"]) == request


@pytest.mark.parametrize(
    ("stdout", "match"),
    [
        (b"not-json", "invalid JSON"),
        (b'{"ok":true} trailing', "invalid JSON"),
        (b"[]", "root"),
    ],
)
def test_runner_rejects_invalid_or_extra_stdout(monkeypatch, stdout, match):
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess((stdout, b"")))
    with pytest.raises(BaoStockProtocolError, match=match):
        BaoStockWorkerRunner(_settings()).run({})


def test_runner_rejects_oversized_stdout(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess((b"x" * 1025, b"")),
    )
    with pytest.raises(BaoStockProtocolError, match="size"):
        BaoStockWorkerRunner(_settings(baostock_max_response_bytes=1024)).run({})


def test_settings_reject_response_limit_above_worker_boundary():
    with pytest.raises(ValueError, match="less than or equal"):
        _settings(
            baostock_max_response_bytes=baostock_worker.MAX_OUTPUT_BYTES + 1
        )


def test_runner_maps_nonzero_worker_error_and_truncates_stderr(monkeypatch):
    error = {
        "worker_protocol_version": "1.0.0",
        "ok": False,
        "error_type": "AUTHENTICATION",
        "message": "login failed",
    }
    process = FakeProcess((_canonical_json(error), b"secret-like-noise" * 1000), returncode=2)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(BaoStockAuthenticationError, match="login failed") as caught:
        BaoStockWorkerRunner(_settings()).run({})
    assert len(str(caught.value)) <= 500


def test_timeout_path_calls_kill_and_reaps_worker():
    process = FakeProcess(timeout=True)
    with pytest.raises(BaoStockTimeoutError):
        _communicate_with_hard_timeout(process, b"{}", timeout_seconds=1)
    assert process.killed is True
    assert process.communicate_calls == 2
    assert process.poll() is not None


def test_real_sleeping_process_is_killed_and_reaped_within_bound():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    started = time.perf_counter()
    with pytest.raises(BaoStockTimeoutError):
        _communicate_with_hard_timeout(process, b"", timeout_seconds=0.2)
    elapsed = time.perf_counter() - started
    assert elapsed < 2
    assert process.poll() is not None


def test_worker_fixed_protocol_rejects_extra_fields(monkeypatch):
    request = {
        "operation": "index_daily",
        "symbol": "CSI000300",
        "start": START.isoformat(),
        "end": END.isoformat(),
        "command": "arbitrary",
    }
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(_canonical_json(request)), encoding="utf-8"),
    )
    with pytest.raises(baostock_worker.WorkerRequestError, match="fields"):
        baostock_worker._read_request()


def test_worker_normal_query_uses_fixed_baostock_call_and_logout(monkeypatch):
    rows = _raw_rows()
    calls = []

    class Query:
        error_code = "0"
        fields = list(baostock_worker.FIELDS)

        def __init__(self):
            self.index = -1

        def next(self):
            self.index += 1
            return self.index < len(rows)

        def get_row_data(self):
            row = rows[self.index]
            return [row[field] for field in self.fields]

    fake = SimpleNamespace(
        __version__="00.9.30",
        login=lambda: calls.append(("login",)) or SimpleNamespace(error_code="0"),
        logout=lambda: calls.append(("logout",)),
        query_history_k_data_plus=lambda *args, **kwargs: calls.append(
            ("query", args, kwargs)
        )
        or Query(),
    )
    monkeypatch.setitem(sys.modules, "baostock", fake)
    result = baostock_worker._query(START, END)

    assert result["row_count"] == 80
    query_call = next(item for item in calls if item[0] == "query")
    assert query_call[1] == ("sh.000300", ",".join(baostock_worker.FIELDS))
    assert query_call[2] == {
        "start_date": START.isoformat(),
        "end_date": END.isoformat(),
        "frequency": "d",
        "adjustflag": "3",
    }
    assert calls[-1] == ("logout",)


def _worker_sdk(rows, *, noisy: bool = False):
    class Query:
        error_code = "0"
        fields = list(baostock_worker.FIELDS)

        def __init__(self):
            self.index = -1

        def next(self):
            if noisy:
                print("next diagnostic")
            self.index += 1
            return self.index < len(rows)

        def get_row_data(self):
            if noisy:
                print("row diagnostic")
            row = rows[self.index]
            return [row[field] for field in self.fields]

    def login():
        if noisy:
            print("login diagnostic")
        return SimpleNamespace(error_code="0")

    def query(*_args, **_kwargs):
        if noisy:
            print("query diagnostic")
        return Query()

    def logout():
        if noisy:
            print("logout diagnostic")

    return SimpleNamespace(
        __version__="00.9.30",
        login=login,
        logout=logout,
        query_history_k_data_plus=query,
    )


def _invoke_worker_main(monkeypatch, sdk) -> tuple[int, bytes, bytes]:
    request = {
        "operation": "index_daily",
        "symbol": "CSI000300",
        "start": START.isoformat(),
        "end": END.isoformat(),
    }
    stdin = io.TextIOWrapper(io.BytesIO(_canonical_json(request)), encoding="utf-8")
    stdout_buffer = io.BytesIO()
    stderr_buffer = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_buffer, encoding="utf-8")
    stderr = io.TextIOWrapper(stderr_buffer, encoding="utf-8")
    monkeypatch.setitem(sys.modules, "baostock", sdk)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    return_code = baostock_worker.main()
    stdout.flush()
    stderr.flush()
    return return_code, stdout_buffer.getvalue(), stderr_buffer.getvalue()


def test_worker_rejects_overlong_single_field(monkeypatch):
    rows = _raw_rows()[:1]
    rows[0]["close"] = "1" * (baostock_worker.MAX_FIELD_CHARS + 1)
    monkeypatch.setitem(sys.modules, "baostock", _worker_sdk(rows))

    with pytest.raises(baostock_worker.WorkerQueryError, match="field length"):
        baostock_worker._query(START, END)


def test_worker_rejects_total_json_above_fixed_limit(monkeypatch):
    field_value = "1" * baostock_worker.MAX_FIELD_CHARS
    rows = [
        {field: field_value for field in baostock_worker.FIELDS}
        for _ in range(baostock_worker.MAX_ROWS)
    ]

    return_code, stdout, _stderr = _invoke_worker_main(
        monkeypatch, _worker_sdk(rows)
    )

    assert return_code == 2
    assert len(stdout) <= baostock_worker.MAX_ERROR_OUTPUT_BYTES
    assert len(stdout) < baostock_worker.MAX_OUTPUT_BYTES
    response = json.loads(stdout)
    assert response["ok"] is False
    assert "output limit" in response["message"]


def test_worker_import_and_sdk_stdout_do_not_pollute_protocol(
    monkeypatch, capsys
):
    sdk = _worker_sdk(_raw_rows()[:1], noisy=True)
    monkeypatch.delitem(sys.modules, "baostock", raising=False)
    real_import = builtins.__import__

    def noisy_import(name, *args, **kwargs):
        if name == "baostock":
            print("import diagnostic")
            return sdk
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", noisy_import)
    result = baostock_worker._query(START, END)
    captured = capsys.readouterr()

    assert result["row_count"] == 1
    assert captured.out == ""
    assert "import diagnostic" in captured.err
    assert "login diagnostic" in captured.err
    assert "logout diagnostic" in captured.err


def test_worker_error_json_is_always_within_small_fixed_limit(monkeypatch):
    sdk = _worker_sdk(_raw_rows()[:1])
    sdk.login = lambda: (_ for _ in ()).throw(RuntimeError("x" * 20_000))

    return_code, stdout, _stderr = _invoke_worker_main(monkeypatch, sdk)

    assert return_code == 2
    assert len(stdout) <= baostock_worker.MAX_ERROR_OUTPUT_BYTES
    assert set(json.loads(stdout)) == {
        "worker_protocol_version",
        "ok",
        "error_type",
        "message",
    }


def test_worker_normal_136_row_response_is_single_bounded_json(monkeypatch):
    template = _raw_rows()[0]
    rows = [
        {**template, "date": f"2026-01-{(index % 28) + 1:02d}", "volume": str(index + 1)}
        for index in range(136)
    ]

    return_code, stdout, _stderr = _invoke_worker_main(
        monkeypatch, _worker_sdk(rows)
    )

    assert return_code == 0
    assert len(stdout) <= baostock_worker.MAX_OUTPUT_BYTES
    assert json.loads(stdout)["row_count"] == 136


def test_health_maps_timeout_and_post_success_failure_to_degraded():
    runner = StubRunner(error=BaoStockTimeoutError("timeout"))
    provider = BaoStockBenchmarkProvider(
        _settings(), runner=runner, now_fn=lambda: NOW
    )
    assert provider.health_check(probe=True)["status"] == "TIMEOUT"

    runner.error = None
    assert provider.health_check(probe=True)["status"] == "READY"
    runner.error = BaoStockTimeoutError("timeout")
    assert provider.health_check(probe=True)["status"] == "DEGRADED"


def test_composition_routes_benchmark_to_baostock_only_when_enabled(monkeypatch, session):
    enabled = _settings(freestockdb_enabled=True)
    monkeypatch.setattr("app.composition.data_hub.get_settings", lambda: enabled)
    history = build_history_data_hub(session, now_fn=lambda: NOW)
    providers = history.registry.providers_for("market.index_daily")
    assert [provider.provider_id for provider in providers] == ["baostock-benchmark"]
    assert providers[0].metadata.enabled is True

    registry = build_provider_registry()
    baostock = next(item for item in registry.all() if item.provider_id == "baostock-benchmark")
    assert baostock.metadata.supported_capabilities == ("market.index_daily",)


def test_router_records_single_source_benchmark_lineage(session):
    registry = ProviderRegistry()
    provider = _provider()
    registry.register(provider)
    router = DataHubRouter(session, registry, now_fn=lambda: NOW)

    result = router.get_index_history("CSI000300", START, END)

    assert result.quality_status.value == "SINGLE_SOURCE"
    assert result.provider_id == "baostock-benchmark"
    observation = result.provider_observations[0]
    assert observation["process_isolation"] == "hard-timeout-subprocess"
    assert observation["external_symbol"] == "sh.000300"
    assert observation["row_count"] == 80


def test_router_result_persists_benchmark_atomically_with_exact_lineage(session):
    registry = ProviderRegistry()
    registry.register(_provider())
    router = DataHubRouter(session, registry, now_fn=lambda: NOW)
    result = router.get_index_history("CSI000300", START, END)

    written = persist_index_history_window(
        session,
        router,
        result=result,
        subject=result.subject,
        requested_start=START,
        requested_end=END,
        minimum_rows=80,
    )

    assert written == 80
    bars = session.scalars(
        select(MarketDailyBar)
        .where(MarketDailyBar.symbol == "CSI000300")
        .order_by(MarketDailyBar.trade_date)
    ).all()
    assert len(bars) == 80
    assert bars[-1].trade_date == END
    assert bars[-1].source == "baostock"
    assert {bar.quality_record_id for bar in bars} == {result.quality_record_id}
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is True


class StockHistoryFixture:
    metadata = ProviderMetadata(
        provider_id="stock-history-fixture",
        supported_capabilities=("market.daily.qfq", "market.turnover.daily"),
        priority=1,
    )

    @property
    def provider_id(self):
        return self.metadata.provider_id

    @property
    def configured(self):
        return True

    def credential_status(self):
        return {"configured": True, "required_credentials": []}

    def health_check(self, probe: bool = False):
        return {"status": "READY"}

    def get_history(self, symbol, start, end):
        fetched_at = NOW
        rows = [
            DailyBar(
                symbol=symbol,
                trade_date=day,
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10.5"),
                volume=Decimal("1000000"),
                adjustment="qfq",
                price_unit="CNY",
                volume_unit="share",
                observed_at=CALENDAR.session_close_at(day),
                source="stock-history-fixture",
                fetched_at=fetched_at,
            )
            for day in DATES
        ]
        return ObservedRows(
            rows,
            observed_at=CALENDAR.session_close_at(END),
            fetched_at=fetched_at,
            provider_lineage={"row_count": len(rows)},
        )

    def get_turnover_daily(self, symbol, start, end):
        amount = Decimal("100000000")
        rows = [
            TurnoverDaily(
                symbol=symbol,
                trade_date=day,
                turnover_rate=Decimal("2"),
                amount=amount,
                observed_at=CALENDAR.session_close_at(day),
                source="stock-history-fixture",
                fetched_at=NOW,
            )
            for day in DATES
        ]
        return ObservedRows(
            rows,
            observed_at=CALENDAR.session_close_at(END),
            fetched_at=NOW,
            provider_lineage={"row_count": len(rows)},
        )


def test_history_bootstrap_uses_baostock_benchmark_capability(session):
    registry = ProviderRegistry()
    registry.register(StockHistoryFixture())
    registry.register(_provider())
    router = DataHubRouter(session, registry, now_fn=lambda: NOW)
    plan = HistoryRequirementPlan.create(
        trade_date=END,
        benchmark_symbols=("CSI000300",),
        selected_industries=("fixture",),
        required_stock_symbols=("600519",),
        required_capabilities=(
            "market.daily.qfq",
            "market.index_daily",
            "market.turnover.daily",
        ),
        start_date=START,
        end_date=END,
        minimum_rows=80,
        config={"fixture": True},
    )
    service = HistoricalDataBootstrapService(
        session,
        router=router,
        planning_router=router,
        settings=_settings(
            candidate_min_history_coverage_ratio="0.8",
            freestockdb_history_lookback_sessions=80,
            freestockdb_refresh_rewrite_sessions=80,
        ),
        plan_factory=lambda **kwargs: plan,
        now_fn=lambda: NOW,
    )

    first = service.run(trade_date=END, now=NOW)
    second = service.run(trade_date=END, now=NOW)

    assert first.status == "SUCCEEDED"
    assert first.provider_id == "freestockdb+baostock-benchmark"
    assert first.adapter_version == "freestockdb:1.0.0;baostock:1.0.0"
    assert first.benchmark_ready is True
    assert first.coverage_ratio == 1
    assert first.blocked_reasons == []
    assert second.id == first.id
    benchmark_record = session.scalar(
        select(DataQualityRecord)
        .where(DataQualityRecord.capability == "market.index_daily")
        .order_by(DataQualityRecord.id.desc())
    )
    assert benchmark_record.quality_status == DataQualityStatus.SINGLE_SOURCE.value
    assert benchmark_record.persisted is True
