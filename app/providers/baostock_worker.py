from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
from datetime import date, datetime, timezone
from typing import Any


WORKER_PROTOCOL_VERSION = "1.0.0"
OPERATION = "index_daily"
INTERNAL_SYMBOL = "CSI000300"
EXTERNAL_SYMBOL = "sh.000300"
FIELDS = (
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "pctChg",
)
MAX_INPUT_BYTES = 4096
MAX_ROWS = 1000
MAX_CALENDAR_DAYS = 2000
MAX_DIAGNOSTIC_CHARS = 2000
MAX_FIELD_CHARS = 256
MAX_OUTPUT_BYTES = 2_000_000
MAX_ERROR_OUTPUT_BYTES = 4096


class WorkerRequestError(ValueError):
    pass


class WorkerAuthenticationError(RuntimeError):
    pass


class WorkerQueryError(RuntimeError):
    pass


class WorkerDataUnavailableError(RuntimeError):
    pass


class _DiagnosticBuffer(io.TextIOBase):
    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self.parts: list[str] = []
        self.length = 0

    def write(self, value: str) -> int:
        rendered = str(value)
        remaining = self.max_chars - self.length
        if remaining > 0:
            part = rendered[:remaining]
            self.parts.append(part)
            self.length += len(part)
        return len(rendered)

    def getvalue(self) -> str:
        return "".join(self.parts)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _field_value(value: Any) -> str:
    rendered = str(value)
    if not rendered.strip():
        raise WorkerQueryError("BaoStock row field must be non-empty")
    if len(rendered) > MAX_FIELD_CHARS:
        raise WorkerQueryError("BaoStock row field length exceeds the fixed limit")
    return rendered


def _read_request() -> tuple[dict[str, str], date, date]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise WorkerRequestError("worker request is too large")
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerRequestError("worker request must be one UTF-8 JSON object") from exc
    if not isinstance(request, dict) or set(request) != {
        "operation",
        "symbol",
        "start",
        "end",
    }:
        raise WorkerRequestError("worker request fields are invalid")
    if request["operation"] != OPERATION or request["symbol"] != INTERNAL_SYMBOL:
        raise WorkerRequestError("worker operation or symbol is unsupported")
    try:
        start = date.fromisoformat(request["start"])
        end = date.fromisoformat(request["end"])
    except (TypeError, ValueError) as exc:
        raise WorkerRequestError("worker dates must use ISO format") from exc
    if request["start"] != start.isoformat() or request["end"] != end.isoformat():
        raise WorkerRequestError("worker dates must use canonical ISO format")
    if start > end:
        raise WorkerRequestError("worker date range is invalid")
    if (end - start).days > MAX_CALENDAR_DAYS:
        raise WorkerRequestError("worker date range exceeds the fixed limit")
    return request, start, end


def _query(start: date, end: date) -> dict[str, Any]:
    login_result = None
    bs = None
    sdk_output = _DiagnosticBuffer(MAX_DIAGNOSTIC_CHARS)
    result: dict[str, Any] | None = None
    try:
        with contextlib.redirect_stdout(sdk_output):
            import baostock as bs

            login_result = bs.login()
        if str(getattr(login_result, "error_code", "")) != "0":
            raise WorkerAuthenticationError("BaoStock login failed")
        with contextlib.redirect_stdout(sdk_output):
            query = bs.query_history_k_data_plus(
                EXTERNAL_SYMBOL,
                ",".join(FIELDS),
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag="3",
            )
        if str(getattr(query, "error_code", "")) != "0":
            raise WorkerQueryError("BaoStock index query failed")
        actual_fields = tuple(
            _field_value(item) for item in getattr(query, "fields", ())
        )
        if actual_fields != FIELDS:
            raise WorkerQueryError("BaoStock query fields do not match the fixed protocol")
        rows: list[dict[str, str]] = []
        while True:
            with contextlib.redirect_stdout(sdk_output):
                has_next = query.next()
            if not has_next:
                break
            with contextlib.redirect_stdout(sdk_output):
                raw_row = query.get_row_data()
            if not isinstance(raw_row, list) or len(raw_row) != len(FIELDS):
                raise WorkerQueryError("BaoStock row shape is invalid")
            row = dict(
                zip(FIELDS, (_field_value(value) for value in raw_row), strict=True)
            )
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise WorkerQueryError("BaoStock row count exceeds the fixed worker limit")
        if not rows:
            raise WorkerDataUnavailableError("BaoStock index data is unavailable")
        query_digest = hashlib.sha256(_canonical_json(rows)).hexdigest()
        package_version = _field_value(getattr(bs, "__version__", ""))
        result = {
            "worker_protocol_version": WORKER_PROTOCOL_VERSION,
            "baostock_package_version": package_version,
            "operation": OPERATION,
            "external_symbol": EXTERNAL_SYMBOL,
            "fields": list(FIELDS),
            "rows": rows,
            "row_count": len(rows),
            "query_digest": query_digest,
        }
    finally:
        if bs is not None and login_result is not None:
            try:
                with contextlib.redirect_stdout(sdk_output):
                    bs.logout()
            except Exception as exc:
                print(
                    f"BaoStock logout failed: {type(exc).__name__}"[:MAX_DIAGNOSTIC_CHARS],
                    file=sys.stderr,
                )
        diagnostic = sdk_output.getvalue().strip()
        if diagnostic:
            print(diagnostic[:MAX_DIAGNOSTIC_CHARS], file=sys.stderr)
    if result is None:
        raise WorkerQueryError("BaoStock worker did not produce a result")
    return {**result, "fetched_at": datetime.now(timezone.utc).isoformat()}


def _error_type(exc: Exception) -> str:
    if isinstance(exc, WorkerRequestError):
        return "REQUEST"
    if isinstance(exc, WorkerAuthenticationError):
        return "AUTHENTICATION"
    if isinstance(exc, WorkerDataUnavailableError):
        return "DATA_UNAVAILABLE"
    if isinstance(exc, WorkerQueryError):
        return "QUERY"
    if isinstance(exc, (ConnectionError, OSError)):
        return "CONNECTION"
    return "QUERY"


def _error_json(exc: Exception) -> bytes:
    response = {
        "worker_protocol_version": WORKER_PROTOCOL_VERSION,
        "ok": False,
        "error_type": _error_type(exc),
        "message": f"{type(exc).__name__}: {str(exc)}"[:500],
    }
    encoded = _canonical_json(response)
    if len(encoded) <= MAX_ERROR_OUTPUT_BYTES:
        return encoded
    return _canonical_json(
        {
            "worker_protocol_version": WORKER_PROTOCOL_VERSION,
            "ok": False,
            "error_type": "QUERY",
            "message": "WorkerQueryError: bounded worker failure",
        }
    )


def main() -> int:
    try:
        _request, start, end = _read_request()
        response = _query(start, end)
        encoded = _canonical_json(response)
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise WorkerQueryError(
                "BaoStock worker response exceeds the fixed output limit"
            )
    except Exception as exc:
        sys.stdout.buffer.write(_error_json(exc))
        return 2
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
