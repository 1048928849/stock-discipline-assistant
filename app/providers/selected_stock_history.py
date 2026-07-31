from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.config import Settings
from app.data_hub.contracts import (
    DailyBar,
    DataProvider,
    ObservedRows,
    ProviderMetadata,
    ProviderUnavailableError,
)
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    shanghai_now,
    to_shanghai_aware,
)
from app.providers.selected_stock_history_worker import (
    MAX_OUTPUT_BYTES,
    PROTOCOL_VERSION,
)


class PublicHistoryProtocolError(ProviderUnavailableError):
    pass


class PublicHistoryWorkerRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self, request: dict[str, str]) -> dict[str, Any]:
        root = str(Path(__file__).resolve().parents[2])
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            item for item in (root, environment.get("PYTHONPATH", "")) if item
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "app.providers.selected_stock_history_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        payload = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        try:
            stdout, stderr = process.communicate(
                payload,
                timeout=self.settings.selected_stock_history_worker_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise ProviderUnavailableError("public history worker timed out") from exc
        if len(stdout) > self.settings.selected_stock_history_max_response_bytes:
            raise PublicHistoryProtocolError("public history response exceeds parent limit")
        if process.returncode != 0:
            raise PublicHistoryProtocolError(
                f"public history worker failed: {stderr.decode(errors='replace')[:300]}"
            )
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicHistoryProtocolError("public history stdout is not one JSON object") from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ProviderUnavailableError(
                f"public history unavailable: {str(response.get('error'))[:300]}"
            )
        return response


class SelectedStockPublicHistoryProvider(DataProvider):
    source = "akshare-public-stock-history"

    def __init__(
        self,
        settings: Settings,
        *,
        runner: PublicHistoryWorkerRunner | None = None,
        calendar: TradingCalendar | None = None,
        now_fn=None,
    ) -> None:
        if settings.selected_stock_history_max_response_bytes > MAX_OUTPUT_BYTES:
            raise ValueError("parent response limit exceeds worker output limit")
        self.settings = settings
        self.runner = runner or PublicHistoryWorkerRunner(settings)
        self.calendar = calendar or get_trading_calendar()
        self.now_fn = now_fn or shanghai_now
        self.metadata = ProviderMetadata(
            provider_id="selected-stock-public-history",
            supported_capabilities=("market.daily.qfq",),
            priority=45,
            health_status="READY",
            timeout=settings.selected_stock_history_worker_timeout_seconds,
            retry=0,
            rate_limit="one stock per bounded subprocess",
        )

    def health_check(self, probe: bool = False) -> dict[str, Any]:
        return {
            "status": "READY",
            "message": "Tencent then Sina public history worker",
            "protocol_version": PROTOCOL_VERSION,
            "process_isolation": "hard-timeout-subprocess",
        }

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PublicHistoryProtocolError(f"invalid {field}") from exc
        if not number.is_finite():
            raise PublicHistoryProtocolError(f"invalid {field}")
        return number

    def get_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        response = self.runner.run(
            {"symbol": symbol, "start": start.isoformat(), "end": end.isoformat()}
        )
        expected = {
            "ok",
            "protocol_version",
            "akshare_version",
            "source",
            "source_function",
            "adjustment",
            "price_unit",
            "volume_unit",
            "fetched_at",
            "rows",
            "row_count",
            "attempts",
        }
        if set(response) != expected or response["protocol_version"] != PROTOCOL_VERSION:
            raise PublicHistoryProtocolError("public history response schema changed")
        if response["adjustment"] != "qfq" or response["price_unit"] != "CNY":
            raise PublicHistoryProtocolError("public history dimensions are invalid")
        if response["volume_unit"] != "share":
            raise PublicHistoryProtocolError("public history volume unit is invalid")
        if response["row_count"] != len(response["rows"]):
            raise PublicHistoryProtocolError("public history row count mismatch")
        if not 1 <= len(response["rows"]) <= self.settings.selected_stock_history_max_rows:
            raise ProviderUnavailableError("public history row count is outside configured bounds")
        fetched_at = to_shanghai_aware(datetime.fromisoformat(response["fetched_at"]))
        rows = []
        for raw in response["rows"]:
            if set(raw) != {
                "symbol",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "volume",
            } or raw["symbol"] != symbol:
                raise PublicHistoryProtocolError("public history row scope is invalid")
            trade_date = date.fromisoformat(raw["trade_date"])
            values = {
                field: self._decimal(raw[field], field)
                for field in ("open", "high", "low", "close", "volume")
            }
            if trade_date < start or trade_date > end:
                raise PublicHistoryProtocolError("public history date is outside request")
            if min(values[name] for name in ("open", "high", "low", "close")) <= 0:
                raise PublicHistoryProtocolError("public history prices must be positive")
            if values["high"] < max(
                values["open"], values["low"], values["close"]
            ) or values["low"] > min(
                values["open"], values["high"], values["close"]
            ):
                raise PublicHistoryProtocolError("public history OHLC is invalid")
            rows.append(
                DailyBar(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=values["open"],
                    high=values["high"],
                    low=values["low"],
                    close=values["close"],
                    volume=values["volume"],
                    adjustment="qfq",
                    price_unit="CNY",
                    volume_unit="share",
                    observed_at=self.calendar.session_close_at(trade_date),
                    source=response["source"],
                    fetched_at=fetched_at,
                )
            )
        if [item.trade_date for item in rows] != sorted(
            {item.trade_date for item in rows}
        ):
            raise PublicHistoryProtocolError("public history dates are invalid")
        request_digest = hashlib.sha256(
            json.dumps(
                {"symbol": symbol, "start": start.isoformat(), "end": end.isoformat()},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        response_digest = hashlib.sha256(
            json.dumps(response["rows"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ObservedRows(
            rows,
            observed_at=rows[-1].observed_at,
            fetched_at=fetched_at,
            provider_lineage={
                "adapter_version": self.settings.selected_stock_history_adapter_version,
                "protocol_version": PROTOCOL_VERSION,
                "source_service": response["source"],
                "requested_start": start.isoformat(),
                "requested_end": end.isoformat(),
                "requested_adjustment": "qfq",
                "row_count": len(rows),
                "request_digest": request_digest,
                "response_digest": response_digest,
                "actual_fields": sorted(response["rows"][0]),
                "source_function": response["source_function"],
                "akshare_version": response["akshare_version"],
                "process_isolation": "hard-timeout-subprocess",
                "timeout_seconds": self.settings.selected_stock_history_worker_timeout_seconds,
            },
        )


__all__ = [
    "PublicHistoryProtocolError",
    "PublicHistoryWorkerRunner",
    "SelectedStockPublicHistoryProvider",
]
