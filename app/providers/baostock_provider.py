from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.config import Settings
from app.data_hub.contracts import (
    MarketDataProvider,
    ProviderMetadata,
    ProviderUnavailableError,
)
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    shanghai_now,
    to_shanghai_aware,
)
from app.providers.baostock_worker import (
    EXTERNAL_SYMBOL,
    FIELDS,
    INTERNAL_SYMBOL,
    OPERATION,
    WORKER_PROTOCOL_VERSION,
)


BAOSTOCK_PACKAGE_VERSION = "00.9.30"
MINIMUM_INDEX_ROWS = 80
STDERR_LIMIT_BYTES = 4096


class BaoStockError(ProviderUnavailableError):
    pass


class BaoStockConnectionError(BaoStockError):
    pass


class BaoStockTimeoutError(BaoStockError):
    pass


class BaoStockAuthenticationError(BaoStockError):
    pass


class BaoStockQueryError(BaoStockError):
    pass


class BaoStockProtocolError(BaoStockError):
    pass


class BaoStockDataUnavailableError(BaoStockError):
    pass


class BaoStockStaleDataError(BaoStockError):
    pass


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _worker_command() -> list[str]:
    return [sys.executable, "-m", "app.providers.baostock_worker"]


def _communicate_with_hard_timeout(
    process: subprocess.Popen,
    payload: bytes,
    *,
    timeout_seconds: float,
) -> tuple[bytes, bytes]:
    try:
        stdout, stderr = process.communicate(input=payload, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate()
        if process.poll() is None:
            process.wait()
        raise BaoStockTimeoutError("BaoStock worker exceeded the hard timeout") from exc
    return bytes(stdout or b""), bytes(stderr or b"")


class BaoStockWorkerRunner:
    def __init__(self, settings: Settings) -> None:
        self.timeout_seconds = settings.baostock_worker_timeout_seconds
        self.max_response_bytes = settings.baostock_max_response_bytes

    @staticmethod
    def _worker_error(stdout: bytes, stderr: bytes) -> BaoStockError:
        detail = stderr[:STDERR_LIMIT_BYTES].decode("utf-8", errors="replace").strip()
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return BaoStockProtocolError(
                f"BaoStock worker failed with an invalid error response: {detail}"[:500]
            )
        if (
            not isinstance(response, dict)
            or set(response)
            != {"worker_protocol_version", "ok", "error_type", "message"}
            or response.get("worker_protocol_version") != WORKER_PROTOCOL_VERSION
            or response.get("ok") is not False
            or not isinstance(response.get("error_type"), str)
            or not isinstance(response.get("message"), str)
        ):
            return BaoStockProtocolError("BaoStock worker error response is invalid")
        message = str(response.get("message") or detail or "BaoStock worker failed")[:500]
        error_type = response.get("error_type")
        errors = {
            "CONNECTION": BaoStockConnectionError,
            "AUTHENTICATION": BaoStockAuthenticationError,
            "QUERY": BaoStockQueryError,
            "DATA_UNAVAILABLE": BaoStockDataUnavailableError,
            "REQUEST": BaoStockProtocolError,
        }
        return errors.get(error_type, BaoStockProtocolError)(message)

    def run(self, request: dict[str, str]) -> dict[str, Any]:
        payload = _canonical_json(request)
        try:
            process = subprocess.Popen(
                _worker_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as exc:
            raise BaoStockConnectionError("BaoStock worker could not be started") from exc
        stdout, stderr = _communicate_with_hard_timeout(
            process,
            payload,
            timeout_seconds=self.timeout_seconds,
        )
        if len(stdout) > self.max_response_bytes:
            raise BaoStockProtocolError("BaoStock worker response exceeds the size limit")
        if process.returncode != 0:
            raise self._worker_error(stdout, stderr)
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BaoStockProtocolError("BaoStock worker returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise BaoStockProtocolError("BaoStock worker response root must be an object")
        return response


class BaoStockBenchmarkProvider(MarketDataProvider):
    source = "baostock"

    def __init__(
        self,
        settings: Settings,
        *,
        runner: BaoStockWorkerRunner | None = None,
        calendar: TradingCalendar | None = None,
        now_fn=None,
    ) -> None:
        self.settings = settings
        self.runner = runner or BaoStockWorkerRunner(settings)
        self.calendar = calendar or get_trading_calendar()
        self.now_fn = now_fn or shanghai_now
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self.metadata = ProviderMetadata(
            provider_id="baostock-benchmark",
            supported_capabilities=("market.index_daily",),
            enabled=settings.baostock_enabled,
            priority=30,
            health_status="DISABLED" if not settings.baostock_enabled else "DEGRADED",
            timeout=settings.baostock_worker_timeout_seconds,
            retry=0,
            rate_limit="one fixed CSI300 request per call",
        )

    def _now(self) -> datetime:
        value = self.now_fn()
        return to_shanghai_aware(value, naive_is_shanghai=value.tzinfo is None)

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        if not isinstance(value, str) or not value.strip():
            raise BaoStockProtocolError(f"BaoStock {field} must be a non-empty string")
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise BaoStockProtocolError(f"BaoStock {field} is not decimal") from exc
        if not number.is_finite():
            raise BaoStockProtocolError(f"BaoStock {field} must be finite")
        return number

    @staticmethod
    def _trade_date(value: Any) -> date:
        if not isinstance(value, str) or not value.strip():
            raise BaoStockProtocolError("BaoStock date must be a non-empty string")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise BaoStockProtocolError("BaoStock date must use ISO format") from exc
        if parsed.isoformat() != value:
            raise BaoStockProtocolError("BaoStock date must use canonical ISO format")
        return parsed

    def _validate_request(self, symbol: str, start: date, end: date) -> None:
        if symbol != INTERNAL_SYMBOL:
            raise BaoStockDataUnavailableError("BaoStock only supports CSI000300")
        if start > end:
            raise BaoStockDataUnavailableError("BaoStock date range is invalid")
        if not self.calendar.is_session(end):
            raise BaoStockDataUnavailableError(
                "BaoStock range end must be a trading session"
            )
        latest = self.calendar.latest_completed_session(self._now())
        if end != latest:
            raise BaoStockStaleDataError(
                "BaoStock request must end at the latest completed trading session"
            )
        session_count = sum(
            1
            for offset in range((end - start).days + 1)
            if self.calendar.is_session(start + timedelta(days=offset))
        )
        if session_count > self.settings.baostock_max_rows:
            raise BaoStockDataUnavailableError("BaoStock range exceeds configured max rows")

    def _validate_response(
        self,
        response: dict[str, Any],
        *,
        start: date,
        end: date,
        request_started_at: datetime,
        request_completed_at: datetime,
    ) -> tuple[list[dict[str, Any]], datetime]:
        expected_keys = {
            "worker_protocol_version",
            "baostock_package_version",
            "operation",
            "external_symbol",
            "fields",
            "rows",
            "row_count",
            "fetched_at",
            "query_digest",
        }
        if set(response) != expected_keys:
            raise BaoStockProtocolError("BaoStock worker response fields are invalid")
        if response["worker_protocol_version"] != WORKER_PROTOCOL_VERSION:
            raise BaoStockProtocolError("BaoStock worker protocol version is unsupported")
        if response["baostock_package_version"] != BAOSTOCK_PACKAGE_VERSION:
            raise BaoStockProtocolError("BaoStock package version is unsupported")
        if response["operation"] != OPERATION or response["external_symbol"] != EXTERNAL_SYMBOL:
            raise BaoStockProtocolError("BaoStock worker response scope is invalid")
        if response["fields"] != list(FIELDS):
            raise BaoStockProtocolError("BaoStock worker fields are invalid")
        raw_rows = response["rows"]
        if not isinstance(raw_rows, list) or not (
            MINIMUM_INDEX_ROWS <= len(raw_rows) <= self.settings.baostock_max_rows
        ):
            raise BaoStockDataUnavailableError("BaoStock index rows are insufficient")
        if response["row_count"] != len(raw_rows):
            raise BaoStockProtocolError("BaoStock worker row count is invalid")
        expected_digest = hashlib.sha256(_canonical_json(raw_rows)).hexdigest()
        if response["query_digest"] != expected_digest:
            raise BaoStockProtocolError("BaoStock worker query digest is invalid")
        try:
            fetched_at = datetime.fromisoformat(response["fetched_at"])
        except (TypeError, ValueError) as exc:
            raise BaoStockProtocolError("BaoStock fetched_at is invalid") from exc
        if fetched_at.tzinfo is None:
            raise BaoStockProtocolError("BaoStock fetched_at must be timezone-aware")
        fetched_at = to_shanghai_aware(fetched_at)
        if not (
            request_started_at - timedelta(seconds=1)
            <= fetched_at
            <= request_completed_at + timedelta(seconds=1)
        ):
            raise BaoStockProtocolError(
                "BaoStock fetched_at does not match request completion"
            )

        rows: list[dict[str, Any]] = []
        dates: list[date] = []
        for raw in raw_rows:
            if not isinstance(raw, dict) or set(raw) != set(FIELDS):
                raise BaoStockProtocolError("BaoStock row fields are invalid")
            if any(not isinstance(raw[field], str) or not raw[field].strip() for field in FIELDS):
                raise BaoStockProtocolError("BaoStock row contains an empty field")
            if raw["code"] != EXTERNAL_SYMBOL:
                raise BaoStockProtocolError("BaoStock row symbol is invalid")
            trade_date = self._trade_date(raw["date"])
            if trade_date < start or trade_date > end or not self.calendar.is_session(trade_date):
                raise BaoStockProtocolError("BaoStock row date is outside the request scope")
            numeric = {
                field: self._decimal(raw[field], field)
                for field in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "preclose",
                    "volume",
                    "amount",
                    "pctChg",
                )
            }
            prices = tuple(numeric[field] for field in ("open", "high", "low", "close"))
            if min((*prices, numeric["preclose"])) <= 0:
                raise BaoStockProtocolError("BaoStock prices must be positive")
            if numeric["high"] < max(prices) or numeric["low"] > min(prices):
                raise BaoStockProtocolError("BaoStock OHLC relationship is invalid")
            if numeric["volume"] < 0 or numeric["amount"] < 0:
                raise BaoStockProtocolError("BaoStock volume and amount must be nonnegative")
            dates.append(trade_date)
            rows.append({"date": trade_date, "code": raw["code"], **numeric})
        if dates != sorted(set(dates)):
            raise BaoStockProtocolError("BaoStock dates must be unique and increasing")
        expected_dates = [
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
            if self.calendar.is_session(start + timedelta(days=offset))
        ]
        if dates != expected_dates:
            raise BaoStockStaleDataError("BaoStock result does not cover the complete window")
        return rows, fetched_at

    def _request(self, symbol: str, start: date, end: date) -> dict[str, Any]:
        self._validate_request(symbol, start, end)
        request = {
            "operation": OPERATION,
            "symbol": INTERNAL_SYMBOL,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        request_started_at = self._now()
        response = self.runner.run(request)
        request_completed_at = self._now()
        rows, fetched_at = self._validate_response(
            response,
            start=start,
            end=end,
            request_started_at=request_started_at,
            request_completed_at=request_completed_at,
        )
        request_digest = hashlib.sha256(_canonical_json(request)).hexdigest()
        schema_fingerprint = hashlib.sha256(_canonical_json(list(FIELDS))).hexdigest()
        lineage = {
            "provider_id": "baostock-benchmark",
            "adapter_version": self.settings.baostock_adapter_version,
            "worker_protocol_version": response["worker_protocol_version"],
            "baostock_package_version": response["baostock_package_version"],
            "internal_symbol": INTERNAL_SYMBOL,
            "external_symbol": EXTERNAL_SYMBOL,
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "requested_fields": list(FIELDS),
            "actual_fields": response["fields"],
            "adjustment": "unadjusted",
            "price_unit": "CNY",
            "volume_unit": "share",
            "amount_unit": "CNY",
            "row_count": len(rows),
            "request_digest": request_digest,
            "response_digest": response["query_digest"],
            "schema_fingerprint": schema_fingerprint,
            "fetched_at": fetched_at.isoformat(),
            "source_service": "BaoStock remote service",
            "process_isolation": "hard-timeout-subprocess",
            "timeout_seconds": self.settings.baostock_worker_timeout_seconds,
        }
        self._last_success_at = fetched_at
        self._last_error = None
        return {
            "rows": rows,
            "source": self.source,
            "fetched_at": fetched_at,
            "provider_lineage": lineage,
        }

    def get_index_history(self, symbol: str, start: date, end: date) -> dict[str, Any]:
        if not self.settings.baostock_enabled:
            raise BaoStockDataUnavailableError("BaoStock benchmark provider is disabled")
        try:
            return self._request(symbol, start, end)
        except BaoStockError as exc:
            self._last_error = str(exc)[:300]
            raise

    def _health_base(self) -> dict[str, Any]:
        return {
            "adapter_version": self.settings.baostock_adapter_version,
            "worker_protocol_version": WORKER_PROTOCOL_VERSION,
            "baostock_package_version": BAOSTOCK_PACKAGE_VERSION,
            "supported_capabilities": list(self.metadata.supported_capabilities),
            "process_isolation": "hard-timeout-subprocess",
            "timeout_seconds": self.settings.baostock_worker_timeout_seconds,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
        }

    def health_check(self, probe: bool = False) -> dict[str, Any]:
        if not self.settings.baostock_enabled:
            return {"status": "DISABLED", "message": "provider disabled", **self._health_base()}
        if not probe:
            status = "READY" if self._last_success_at and not self._last_error else "DEGRADED"
            return {"status": status, "message": self._last_error, **self._health_base()}
        end = self.calendar.latest_completed_session(self._now())
        start = end
        sessions = 1
        while sessions < MINIMUM_INDEX_ROWS:
            start -= timedelta(days=1)
            if self.calendar.is_session(start):
                sessions += 1
        try:
            self.get_index_history(INTERNAL_SYMBOL, start, end)
            return {"status": "READY", "message": "CSI300 benchmark ready", **self._health_base()}
        except BaoStockError as exc:
            self._last_error = str(exc)[:300]
            if self._last_success_at is not None:
                status = "DEGRADED"
            elif isinstance(exc, BaoStockTimeoutError):
                status = "TIMEOUT"
            elif isinstance(exc, (BaoStockConnectionError, BaoStockAuthenticationError)):
                status = "UNREACHABLE"
            elif isinstance(exc, BaoStockProtocolError):
                status = "PROTOCOL_UNSUPPORTED"
            elif isinstance(exc, BaoStockStaleDataError):
                status = "STALE"
            else:
                status = "DATA_UNAVAILABLE"
            return {"status": status, "message": self._last_error, **self._health_base()}

    def get_history(self, symbol: str, start: date, end: date):
        raise BaoStockDataUnavailableError("BaoStock benchmark provider does not provide stocks")

    def get_quote(self, symbol: str):
        raise BaoStockDataUnavailableError("BaoStock benchmark provider does not provide quotes")

    def get_sector_history(self, industry: str, start: date, end: date):
        raise BaoStockDataUnavailableError("BaoStock benchmark provider does not provide sectors")


__all__ = [
    "BaoStockAuthenticationError",
    "BaoStockBenchmarkProvider",
    "BaoStockConnectionError",
    "BaoStockDataUnavailableError",
    "BaoStockProtocolError",
    "BaoStockQueryError",
    "BaoStockStaleDataError",
    "BaoStockTimeoutError",
    "BaoStockWorkerRunner",
]
