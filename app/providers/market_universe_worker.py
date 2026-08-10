from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
from datetime import date, datetime, timezone


PROTOCOL_VERSION = "market-universe-worker-v1"
OPERATION = "security_master"
FIELDS = ("code", "code_name", "ipoDate", "outDate", "type", "status")
MAX_INPUT_BYTES = 2048
MAX_ROWS = 10_000
MAX_OUTPUT_BYTES = 4_000_000
MAX_DIAGNOSTIC_CHARS = 2000


class WorkerError(RuntimeError):
    pass


class _Diagnostics(io.TextIOBase):
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0

    def write(self, value: str) -> int:
        rendered = str(value)
        remaining = MAX_DIAGNOSTIC_CHARS - self.size
        if remaining > 0:
            part = rendered[:remaining]
            self.parts.append(part)
            self.size += len(part)
        return len(rendered)

    def getvalue(self) -> str:
        return "".join(self.parts)


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _read_request() -> date:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise WorkerError("request exceeds fixed input limit")
    try:
        request = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerError("request must be one JSON object") from exc
    if not isinstance(request, dict) or set(request) != {"operation", "trade_date"}:
        raise WorkerError("request fields are invalid")
    if request["operation"] != OPERATION:
        raise WorkerError("operation is unsupported")
    try:
        trade_date = date.fromisoformat(request["trade_date"])
    except (TypeError, ValueError) as exc:
        raise WorkerError("trade_date is invalid") from exc
    if request["trade_date"] != trade_date.isoformat():
        raise WorkerError("trade_date must be canonical ISO")
    return trade_date


def _query(trade_date: date) -> dict:
    diagnostics = _Diagnostics()
    bs = None
    logged_in = False
    try:
        with contextlib.redirect_stdout(diagnostics):
            import baostock as bs

            login = bs.login()
        if str(getattr(login, "error_code", "")) != "0":
            raise WorkerError("BaoStock login failed")
        logged_in = True
        with contextlib.redirect_stdout(diagnostics):
            query = bs.query_stock_basic()
        if str(getattr(query, "error_code", "")) != "0":
            raise WorkerError("BaoStock security master query failed")
        if tuple(getattr(query, "fields", ())) != FIELDS:
            raise WorkerError("BaoStock security master schema changed")
        rows = []
        while True:
            with contextlib.redirect_stdout(diagnostics):
                has_next = query.next()
            if not has_next:
                break
            with contextlib.redirect_stdout(diagnostics):
                values = query.get_row_data()
            if not isinstance(values, list) or len(values) != len(FIELDS):
                raise WorkerError("BaoStock security master row is invalid")
            row = dict(zip(FIELDS, (str(value) for value in values), strict=True))
            if any(len(value) > 256 for value in row.values()):
                raise WorkerError("BaoStock security master field is oversized")
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise WorkerError("BaoStock security master exceeds row limit")
        if not rows:
            raise WorkerError("BaoStock security master is unavailable")
        return {
            "protocol_version": PROTOCOL_VERSION,
            "operation": OPERATION,
            "trade_date": trade_date.isoformat(),
            "source": "baostock.query_stock_basic",
            "fields": list(FIELDS),
            "rows": rows,
            "row_count": len(rows),
            "response_digest": hashlib.sha256(_canonical(rows)).hexdigest(),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        if bs is not None and logged_in:
            try:
                with contextlib.redirect_stdout(diagnostics):
                    bs.logout()
            except Exception:
                pass
        if diagnostics.getvalue().strip():
            print(diagnostics.getvalue().strip(), file=sys.stderr)


def main() -> int:
    try:
        response = _query(_read_request())
        encoded = _canonical(response)
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise WorkerError("response exceeds fixed output limit")
    except Exception as exc:
        encoded = _canonical(
            {
                "protocol_version": PROTOCOL_VERSION,
                "ok": False,
                "error_type": type(exc).__name__,
                "message": str(exc)[:500],
            }
        )
        sys.stdout.buffer.write(encoded)
        return 2
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
