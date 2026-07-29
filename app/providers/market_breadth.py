from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.data_hub.contracts import (
    DataProvider,
    MarketBreadthDaily,
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
from app.providers.market_breadth_worker import (
    OPERATION,
    WORKER_PROTOCOL_VERSION,
)


PROVIDER_ID = "market-breadth-eod"
ADAPTER_SOURCE = "freestockdb+akshare-limit-pools"
UNIVERSE_DEFINITION_VERSION = "cn-a-code-rules-v1"
AKSHARE_PACKAGE_VERSION = "1.18.72"
_ROOT_PATH = "/"
_DAILY_TABLE = "\u65e5k"
_STDERR_LIMIT_BYTES = 65_536
_SYMBOL = re.compile(r"^\d{6}$")
_TARGET_PREFIXES = (
    "600",
    "601",
    "603",
    "605",
    "688",
    "689",
    "000",
    "001",
    "002",
    "003",
    "300",
    "301",
    "4",
    "8",
    "92",
)
_NON_TARGET_PREFIXES = ("200", "900", "5", "15", "16", "18", "11", "12")


class MarketBreadthError(ProviderUnavailableError):
    pass


class BreadthConnectionError(MarketBreadthError):
    pass


class BreadthProtocolError(MarketBreadthError):
    pass


class BreadthTimeoutError(MarketBreadthError):
    pass


class BreadthLimitEvidenceUnavailableError(MarketBreadthError):
    pass


class BreadthUniverseIncompleteError(MarketBreadthError):
    pass


class BreadthPoolUniverseMismatchError(MarketBreadthError):
    pass


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def _loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class FreeStockDBCrossSectionResponse:
    payload: list[Any]
    request_digest: str
    response_digest: str
    response_bytes: int


class FreeStockDBBreadthClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.base_url = settings.freestockdb_base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or not _loopback(parsed.hostname)
        ):
            raise ValueError("market breadth FreeStockDB URL must be a loopback origin")
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=settings.freestockdb_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def daily_cross_section(self, trade_date: date) -> FreeStockDBCrossSectionResponse:
        params = {
            "cmd": "vals",
            "t": _DAILY_TABLE,
            "k1": "all:",
            "k2": f"key:{trade_date:%Y%m%d}",
        }
        try:
            with self.client.stream("GET", _ROOT_PATH, params=params) as response:
                if response.is_redirect:
                    raise BreadthProtocolError("FreeStockDB redirect is not allowed")
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.settings.freestockdb_max_response_bytes:
                        raise BreadthProtocolError("FreeStockDB response exceeds the size limit")
                    chunks.append(chunk)
        except BreadthProtocolError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            raise BreadthConnectionError("FreeStockDB cross-section request failed") from exc
        content = b"".join(chunks)
        if content.lstrip().startswith(b"<"):
            raise BreadthProtocolError("FreeStockDB cross-section is not JSON")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BreadthProtocolError("FreeStockDB cross-section is invalid JSON") from exc
        if not isinstance(payload, list):
            raise BreadthProtocolError("FreeStockDB cross-section root must be a list")
        if not payload or len(payload) > self.settings.market_breadth_max_cross_section_rows:
            raise BreadthProtocolError("FreeStockDB cross-section row count is invalid")
        return FreeStockDBCrossSectionResponse(
            payload=payload,
            request_digest=_digest(params),
            response_digest=hashlib.sha256(content).hexdigest(),
            response_bytes=len(content),
        )


def _worker_command() -> list[str]:
    return [sys.executable, "-m", "app.providers.market_breadth_worker"]


class MarketBreadthWorkerRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _worker_error(stdout: bytes, stderr: bytes) -> MarketBreadthError:
        diagnostic = stderr[:_STDERR_LIMIT_BYTES].decode("utf-8", errors="replace")
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return BreadthProtocolError(
                f"market breadth worker failed with invalid error response: {diagnostic}"[:500]
            )
        if (
            not isinstance(response, dict)
            or set(response)
            != {"worker_protocol_version", "ok", "error_type", "message"}
            or response.get("worker_protocol_version") != WORKER_PROTOCOL_VERSION
            or response.get("ok") is not False
        ):
            return BreadthProtocolError("market breadth worker invalid error response")
        message = str(response.get("message") or "market breadth worker failed")[:500]
        if response.get("error_type") == "LIMIT_EVIDENCE_UNAVAILABLE":
            return BreadthLimitEvidenceUnavailableError(
                f"BREADTH_LIMIT_EVIDENCE_UNAVAILABLE: {message}"
            )
        return BreadthProtocolError(message)

    def run(
        self,
        trade_date: date,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        payload = _canonical_json(
            {"operation": OPERATION, "trade_date": trade_date.isoformat()}
        )
        timeout = min(
            float(timeout_seconds or self.settings.market_breadth_worker_timeout_seconds),
            float(self.settings.market_breadth_worker_timeout_seconds),
        )
        try:
            process = subprocess.Popen(
                _worker_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as exc:
            raise BreadthConnectionError("market breadth worker could not start") from exc
        try:
            stdout, stderr = process.communicate(input=payload, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, stderr = process.communicate()
            if process.poll() is None:
                process.wait()
            raise BreadthTimeoutError("market breadth worker exceeded hard timeout") from exc
        stdout = bytes(stdout or b"")
        stderr = bytes(stderr or b"")
        if process.poll() is None:
            process.wait()
        if len(stdout) > self.settings.market_breadth_max_response_bytes:
            raise BreadthProtocolError("market breadth worker response exceeds size limit")
        if len(stderr) > _STDERR_LIMIT_BYTES:
            raise BreadthProtocolError("market breadth worker diagnostics exceed size limit")
        if process.returncode != 0:
            raise self._worker_error(stdout, stderr)
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BreadthProtocolError("market breadth worker returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise BreadthProtocolError("market breadth worker root must be an object")
        return response


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _normalize_symbol(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().upper()
    patterns = (
        r"^(\d{1,6})$",
        r"^(?:SH|SZ|BJ)\.?(\d{6})$",
        r"^(\d{6})\.(?:SH|SS|SZ|BJ)$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return match.group(1).zfill(6)
    return None


def _instrument(symbol: str) -> str:
    if symbol.startswith(_TARGET_PREFIXES):
        return "TARGET"
    if symbol.startswith(_NON_TARGET_PREFIXES):
        return "NON_TARGET"
    return "UNKNOWN"


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


class MarketBreadthEODProvider(DataProvider):
    source = ADAPTER_SOURCE

    def __init__(
        self,
        settings: Settings,
        *,
        client: FreeStockDBBreadthClient | None = None,
        runner: MarketBreadthWorkerRunner | None = None,
        calendar: TradingCalendar | None = None,
        now_fn=None,
        fetch_now_fn=None,
    ) -> None:
        self.settings = settings
        self.client = client or (
            FreeStockDBBreadthClient(settings)
            if settings.market_breadth_enabled
            else None
        )
        self.runner = runner or MarketBreadthWorkerRunner(settings)
        self.calendar = calendar or get_trading_calendar()
        self.now_fn = now_fn or shanghai_now
        self.fetch_now_fn = fetch_now_fn or shanghai_now
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self.metadata = ProviderMetadata(
            provider_id=PROVIDER_ID,
            supported_capabilities=("market.breadth.daily",),
            enabled=settings.market_breadth_enabled,
            priority=25,
            health_status=(
                "DISABLED" if not settings.market_breadth_enabled else "DEGRADED"
            ),
            timeout=settings.market_breadth_total_budget_seconds,
            retry=0,
            rate_limit="one local cross-section plus two fixed AKShare pool calls",
        )

    def _now(self) -> datetime:
        value = self.now_fn()
        return to_shanghai_aware(value, naive_is_shanghai=value.tzinfo is None)

    def _fetch_now(self) -> datetime:
        value = self.fetch_now_fn()
        return to_shanghai_aware(value, naive_is_shanghai=value.tzinfo is None)

    def health_check(self, probe: bool = False) -> dict[str, Any]:
        del probe
        if not self.settings.market_breadth_enabled:
            return {"status": "DISABLED", "message": "provider disabled"}
        return {
            "status": "READY" if self._last_success_at and not self._last_error else "DEGRADED",
            "message": self._last_error,
            "adapter_version": self.settings.market_breadth_adapter_version,
            "last_success_at": self._last_success_at,
        }

    def _validate_day(self, trade_date: date, now: datetime) -> None:
        latest = self.calendar.latest_completed_session(now)
        is_session = getattr(self.calendar, "is_session", lambda day: day == latest)
        if not is_session(trade_date) or trade_date > latest:
            raise BreadthProtocolError(
                "market breadth requires a completed trading session"
            )
        if (now.date() - trade_date).days > self.settings.market_breadth_history_window_days:
            raise BreadthLimitEvidenceUnavailableError(
                "BREADTH_LIMIT_EVIDENCE_UNAVAILABLE: trade date exceeds pool history window"
            )

    def _cross_section(
        self,
        response: FreeStockDBCrossSectionResponse,
        trade_date: date,
    ) -> tuple[dict[str, tuple[Decimal, Decimal]], dict[str, str], Counter[str]]:
        seen: set[str] = set()
        valid: dict[str, tuple[Decimal, Decimal]] = {}
        target_presence: dict[str, str] = {}
        excluded: Counter[str] = Counter()
        for raw in response.payload:
            if not isinstance(raw, dict):
                raise BreadthProtocolError("cross-section row must be an object")
            symbol = _normalize_symbol(raw.get("code"))
            if symbol is None:
                raise BreadthProtocolError("cross-section symbol normalization failed")
            if symbol in seen:
                raise BreadthProtocolError("duplicate cross-section symbol")
            seen.add(symbol)
            raw_date = str(raw.get("date") or "")
            if raw_date != trade_date.strftime("%Y%m%d"):
                raise BreadthProtocolError("cross-section trade date does not match request")
            instrument = _instrument(symbol)
            if instrument != "TARGET":
                excluded[
                    "non_target_instrument"
                    if instrument == "NON_TARGET"
                    else "unknown_instrument"
                ] += 1
                continue
            close = _decimal(raw.get("close"))
            if close is None:
                target_presence[symbol] = "invalid_close"
                excluded["invalid_close"] += 1
                continue
            previous = _decimal(raw.get("pre_close"))
            if previous is None:
                target_presence[symbol] = "invalid_pre_close"
                excluded["invalid_pre_close"] += 1
                continue
            if _decimal(raw.get("volume")) is None:
                target_presence[symbol] = "no_valid_volume"
                excluded["no_valid_volume"] += 1
                continue
            valid[symbol] = (close, previous)
            target_presence[symbol] = "valid"
        if not valid:
            raise BreadthUniverseIncompleteError(
                "BREADTH_UNIVERSE_INCOMPLETE: no valid A-share rows"
            )
        return valid, target_presence, excluded

    def _validate_worker_response(self, response: dict[str, Any], trade_date: date) -> None:
        expected = {
            "worker_protocol_version",
            "akshare_package_version",
            "operation",
            "trade_date",
            "limit_up_symbols",
            "limit_down_symbols",
            "raw_limit_up_count",
            "raw_limit_down_count",
            "limit_up_response_digest",
            "limit_down_response_digest",
            "fetched_at",
        }
        if set(response) != expected:
            raise BreadthProtocolError("market breadth worker response fields are invalid")
        if response["worker_protocol_version"] != WORKER_PROTOCOL_VERSION:
            raise BreadthProtocolError("market breadth worker protocol is unsupported")
        if response["akshare_package_version"] != AKSHARE_PACKAGE_VERSION:
            raise BreadthProtocolError("market breadth AKShare version is unsupported")
        if response["operation"] != OPERATION or response["trade_date"] != trade_date.isoformat():
            raise BreadthProtocolError("market breadth worker scope is invalid")
        for key in ("limit_up_symbols", "limit_down_symbols"):
            if not isinstance(response[key], list):
                raise BreadthProtocolError("market breadth pool symbols are invalid")
        for key, symbols_key in (
            ("raw_limit_up_count", "limit_up_symbols"),
            ("raw_limit_down_count", "limit_down_symbols"),
        ):
            if response[key] != len(response[symbols_key]):
                raise BreadthProtocolError("market breadth pool row count is invalid")
            if response[key] > self.settings.market_breadth_max_pool_rows:
                raise BreadthProtocolError("market breadth pool exceeds configured row limit")
        for key in ("limit_up_response_digest", "limit_down_response_digest"):
            if not isinstance(response[key], str) or not re.fullmatch(r"[0-9a-f]{64}", response[key]):
                raise BreadthProtocolError("market breadth pool digest is invalid")
        try:
            fetched_at = datetime.fromisoformat(response["fetched_at"])
        except (TypeError, ValueError) as exc:
            raise BreadthProtocolError("market breadth worker fetched_at is invalid") from exc
        if fetched_at.tzinfo is None:
            raise BreadthProtocolError("market breadth worker fetched_at must be aware")

    def _pool(
        self,
        raw_symbols: list[Any],
        valid: dict[str, tuple[Decimal, Decimal]],
        target_presence: dict[str, str],
    ) -> tuple[set[str], list[dict[str, str]]]:
        normalized: set[str] = set()
        unmatched: list[dict[str, str]] = []
        failures: set[str] = set()
        for raw in raw_symbols:
            symbol = _normalize_symbol(raw)
            if symbol is None:
                unmatched.append(
                    {
                        "symbol": str(raw)[:32],
                        "classification": "SYMBOL_NORMALIZATION_FAILURE",
                    }
                )
                failures.add("mismatch")
                continue
            normalized.add(symbol)
        for symbol in sorted(normalized - set(valid)):
            instrument = _instrument(symbol)
            if instrument == "NON_TARGET":
                classification = "NON_TARGET_INSTRUMENT"
            elif instrument == "TARGET" or symbol in target_presence:
                classification = "INVALID_OR_MISSING_DAILY_ROW"
                failures.add("incomplete")
            else:
                classification = "UNKNOWN"
                failures.add("mismatch")
            unmatched.append({"symbol": symbol, "classification": classification})
        unmatched.sort(key=lambda item: (item["classification"], item["symbol"]))
        detail = json.dumps(unmatched, ensure_ascii=True, separators=(",", ":"))[:350]
        if "incomplete" in failures:
            raise BreadthUniverseIncompleteError(
                "BREADTH_UNIVERSE_INCOMPLETE: " + detail
            )
        if "mismatch" in failures:
            raise BreadthPoolUniverseMismatchError(
                "BREADTH_POOL_UNIVERSE_MISMATCH: " + detail
            )
        return normalized & set(valid), unmatched

    def get_market_breadth(self, trade_date: date) -> ObservedRows:
        started = time.perf_counter()
        now = self._now()
        self._validate_day(trade_date, now)
        request_started_at = self._fetch_now()
        try:
            if self.client is None:
                self.client = FreeStockDBBreadthClient(self.settings)
            cross = self.client.daily_cross_section(trade_date)
            elapsed = time.perf_counter() - started
            remaining = self.settings.market_breadth_total_budget_seconds - elapsed
            if remaining <= 0:
                raise BreadthTimeoutError("market breadth total budget exhausted")
            worker = self.runner.run(trade_date, timeout_seconds=remaining)
            request_completed_at = self._fetch_now()
            self._validate_worker_response(worker, trade_date)
            worker_fetched_at = to_shanghai_aware(
                datetime.fromisoformat(worker["fetched_at"])
            )
            if not request_started_at <= worker_fetched_at <= request_completed_at:
                raise BreadthProtocolError(
                    "market breadth worker fetched_at is outside request bounds"
                )
            valid, target_presence, excluded = self._cross_section(cross, trade_date)
            matched_up, unmatched_up = self._pool(
                worker["limit_up_symbols"], valid, target_presence
            )
            matched_down, unmatched_down = self._pool(
                worker["limit_down_symbols"], valid, target_presence
            )
            changes = [
                (close / previous - Decimal("1")) * Decimal("100")
                for close, previous in valid.values()
            ]
            advancing = sum(close > previous for close, previous in valid.values())
            declining = sum(close < previous for close, previous in valid.values())
            unchanged = sum(close == previous for close, previous in valid.values())
            if advancing + declining + unchanged != len(valid):
                raise BreadthProtocolError("breadth direction counts violate universe invariant")
            fetched_at = request_completed_at
            row = MarketBreadthDaily(
                trade_date=trade_date,
                advancing=advancing,
                declining=declining,
                unchanged=unchanged,
                limit_up=len(matched_up),
                limit_down=len(matched_down),
                new_highs=None,
                new_lows=None,
                median_change_pct=_median(changes).quantize(Decimal("0.000001")),
                above_ma20_ratio=None,
                above_ma50_ratio=None,
                observed_at=self.calendar.session_close_at(trade_date),
                source=ADAPTER_SOURCE,
                fetched_at=fetched_at,
            )
            normalized_business = {
                "trade_date": trade_date.isoformat(),
                "universe": [
                    [symbol, format(close, "f"), format(previous, "f")]
                    for symbol, (close, previous) in sorted(valid.items())
                ],
                "matched_limit_up": sorted(matched_up),
                "matched_limit_down": sorted(matched_down),
            }
            duration_ms = int((time.perf_counter() - started) * 1000)
            lineage = {
                "adapter_version": self.settings.market_breadth_adapter_version,
                "worker_protocol_version": WORKER_PROTOCOL_VERSION,
                "provider_id": PROVIDER_ID,
                "capability": "market.breadth.daily",
                "market": "CN-A",
                "trade_date": trade_date.isoformat(),
                "observed_at": row.observed_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "universe_definition_version": UNIVERSE_DEFINITION_VERSION,
                "raw_cross_section_rows": len(cross.payload),
                "valid_universe_rows": len(valid),
                "excluded_rows_by_reason": dict(sorted(excluded.items())),
                "advancing": advancing,
                "declining": declining,
                "unchanged": unchanged,
                "median_change_pct": format(row.median_change_pct, "f"),
                "raw_limit_up_count": worker["raw_limit_up_count"],
                "matched_limit_up_count": len(matched_up),
                "unmatched_limit_up_symbols": unmatched_up,
                "raw_limit_down_count": worker["raw_limit_down_count"],
                "matched_limit_down_count": len(matched_down),
                "unmatched_limit_down_symbols": unmatched_down,
                "freestockdb_request_digest": cross.request_digest,
                "freestockdb_response_digest": cross.response_digest,
                "limit_up_response_digest": worker["limit_up_response_digest"],
                "limit_down_response_digest": worker["limit_down_response_digest"],
                "normalized_result_digest": _digest(normalized_business),
                "process_isolation": True,
                "timeout_seconds": self.settings.market_breadth_worker_timeout_seconds,
                "total_duration_ms": duration_ms,
            }
            self._last_success_at = fetched_at
            self._last_error = None
            return ObservedRows(
                [row],
                observed_at=row.observed_at,
                fetched_at=fetched_at,
                provider_lineage=lineage,
            )
        except Exception as exc:
            self._last_error = str(exc)[:300]
            raise


__all__ = [
    "BreadthConnectionError",
    "BreadthLimitEvidenceUnavailableError",
    "BreadthPoolUniverseMismatchError",
    "BreadthProtocolError",
    "BreadthTimeoutError",
    "BreadthUniverseIncompleteError",
    "FreeStockDBBreadthClient",
    "FreeStockDBCrossSectionResponse",
    "MarketBreadthEODProvider",
    "MarketBreadthWorkerRunner",
]
