from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
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


_PROTOCOL_VERSION = "native-root-v0.2.1"
_ROOT_PATH = "/"
_DAILY_TABLE = "日k"
_FACTOR_TABLE = "复权"
_CANARY_SYMBOL = "600519"
_CANARY_DATE = date(2026, 7, 24)
_STOCK_PATTERN = re.compile(r"^\d{6}$")
_PRICE_FIELDS = ("open", "high", "low", "close", "pre_close")
_REQUIRED_DAILY_FIELDS = (
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "turnover",
)


class FreeStockDBConnectionError(ProviderUnavailableError):
    pass


class FreeStockDBProtocolError(ProviderUnavailableError):
    pass


class FreeStockDBDataUnavailableError(ProviderUnavailableError):
    pass


@dataclass(frozen=True)
class FreeStockDBResponse:
    payload: list[Any]
    raw_response_digest: str
    request_digest: str
    source_url: str
    operation: Literal["daily", "factors"]


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


def _request_digest(params: dict[str, str]) -> str:
    encoded = json.dumps(
        params,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FreeStockDBHttpClient:
    """Bounded client for the native free-stockdb root command protocol."""

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

    def _request_json(
        self,
        *,
        operation: Literal["daily", "factors"],
        params: dict[str, str],
    ) -> FreeStockDBResponse:
        last_error: Exception | None = None
        for attempt in range(self.settings.freestockdb_max_retries + 1):
            try:
                with self._client.stream("GET", _ROOT_PATH, params=params) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        target = urlsplit(str(response.url.join(location)))
                        if (
                            not self.settings.freestockdb_allow_remote
                            and not _is_loopback_host(target.hostname)
                        ):
                            raise FreeStockDBProtocolError(
                                "free-stockdb redirect to non-loopback address rejected"
                            )
                        raise FreeStockDBProtocolError(
                            "free-stockdb redirect is not supported"
                        )
                    if 400 <= response.status_code < 500:
                        raise FreeStockDBProtocolError(
                            f"free-stockdb native protocol returned HTTP {response.status_code}"
                        )
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            declared_size = int(declared)
                        except ValueError as exc:
                            raise FreeStockDBProtocolError(
                                "free-stockdb content length is invalid"
                            ) from exc
                        if declared_size > self.settings.freestockdb_max_response_bytes:
                            raise FreeStockDBProtocolError(
                                "free-stockdb response size exceeds limit"
                            )
                    chunks = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.settings.freestockdb_max_response_bytes:
                            raise FreeStockDBProtocolError(
                                "free-stockdb response size exceeds limit"
                            )
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    if content.lstrip().startswith(b"<"):
                        raise FreeStockDBProtocolError(
                            "free-stockdb response is not JSON"
                        )
                    try:
                        payload = json.loads(content)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise FreeStockDBProtocolError(
                            "free-stockdb response is not valid JSON"
                        ) from exc
                    if not isinstance(payload, list):
                        raise FreeStockDBProtocolError(
                            "free-stockdb native protocol requires a list root"
                        )
                    return FreeStockDBResponse(
                        payload=payload,
                        raw_response_digest=hashlib.sha256(content).hexdigest(),
                        request_digest=_request_digest(params),
                        source_url=str(response.url.copy_with(query=None)),
                        operation=operation,
                    )
            except (FreeStockDBProtocolError, FreeStockDBDataUnavailableError):
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
            raise FreeStockDBConnectionError(f"free-stockdb timeout: {detail}")
        raise FreeStockDBConnectionError(f"free-stockdb unreachable: {detail}")

    def daily_history(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> FreeStockDBResponse:
        return self._request_json(
            operation="daily",
            params={
                "cmd": "vals",
                "t": _DAILY_TABLE,
                "k1": f"key:{symbol}",
                "k2": f"fwd:{start:%Y%m%d},{end:%Y%m%d}",
            },
        )

    def adjustment_factors(self, symbol: str) -> FreeStockDBResponse:
        return self._request_json(
            operation="factors",
            params={
                "cmd": "get",
                "t": _FACTOR_TABLE,
                "k1": f"key:{symbol}",
                "k2": "all:",
            },
        )

    def canary(self) -> FreeStockDBResponse:
        return self.daily_history(_CANARY_SYMBOL, _CANARY_DATE, _CANARY_DATE)


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ProviderUnavailableError(f"free-stockdb {field} numeric value is invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProviderUnavailableError(
            f"free-stockdb {field} numeric value is invalid"
        ) from exc
    if not result.is_finite():
        raise ProviderUnavailableError(f"free-stockdb {field} numeric value is invalid")
    return result


def _trade_date(value: Any, field: str = "date") -> date:
    if isinstance(value, bool):
        raise ProviderUnavailableError(f"free-stockdb {field} is invalid")
    text = str(value)
    if not re.fullmatch(r"\d{8}", text):
        raise ProviderUnavailableError(f"free-stockdb {field} is invalid")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise ProviderUnavailableError(f"free-stockdb {field} is invalid") from exc


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

    def _health_base(self) -> dict[str, Any]:
        return {
            "adapter_version": self.settings.freestockdb_adapter_version,
            "supported_capabilities": list(self.metadata.supported_capabilities),
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
        }

    @staticmethod
    def _validate_canary(response: FreeStockDBResponse) -> None:
        if not response.payload:
            raise FreeStockDBDataUnavailableError(
                "free-stockdb native canary data is unavailable"
            )
        row = response.payload[0]
        if (
            not isinstance(row, dict)
            or row.get("code") != _CANARY_SYMBOL
            or str(row.get("date")) != f"{_CANARY_DATE:%Y%m%d}"
            or not set(_REQUIRED_DAILY_FIELDS).issubset(row)
        ):
            raise FreeStockDBProtocolError(
                "free-stockdb native canary response is unsupported"
            )

    def health_check(self, probe: bool = False) -> dict:
        if not self.settings.freestockdb_enabled:
            return {
                "status": "DISABLED",
                "message": "provider disabled",
                **self._health_base(),
            }
        if not probe:
            status = "READY" if self._last_success_at and not self._last_error else "DEGRADED"
            return {"status": status, "message": self._last_error, **self._health_base()}
        try:
            response = self.client.canary()
            self._validate_canary(response)
            self._last_success_at = self._now()
            self._last_error = None
            return {
                "status": "READY",
                "message": "native root protocol ready",
                **self._health_base(),
            }
        except FreeStockDBDataUnavailableError as exc:
            self._last_error = str(exc)[:300]
            return {
                "status": "DATA_UNAVAILABLE",
                "message": self._last_error,
                **self._health_base(),
            }
        except FreeStockDBProtocolError as exc:
            self._last_error = str(exc)[:300]
            return {
                "status": "PROTOCOL_UNSUPPORTED",
                "message": self._last_error,
                **self._health_base(),
            }
        except FreeStockDBConnectionError as exc:
            self._last_error = str(exc)[:300]
            return {
                "status": "DEGRADED" if self._last_success_at else "UNREACHABLE",
                "message": self._last_error,
                **self._health_base(),
            }

    @staticmethod
    def _stock_symbol(symbol: str) -> str:
        if not _STOCK_PATTERN.fullmatch(symbol):
            raise ProviderUnavailableError("free-stockdb stock symbol must be six digits")
        return symbol

    def _daily_rows(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> tuple[list[dict[str, Any]], FreeStockDBResponse, list[str]]:
        if start > end:
            raise ProviderUnavailableError("free-stockdb history range is invalid")
        response = self.client.daily_history(symbol, start, end)
        if not response.payload:
            raise FreeStockDBDataUnavailableError(
                "free-stockdb history result is empty"
            )
        if len(response.payload) < self.settings.freestockdb_history_lookback_sessions:
            raise FreeStockDBDataUnavailableError(
                "free-stockdb history rows are insufficient"
            )
        rows: list[dict[str, Any]] = []
        raw_dates: list[date] = []
        actual_fields: set[str] = set()
        for raw in response.payload:
            if not isinstance(raw, dict):
                raise FreeStockDBProtocolError(
                    "free-stockdb daily row protocol is invalid"
                )
            actual_fields.update(str(field) for field in raw)
            missing = set(_REQUIRED_DAILY_FIELDS) - set(raw)
            if missing:
                field = sorted(missing)[0]
                raise ProviderUnavailableError(f"free-stockdb {field} is missing")
            if str(raw["code"]) != symbol:
                raise ProviderUnavailableError("free-stockdb response scope mismatch")
            trade_date = _trade_date(raw["date"])
            if not self.calendar.is_session(trade_date):
                raise ProviderUnavailableError(
                    "free-stockdb date is not a trading session"
                )
            prices = {field: _decimal(raw[field], field) for field in _PRICE_FIELDS}
            volume = _decimal(raw["volume"], "volume")
            amount = _decimal(raw["amount"], "amount")
            turnover = _decimal(raw["turnover"], "turnover")
            if min(prices.values()) <= 0:
                raise ProviderUnavailableError("free-stockdb prices must be positive")
            if (
                prices["high"]
                < max(prices["open"], prices["close"], prices["low"])
                or prices["low"]
                > min(prices["open"], prices["close"], prices["high"])
            ):
                raise ProviderUnavailableError(
                    "free-stockdb OHLC relationship is invalid"
                )
            if volume < 0 or amount < 0 or turnover < 0:
                raise ProviderUnavailableError(
                    "free-stockdb volume/amount/turnover must be nonnegative"
                )
            raw_dates.append(trade_date)
            rows.append(
                {
                    "trade_date": trade_date,
                    **prices,
                    "volume": volume,
                    "amount": amount,
                    "turnover_rate": turnover,
                }
            )
        if len(raw_dates) != len(set(raw_dates)):
            raise ProviderUnavailableError(
                "free-stockdb trade dates contain duplicates"
            )
        if raw_dates not in (sorted(raw_dates), sorted(raw_dates, reverse=True)):
            raise ProviderUnavailableError(
                "free-stockdb trade dates are not monotonic"
            )
        rows.sort(key=lambda row: row["trade_date"])
        dates = [row["trade_date"] for row in rows]
        if dates[0] < start or dates[-1] != end or any(day > end for day in dates):
            raise ProviderUnavailableError(
                "free-stockdb latest trade date does not match request"
            )
        return rows, response, sorted(actual_fields)

    @staticmethod
    def _factor_rows(
        symbol: str,
        response: FreeStockDBResponse,
    ) -> list[tuple[date, Decimal]]:
        factors: list[tuple[date, Decimal]] = []
        pattern = re.compile(rf"^{re.escape(_FACTOR_TABLE)}:{re.escape(symbol)}:(\d{{8}})$")
        for raw in response.payload:
            if not isinstance(raw, list) or len(raw) != 2 or not isinstance(raw[1], dict):
                raise FreeStockDBProtocolError(
                    "free-stockdb factor protocol is invalid"
                )
            match = pattern.fullmatch(str(raw[0]))
            if match is None:
                raise FreeStockDBProtocolError(
                    "free-stockdb factor scope or date is invalid"
                )
            factor_date = _trade_date(match.group(1), "factor date")
            cumulative = _decimal(raw[1].get("cum"), "factor cum")
            if cumulative <= 0:
                raise ProviderUnavailableError(
                    "free-stockdb factor cum must be positive"
                )
            factors.append((factor_date, cumulative))
        dates = [item[0] for item in factors]
        if len(dates) != len(set(dates)):
            raise ProviderUnavailableError(
                "free-stockdb factor dates contain duplicates"
            )
        if dates and dates not in (sorted(dates), sorted(dates, reverse=True)):
            raise ProviderUnavailableError(
                "free-stockdb factor dates are not monotonic"
            )
        factors.sort(key=lambda item: item[0])
        return factors

    @staticmethod
    def _apply_qfq(
        rows: list[dict[str, Any]],
        factors: list[tuple[date, Decimal]],
    ) -> list[dict[str, Any]]:
        if not factors:
            return rows
        factor_dates = [item[0] for item in factors]
        cumulative = [item[1] for item in factors]
        latest = cumulative[-1]
        adjusted = []
        for row in rows:
            index = bisect_right(factor_dates, row["trade_date"]) - 1
            current = cumulative[index] if index >= 0 else Decimal("1")
            ratio = latest / current
            item = dict(row)
            for field in _PRICE_FIELDS:
                item[field] = (item[field] / ratio).quantize(Decimal("0.01"))
            adjusted.append(item)
        return adjusted

    def _lineage(
        self,
        *,
        daily: FreeStockDBResponse,
        rows: list[dict[str, Any]],
        actual_fields: list[str],
        start: date,
        end: date,
        adjustment: str,
        factors: FreeStockDBResponse | None = None,
    ) -> dict[str, Any]:
        response_digests = [daily.raw_response_digest]
        if factors is not None:
            response_digests.append(factors.raw_response_digest)
        combined = hashlib.sha256("|".join(response_digests).encode()).hexdigest()
        lineage: dict[str, Any] = {
            "adapter_version": self.settings.freestockdb_adapter_version,
            "protocol_version": _PROTOCOL_VERSION,
            "source_service": "free-stockdb",
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "requested_fields": actual_fields,
            "actual_fields": actual_fields,
            "requested_adjustment": adjustment,
            "row_count": len(rows),
            "schema_fingerprint": hashlib.sha256(
                json.dumps(actual_fields, separators=(",", ":")).encode()
            ).hexdigest(),
            "raw_response_digest": combined,
            "raw_daily_request_digest": daily.request_digest,
            "raw_daily_response_digest": daily.raw_response_digest,
            "source_url": daily.source_url,
        }
        if factors is not None:
            lineage.update(
                {
                    "factor_request_digest": factors.request_digest,
                    "factor_response_digest": factors.raw_response_digest,
                }
            )
        return lineage

    def get_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        normalized = self._stock_symbol(symbol)
        rows, daily_response, actual_fields = self._daily_rows(
            normalized, start, end
        )
        factor_response = self.client.adjustment_factors(normalized)
        factors = self._factor_rows(normalized, factor_response)
        rows = self._apply_qfq(rows, factors)
        fetched_at = self._now()
        lineage = self._lineage(
            daily=daily_response,
            factors=factor_response,
            rows=rows,
            actual_fields=actual_fields,
            start=start,
            end=end,
            adjustment="qfq",
        )
        self._last_success_at = fetched_at
        self._last_error = None
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

    def get_turnover_daily(
        self, symbol: str, start: date, end: date
    ) -> list[TurnoverDaily]:
        normalized = self._stock_symbol(symbol)
        rows, daily_response, actual_fields = self._daily_rows(
            normalized, start, end
        )
        fetched_at = self._now()
        lineage = self._lineage(
            daily=daily_response,
            rows=rows,
            actual_fields=actual_fields,
            start=start,
            end=end,
            adjustment="unadjusted-native-fields",
        )
        self._last_success_at = fetched_at
        self._last_error = None
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
        raise ProviderUnavailableError("CSI300_HTTP_CAPABILITY_UNAVAILABLE")

    def get_quote(self, symbol: str):
        raise ProviderUnavailableError("free-stockdb does not provide quotes")

    def get_sector_history(self, industry: str, start: date, end: date) -> dict:
        raise ProviderUnavailableError("free-stockdb does not provide sector history")


__all__ = [
    "FreeStockDBHttpClient",
    "FreeStockDBProvider",
    "FreeStockDBResponse",
]
