from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.data_hub.contracts import (
    DailyBar,
    MarketDataProvider,
    ObservedRows,
    ProviderMetadata,
    ProviderUnavailableError,
    TurnoverDaily,
)
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    shanghai_now,
    to_shanghai_aware,
)


_ADAPTER_SCHEMA_VERSION = "1.0"
_HISTORY_PATH = "/api/v1/history/daily"
_HEALTH_PATH = "/health"
_STOCK_PATTERN = re.compile(r"^\d{6}$")
_REQUEST_FIELDS = (
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover_rate",
)


@dataclass(frozen=True)
class FreeStockDBResponse:
    payload: dict
    raw_response_digest: str
    schema_version: str
    source_url: str


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


class FreeStockDBHttpClient:
    """Fixed, bounded adapter for the versioned fixture HTTP contract."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.base_url = settings.freestockdb_base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("freestockdb base URL must be a plain HTTP(S) origin")
        if not settings.freestockdb_allow_remote and not _is_loopback_host(parsed.hostname):
            raise ValueError("freestockdb base URL must use a loopback host")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=settings.freestockdb_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def _request_json(self, path: Literal["/health", "/api/v1/history/daily"], params=None):
        last_error: Exception | None = None
        for attempt in range(self.settings.freestockdb_max_retries + 1):
            try:
                with self._client.stream("GET", path, params=params) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        target = urlsplit(str(response.url.join(location)))
                        if (
                            not self.settings.freestockdb_allow_remote
                            and not _is_loopback_host(target.hostname)
                        ):
                            raise ProviderUnavailableError(
                                "free-stockdb redirect to non-loopback address rejected"
                            )
                        raise ProviderUnavailableError(
                            "free-stockdb redirect is not supported"
                        )
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if (
                        declared
                        and int(declared)
                        > self.settings.freestockdb_max_response_bytes
                    ):
                        raise ProviderUnavailableError(
                            "free-stockdb response size exceeds limit"
                        )
                    chunks = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.settings.freestockdb_max_response_bytes:
                            raise ProviderUnavailableError(
                                "free-stockdb response size exceeds limit"
                            )
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    if content.lstrip().startswith(b"<"):
                        raise ProviderUnavailableError(
                            "free-stockdb response is not JSON"
                        )
                    try:
                        payload = json.loads(content)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ProviderUnavailableError(
                            "free-stockdb response is not valid JSON"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise ProviderUnavailableError(
                            "free-stockdb JSON schema requires an object"
                        )
                    return (
                        payload,
                        hashlib.sha256(content).hexdigest(),
                        str(response.url.copy_with(query=None)),
                    )
            except ProviderUnavailableError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt < self.settings.freestockdb_max_retries:
                    time.sleep(min(0.1 * (attempt + 1), 0.3))
                    continue
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code >= 500 and attempt < self.settings.freestockdb_max_retries:
                    time.sleep(min(0.1 * (attempt + 1), 0.3))
                    continue
            break
        detail = str(last_error or "request failed").replace("\n", " ")[:240]
        if isinstance(last_error, httpx.TimeoutException):
            raise ProviderUnavailableError(f"free-stockdb timeout: {detail}")
        raise ProviderUnavailableError(f"free-stockdb unavailable: {detail}")

    def health(self) -> dict:
        payload, _, _ = self._request_json(_HEALTH_PATH)
        if payload.get("schema_version") != _ADAPTER_SCHEMA_VERSION:
            raise ProviderUnavailableError("free-stockdb health schema unsupported")
        if payload.get("status") != "ok" or payload.get("service") != "free-stockdb":
            raise ProviderUnavailableError("free-stockdb health response is invalid")
        return payload

    def daily_history(
        self,
        *,
        kind: Literal["stock", "index"],
        symbol: str,
        start: date,
        end: date,
        adjustment: Literal["qfq", "unadjusted"],
    ) -> FreeStockDBResponse:
        payload, raw_digest, source_url = self._request_json(
            _HISTORY_PATH,
            params={
                "kind": kind,
                "symbol": symbol,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjustment": adjustment,
                "fields": ",".join(_REQUEST_FIELDS),
            },
        )
        if payload.get("schema_version") != _ADAPTER_SCHEMA_VERSION:
            raise ProviderUnavailableError("free-stockdb response schema unsupported")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            raise ProviderUnavailableError("free-stockdb response schema is invalid")
        return FreeStockDBResponse(
            payload=payload,
            raw_response_digest=raw_digest,
            schema_version=_ADAPTER_SCHEMA_VERSION,
            source_url=source_url,
        )


def _decimal(value, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ProviderUnavailableError(f"free-stockdb {field} numeric value is invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProviderUnavailableError(
            f"free-stockdb {field} numeric value is invalid"
        ) from exc
    if not result.is_finite():
        raise ProviderUnavailableError(f"free-stockdb {field} numeric value is invalid")
    return result


class FreeStockDBProvider(MarketDataProvider):
    source = "free-stockdb"

    def __init__(
        self,
        settings: Settings,
        *,
        client: FreeStockDBHttpClient | None = None,
        calendar: TradingCalendar | None = None,
        now_fn=None,
    ) -> None:
        self.settings = settings
        self.client = client or FreeStockDBHttpClient(settings)
        self.calendar = calendar or get_trading_calendar()
        self.now_fn = now_fn or shanghai_now
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self.metadata = ProviderMetadata(
            provider_id="freestockdb",
            supported_capabilities=(
                "market.index_daily",
                "market.daily.qfq",
                "market.turnover.daily",
            ),
            enabled=settings.freestockdb_enabled,
            priority=40,
            health_status="DISABLED" if not settings.freestockdb_enabled else "DEGRADED",
            timeout=settings.freestockdb_timeout_seconds,
            retry=settings.freestockdb_max_retries,
            rate_limit=f"fixed batch <= {settings.freestockdb_batch_size}",
        )

    def _now(self) -> datetime:
        value = self.now_fn()
        return to_shanghai_aware(value, naive_is_shanghai=value.tzinfo is None)

    def health_check(self, probe: bool = False) -> dict:
        base = {
            "adapter_version": self.settings.freestockdb_adapter_version,
            "supported_capabilities": list(self.metadata.supported_capabilities),
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
        }
        if not self.settings.freestockdb_enabled:
            return {"status": "DISABLED", "message": "provider disabled", **base}
        if not probe:
            status = "READY" if self._last_success_at and not self._last_error else "DEGRADED"
            return {"status": status, "message": self._last_error, **base}
        try:
            response = self.client.health()
            self._last_error = None
            return {
                "status": "READY",
                "message": f"free-stockdb {response.get('version', 'unknown')}",
                **base,
            }
        except ProviderUnavailableError as exc:
            self._last_error = str(exc)[:300]
            status = "SCHEMA_UNSUPPORTED" if "schema" in self._last_error else "UNREACHABLE"
            return {"status": status, "message": self._last_error, **base}

    def _response(
        self,
        *,
        kind: Literal["stock", "index"],
        symbol: str,
        external_symbol: str,
        start: date,
        end: date,
        adjustment: Literal["qfq", "unadjusted"],
        require_amount: bool,
        require_turnover: bool,
    ) -> tuple[list[dict], datetime, dict]:
        if start > end:
            raise ProviderUnavailableError("free-stockdb history range is invalid")
        response = self.client.daily_history(
            kind=kind,
            symbol=external_symbol,
            start=start,
            end=end,
            adjustment=adjustment,
        )
        data = response.payload["data"]
        if data.get("symbol") != external_symbol or data.get("adjustment") != adjustment:
            raise ProviderUnavailableError("free-stockdb response scope mismatch")
        if (
            data.get("price_unit") != "CNY"
            or data.get("volume_unit") != "share"
            or data.get("amount_unit") != "CNY"
            or data.get("turnover_rate_unit") != "percent"
        ):
            raise ProviderUnavailableError("free-stockdb response unit mismatch")
        rows = data["rows"]
        if not rows:
            raise ProviderUnavailableError("free-stockdb history result is empty")
        if len(rows) < self.settings.freestockdb_history_lookback_sessions:
            raise ProviderUnavailableError("free-stockdb history rows are insufficient")
        fetched_at = self._now()
        normalized: list[dict] = []
        dates: list[date] = []
        for raw in rows:
            if not isinstance(raw, dict):
                raise ProviderUnavailableError("free-stockdb row schema is invalid")
            try:
                trade_date = date.fromisoformat(str(raw["trade_date"]))
            except (KeyError, ValueError) as exc:
                raise ProviderUnavailableError("free-stockdb trade_date is invalid") from exc
            if not self.calendar.is_session(trade_date):
                raise ProviderUnavailableError("free-stockdb trade_date is not a trading session")
            values = {
                field: _decimal(raw.get(field), field)
                for field in ("open", "high", "low", "close", "volume")
            }
            amount = (
                _decimal(raw.get("amount"), "amount")
                if require_amount or raw.get("amount") is not None
                else None
            )
            turnover_rate = (
                _decimal(raw.get("turnover_rate"), "turnover_rate")
                if require_turnover or raw.get("turnover_rate") is not None
                else None
            )
            if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
                raise ProviderUnavailableError("free-stockdb prices must be positive")
            if (
                values["high"] < max(values["open"], values["close"], values["low"])
                or values["low"] > min(values["open"], values["close"], values["high"])
            ):
                raise ProviderUnavailableError("free-stockdb OHLC relationship is invalid")
            if (
                values["volume"] < 0
                or (amount is not None and amount < 0)
                or (turnover_rate is not None and turnover_rate < 0)
            ):
                raise ProviderUnavailableError("free-stockdb volume/amount/turnover must be nonnegative")
            dates.append(trade_date)
            normalized.append(
                {
                    "trade_date": trade_date,
                    **values,
                    "amount": amount,
                    "turnover_rate": turnover_rate,
                }
            )
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ProviderUnavailableError("free-stockdb trade dates must be strictly increasing")
        if dates[0] < start or dates[-1] != end or any(day > end for day in dates):
            raise ProviderUnavailableError("free-stockdb latest trade date does not match request")
        fingerprint = hashlib.sha256(
            json.dumps(sorted(rows[0]), separators=(",", ":")).encode()
        ).hexdigest()
        lineage = {
            "adapter_version": self.settings.freestockdb_adapter_version,
            "source_service": "free-stockdb",
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "requested_fields": list(_REQUEST_FIELDS),
            "requested_adjustment": adjustment,
            "row_count": len(normalized),
            "response_schema_version": response.schema_version,
            "schema_fingerprint": fingerprint,
            "raw_response_digest": response.raw_response_digest,
            "source_url": response.source_url,
        }
        self._last_success_at = fetched_at
        self._last_error = None
        return normalized, fetched_at, lineage

    @staticmethod
    def _stock_symbol(symbol: str) -> str:
        if not _STOCK_PATTERN.fullmatch(symbol):
            raise ProviderUnavailableError("free-stockdb stock symbol must be six digits")
        return symbol

    def get_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        normalized = self._stock_symbol(symbol)
        rows, fetched_at, lineage = self._response(
            kind="stock",
            symbol=normalized,
            external_symbol=normalized,
            start=start,
            end=end,
            adjustment="qfq",
            require_amount=False,
            require_turnover=False,
        )
        return ObservedRows(
            [
                DailyBar(
                    symbol=normalized,
                    trade_date=row["trade_date"],
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                    adjustment="qfq",
                    price_unit="CNY",
                    volume_unit="share",
                    observed_at=self.calendar.session_close_at(row["trade_date"]),
                    source=self.source,
                    fetched_at=fetched_at,
                )
                for row in rows
            ],
            observed_at=self.calendar.session_close_at(rows[-1]["trade_date"]),
            fetched_at=fetched_at,
            provider_lineage=lineage,
        )

    def get_turnover_daily(self, symbol: str, start: date, end: date) -> list[TurnoverDaily]:
        normalized = self._stock_symbol(symbol)
        rows, fetched_at, lineage = self._response(
            kind="stock",
            symbol=normalized,
            external_symbol=normalized,
            start=start,
            end=end,
            adjustment="qfq",
            require_amount=True,
            require_turnover=True,
        )
        return ObservedRows(
            [
                TurnoverDaily(
                    symbol=normalized,
                    trade_date=row["trade_date"],
                    turnover_rate=row["turnover_rate"],
                    amount=row["amount"],
                    observed_at=self.calendar.session_close_at(row["trade_date"]),
                    source=self.source,
                    fetched_at=fetched_at,
                )
                for row in rows
            ],
            observed_at=self.calendar.session_close_at(rows[-1]["trade_date"]),
            fetched_at=fetched_at,
            provider_lineage=lineage,
        )

    def get_index_history(self, symbol: str, start: date, end: date) -> dict:
        normalized = symbol.strip().upper()
        if normalized not in {"CSI000300", "000300", "000300.SH", "000300.SS"}:
            raise ProviderUnavailableError("free-stockdb only supports CSI300 index history")
        external = self.settings.freestockdb_csi300_symbol.strip()
        if not external:
            raise ProviderUnavailableError("FREESTOCKDB_CSI300_SYMBOL is not configured")
        rows, fetched_at, lineage = self._response(
            kind="index",
            symbol="CSI000300",
            external_symbol=external,
            start=start,
            end=end,
            adjustment="unadjusted",
            require_amount=True,
            require_turnover=False,
        )
        return {
            "rows": [
                {
                    "date": row["trade_date"],
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"],
                    "amount": row["amount"],
                }
                for row in rows
            ],
            "source": self.source,
            "fetched_at": fetched_at,
            "adjustment": "unadjusted",
            "price_unit": "CNY",
            "volume_unit": "share",
            "provider_lineage": lineage,
        }

    def get_quote(self, symbol: str):
        raise ProviderUnavailableError("free-stockdb does not provide quotes")

    def get_sector_history(self, industry: str, start: date, end: date) -> dict:
        raise ProviderUnavailableError("free-stockdb does not provide sector history")


__all__ = ["FreeStockDBHttpClient", "FreeStockDBProvider", "FreeStockDBResponse"]
