from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import io
import json
import sys
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


WORKER_PROTOCOL_VERSION = "market-breadth-worker-v1"
OPERATION = "limit_pools"
MAX_FIELD_CHARS = 512
MAX_POOL_ROWS = 2000
MAX_OUTPUT_BYTES = 2_000_000
MAX_ERROR_BYTES = 4096
MAX_DIAGNOSTIC_CHARS = 4096


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _request() -> tuple[date, str]:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request must be one JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != {"operation", "trade_date"}:
        raise ValueError("request fields are invalid")
    if payload["operation"] != OPERATION:
        raise ValueError("operation is unsupported")
    if not isinstance(payload["trade_date"], str):
        raise ValueError("trade_date must be an ISO date string")
    try:
        trade_date = date.fromisoformat(payload["trade_date"])
    except ValueError as exc:
        raise ValueError("trade_date must use canonical ISO format") from exc
    if trade_date.isoformat() != payload["trade_date"]:
        raise ValueError("trade_date must use canonical ISO format")
    return trade_date, trade_date.strftime("%Y%m%d")


def _string(value: Any, *, field: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text or len(text) > MAX_FIELD_CHARS:
        raise RuntimeError(f"{field} is empty or exceeds the field limit")
    return text


def _pool(frame: Any, *, name: str) -> tuple[list[str], int, str]:
    if frame is None or not hasattr(frame, "columns"):
        raise RuntimeError(f"{name} response is not tabular")
    fields = [_string(item, field=f"{name} field") for item in frame.columns]
    if "代码" not in fields:
        raise RuntimeError(f"{name} response has no code field")
    records = [] if frame.empty else frame.where(frame.notna(), None).to_dict("records")
    if len(records) > MAX_POOL_ROWS:
        raise RuntimeError(f"{name} response exceeds the row limit")
    canonical_rows: list[dict[str, str]] = []
    symbols: list[str] = []
    for row in records:
        if not isinstance(row, dict):
            raise RuntimeError(f"{name} row is invalid")
        canonical = {
            _string(key, field=f"{name} field"): _string(
                value, field=f"{name} value"
            )
            for key, value in row.items()
            if str(key) != "序号"
        }
        symbols.append(_string(row.get("代码"), field=f"{name} code"))
        canonical_rows.append(canonical)
    canonical_rows.sort(key=lambda item: _canonical_json(item))
    return (
        symbols,
        len(records),
        hashlib.sha256(_canonical_json(canonical_rows)).hexdigest(),
    )


def _error_type(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, ValueError) and "最近 30" in text:
        return "LIMIT_EVIDENCE_UNAVAILABLE"
    if isinstance(exc, ValueError):
        return "REQUEST"
    return "QUERY"


def _emit_error(exc: Exception) -> int:
    response = {
        "worker_protocol_version": WORKER_PROTOCOL_VERSION,
        "ok": False,
        "error_type": _error_type(exc),
        "message": str(exc).replace("\n", " ")[:500] or type(exc).__name__,
    }
    encoded = _canonical_json(response)
    if len(encoded) > MAX_ERROR_BYTES:
        encoded = _canonical_json(
            {
                "worker_protocol_version": WORKER_PROTOCOL_VERSION,
                "ok": False,
                "error_type": "PROTOCOL",
                "message": "bounded worker error",
            }
        )
    sys.stdout.buffer.write(encoded)
    return 1


def main() -> int:
    diagnostics = io.StringIO()
    try:
        trade_date, query_date = _request()
        with contextlib.redirect_stdout(diagnostics), contextlib.redirect_stderr(diagnostics):
            import akshare as ak

            limit_up = ak.stock_zt_pool_em(date=query_date)
            limit_down = ak.stock_zt_pool_dtgc_em(date=query_date)
        up_symbols, raw_up, up_digest = _pool(limit_up, name="limit_up")
        down_symbols, raw_down, down_digest = _pool(limit_down, name="limit_down")
        response = {
            "worker_protocol_version": WORKER_PROTOCOL_VERSION,
            "akshare_package_version": importlib.metadata.version("akshare"),
            "operation": OPERATION,
            "trade_date": trade_date.isoformat(),
            "limit_up_symbols": up_symbols,
            "limit_down_symbols": down_symbols,
            "raw_limit_up_count": raw_up,
            "raw_limit_down_count": raw_down,
            "limit_up_response_digest": up_digest,
            "limit_down_response_digest": down_digest,
            "fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        }
        encoded = _canonical_json(response)
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise RuntimeError("worker response exceeds the output limit")
        diagnostic = diagnostics.getvalue()[-MAX_DIAGNOSTIC_CHARS:]
        if diagnostic:
            sys.stderr.write(diagnostic)
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception as exc:
        diagnostic = diagnostics.getvalue()[-MAX_DIAGNOSTIC_CHARS:]
        if diagnostic:
            sys.stderr.write(diagnostic)
        return _emit_error(exc)


if __name__ == "__main__":
    raise SystemExit(main())
