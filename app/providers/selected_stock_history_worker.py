from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo


MAX_FIELD_CHARS = 256
MAX_OUTPUT_BYTES = 4_000_000
MAX_INPUT_BYTES = 4096
MAX_DIAGNOSTIC_CHARS = 2000
MAX_ERROR_BYTES = 4096
PROTOCOL_VERSION = "selected-stock-history-worker-v1"


class WorkerError(RuntimeError):
    pass


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _text(value: Any, field: str) -> str:
    result = str(value).strip()
    if not result or len(result) > MAX_FIELD_CHARS:
        raise WorkerError(f"invalid field: {field}")
    return result


def _request(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"symbol", "start", "end"}:
        raise WorkerError("invalid request fields")
    symbol = _text(value["symbol"], "symbol")
    if len(symbol) != 6 or not symbol.isdigit():
        raise WorkerError("invalid symbol")
    start = date.fromisoformat(_text(value["start"], "start"))
    end = date.fromisoformat(_text(value["end"], "end"))
    if start > end or (end - start).days > 1500:
        raise WorkerError("invalid date range")
    return {"symbol": symbol, "start": start.isoformat(), "end": end.isoformat()}


def _prefixed(symbol: str) -> str:
    if symbol.startswith(("4", "8", "920")):
        return f"bj{symbol}"
    if symbol.startswith(("5", "6", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def _frame_rows(frame, *, source: str, symbol: str) -> list[dict[str, str]]:
    mappings = {
        "tencent": {
            "date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "amount",
        },
        "sina": {
            "date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        },
    }
    columns = mappings[source]
    missing = set(columns.values()) - set(frame.columns)
    if missing:
        raise WorkerError(f"schema changed: {sorted(missing)}")
    rows: list[dict[str, str]] = []
    for _, raw in frame.iterrows():
        row = {
            "symbol": symbol,
            "trade_date": _text(str(raw[columns["date"]])[:10], "trade_date"),
            "open": _text(raw[columns["open"]], "open"),
            "high": _text(raw[columns["high"]], "high"),
            "low": _text(raw[columns["low"]], "low"),
            "close": _text(raw[columns["close"]], "close"),
            "volume": _text(raw[columns["volume"]], "volume"),
        }
        if source == "tencent":
            row["volume"] = _text(
                format(Decimal(row["volume"]) * Decimal("100"), "f"),
                "volume",
            )
        rows.append(row)
    rows.sort(key=lambda item: item["trade_date"])
    if not rows or len(rows) > 1000:
        raise WorkerError("history row count is invalid")
    if [item["trade_date"] for item in rows] != sorted(
        {item["trade_date"] for item in rows}
    ):
        raise WorkerError("history dates must be unique and increasing")
    return rows


def process(request: dict[str, str], ak) -> dict[str, Any]:
    symbol = request["symbol"]
    start = request["start"].replace("-", "")
    end = request["end"].replace("-", "")
    errors = []
    candidates = (
        (
            "tencent",
            "stock_zh_a_hist_tx",
            lambda: ak.stock_zh_a_hist_tx(
                symbol=_prefixed(symbol),
                start_date=start,
                end_date=end,
                adjust="qfq",
            ),
        ),
        (
            "sina",
            "stock_zh_a_daily",
            lambda: ak.stock_zh_a_daily(
                symbol=_prefixed(symbol),
                start_date=start,
                end_date=end,
                adjust="qfq",
            ),
        ),
    )
    for source, function_name, callback in candidates:
        if not hasattr(ak, function_name):
            errors.append(f"{function_name}: unavailable")
            continue
        try:
            rows = _frame_rows(callback(), source=source, symbol=symbol)
            return {
                "ok": True,
                "protocol_version": PROTOCOL_VERSION,
                "akshare_version": _text(getattr(ak, "__version__", "unknown"), "version"),
                "source": f"akshare_{source}_qfq",
                "source_function": function_name,
                "adjustment": "qfq",
                "price_unit": "CNY",
                "volume_unit": "share",
                "fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "rows": rows,
                "row_count": len(rows),
                "attempts": [item.split(":", 1)[0] for item in errors] + [function_name],
            }
        except Exception as exc:
            errors.append(f"{function_name}: {type(exc).__name__}: {str(exc)[:160]}")
    raise WorkerError("all public stock history sources failed: " + "; ".join(errors))


def _error_payload(exc: Exception) -> bytes:
    payload = {
        "ok": False,
        "protocol_version": PROTOCOL_VERSION,
        "error_type": type(exc).__name__,
        "error": str(exc).replace("\n", " ")[:1000],
    }
    encoded = _canonical_json(payload)
    if len(encoded) > MAX_ERROR_BYTES:
        encoded = _canonical_json(
            {
                "ok": False,
                "protocol_version": PROTOCOL_VERSION,
                "error_type": "WorkerError",
                "error": "bounded worker error",
            }
        )
    return encoded


def main() -> int:
    diagnostic = io.StringIO()
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise WorkerError("request exceeds input limit")
        request = _request(json.loads(raw.decode("utf-8")))
        with contextlib.redirect_stdout(diagnostic):
            import akshare as ak

            response = process(request, ak)
        encoded = _canonical_json(response)
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise WorkerError("response exceeds output limit")
    except Exception as exc:
        encoded = _error_payload(exc)
    message = diagnostic.getvalue()[:MAX_DIAGNOSTIC_CHARS]
    if message:
        sys.stderr.write(message)
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
