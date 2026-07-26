from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date, datetime, time as datetime_time
from decimal import Decimal, InvalidOperation
import threading
import time
from typing import Any, Callable

from app.config import Settings
from app.data_hub.contracts import (
    DataProvider,
    IndustryCapitalFlow,
    MarketPoolEvent,
    ObservedRows,
    ProviderMetadata,
    ProviderUnavailableError,
)
from app.data_hub.trading_calendar import (
    SHANGHAI_TZ,
    TradingCalendar,
    get_trading_calendar,
    shanghai_now,
    to_shanghai_aware,
)


ADAPTER_VERSION = "1.0.0"


def _records(frame: Any) -> list[dict[str, Any]]:
    if isinstance(frame, str) and "<html" in frame.lower():
        raise ProviderUnavailableError("external endpoint returned an HTML error page")
    if isinstance(frame, list) and all(isinstance(item, dict) for item in frame):
        return frame
    if hasattr(frame, "to_dict"):
        rows = frame.to_dict(orient="records")
        if isinstance(rows, list):
            return rows
    raise ProviderUnavailableError("external endpoint returned an unsupported schema")


def _field(row: dict[str, Any], *names: str, required: bool = True):
    for name in names:
        if name in row and row[name] not in (None, "", "-"):
            return row[name]
    if required:
        raise ProviderUnavailableError(f"external endpoint missing fields: {names}")
    return None


def _decimal(value: Any, field: str, *, required: bool = True) -> Decimal | None:
    if value in (None, "", "-"):
        if required:
            raise ProviderUnavailableError(f"external endpoint missing fields: {field}")
        return None
    try:
        result = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProviderUnavailableError(f"external endpoint invalid decimal: {field}") from exc
    if not result.is_finite():
        raise ProviderUnavailableError(f"external endpoint invalid decimal: {field}")
    return result


def _date(value: Any, expected: date) -> date:
    try:
        parsed = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ProviderUnavailableError("external endpoint invalid trade_date") from exc
    if parsed != expected:
        raise ProviderUnavailableError("external endpoint trade_date mismatch")
    return parsed


def _event_time(value: Any, day: date) -> datetime | None:
    if value in (None, "", "-"):
        return None
    text = str(value).strip()
    if text.isdigit():
        text = text.zfill(6)
    try:
        if ":" in text:
            parsed_time = datetime_time.fromisoformat(text)
        else:
            parsed_time = datetime_time(
                int(text[:2]), int(text[2:4]), int(text[4:6])
            )
    except (TypeError, ValueError) as exc:
        raise ProviderUnavailableError("external endpoint invalid event time") from exc
    return datetime.combine(day, parsed_time, tzinfo=SHANGHAI_TZ)


class _AKShareDiscoveryClient:
    @staticmethod
    def _ak():
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderUnavailableError("AKShare is not installed") from exc
        return ak

    @staticmethod
    def _frame_records(frame) -> list[dict[str, Any]]:
        return _records(frame)

    def industry_capital_flow(self, day: date):
        frames = {}
        for period in ("今日", "5日", "10日"):
            frame = self._ak().stock_sector_fund_flow_rank(
                indicator=period,
                sector_type="行业资金流",
            )
            frames[period] = self._frame_records(frame)
        by_name: dict[str, dict[str, Any]] = {}
        for period, rows in frames.items():
            for row in rows:
                name = str(_field(row, "名称", "行业名称", "industry_name")).strip()
                target = by_name.setdefault(
                    name,
                    {
                        "industry_key": str(row.get("行业代码") or row.get("代码") or name),
                        "industry_name": name,
                        "trade_date": day.isoformat(),
                        "amount": row.get("今日成交额") or row.get("成交额"),
                    },
                )
                target[f"net_inflow_{'1d' if period == '今日' else period.lower()}"] = (
                    row.get(f"{period}主力净流入-净额")
                    or row.get("主力净流入-净额")
                    or row.get("主力净流入净额")
                )
        return list(by_name.values())

    def limit_up_pool(self, day: date):
        return self._frame_records(
            self._ak().stock_zt_pool_em(date=day.strftime("%Y%m%d"))
        )

    def broken_limit_pool(self, day: date):
        return self._frame_records(
            self._ak().stock_zt_pool_zbgc_em(date=day.strftime("%Y%m%d"))
        )


class AStockDiscoveryProvider(DataProvider):
    """Thin, disabled-by-default adapter for public A-share discovery endpoints."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        calendar: TradingCalendar | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or _AKShareDiscoveryClient()
        self.calendar = calendar or get_trading_calendar()
        self.now_fn = now_fn or shanghai_now
        self._last_call = 0.0
        self._rate_lock = threading.Lock()
        self._cache: dict[
            tuple[str, date], tuple[float, datetime, list[dict[str, Any]]]
        ] = {}
        self.metadata = ProviderMetadata(
            provider_id=f"astock-discovery-adapter/{ADAPTER_VERSION}",
            supported_capabilities=(
                "market.industry.capital_flow",
                "market.limit_up_pool",
                "market.broken_limit_pool",
            ),
            enabled=settings.astock_data_enabled,
            priority=60,
            timeout=settings.astock_data_timeout_seconds,
            retry=settings.astock_data_max_retries,
            rate_limit=f"{settings.astock_data_min_interval_seconds}s",
        )

    def health_check(self, probe: bool = False) -> dict[str, Any]:
        if not self.metadata.enabled:
            return {"status": "disabled", "message": "candidate discovery source disabled"}
        return {
            "status": "available",
            "message": f"candidate discovery adapter {ADAPTER_VERSION}",
        }

    def _now(self) -> datetime:
        value = self.now_fn()
        return to_shanghai_aware(value, naive_is_shanghai=value.tzinfo is None)

    def _validate_day(self, day: date, now: datetime) -> None:
        if day != self.calendar.latest_completed_session(now):
            raise ProviderUnavailableError(
                "candidate discovery endpoints require latest completed session"
            )

    def _invoke(self, operation: str, day: date):
        key = (operation, day)
        now_monotonic = time.monotonic()
        cached = self._cache.get(key)
        if cached and now_monotonic - cached[0] <= self.settings.astock_data_cache_ttl_seconds:
            return cached[2], cached[1], True
        errors = []
        for attempt in range(self.settings.astock_data_max_retries):
            with self._rate_lock:
                remaining = (
                    self.settings.astock_data_min_interval_seconds
                    - (time.monotonic() - self._last_call)
                )
                if remaining > 0:
                    time.sleep(remaining)
                self._last_call = time.monotonic()
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(getattr(self.client, operation), day)
            try:
                value = future.result(timeout=self.settings.astock_data_timeout_seconds)
                value = _records(value)
                fetched_at = self._now()
                self._cache[key] = (time.monotonic(), fetched_at, value)
                return value, fetched_at, False
            except FutureTimeoutError:
                future.cancel()
                errors.append("timeout")
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            if attempt + 1 < self.settings.astock_data_max_retries:
                time.sleep(float(min(Decimal("0.25") * (attempt + 1), Decimal("1"))))
        raise ProviderUnavailableError(
            f"candidate discovery endpoint failed: {'; '.join(errors)}"
        )

    def get_industry_capital_flow(self, day: date) -> list[IndustryCapitalFlow]:
        requested_at = self._now()
        self._validate_day(day, requested_at)
        observed_at = self.calendar.session_close_at(day)
        raw_rows, fetched_at, cache_used = self._invoke("industry_capital_flow", day)
        rows = []
        for raw in raw_rows:
            rows.append(
                IndustryCapitalFlow(
                    industry_key=str(_field(raw, "industry_key")).strip(),
                    industry_name=str(_field(raw, "industry_name")).strip(),
                    trade_date=_date(_field(raw, "trade_date"), day),
                    net_inflow_1d=_decimal(_field(raw, "net_inflow_1d"), "net_inflow_1d"),
                    net_inflow_5d=_decimal(_field(raw, "net_inflow_5d"), "net_inflow_5d"),
                    net_inflow_10d=_decimal(_field(raw, "net_inflow_10d"), "net_inflow_10d"),
                    amount=_decimal(_field(raw, "amount"), "amount"),
                    amount_unit="CNY",
                    source=f"public-a-share-adapter/{ADAPTER_VERSION}",
                    observed_at=observed_at,
                    fetched_at=fetched_at,
                )
            )
        if not rows:
            raise ProviderUnavailableError("industry capital flow returned no rows")
        return ObservedRows(
            rows,
            observed_at=observed_at,
            fetched_at=fetched_at,
            cache_used=cache_used,
        )

    def _pool(self, day: date, *, operation: str, event_type: str) -> ObservedRows:
        requested_at = self._now()
        self._validate_day(day, requested_at)
        observed_at = self.calendar.session_close_at(day)
        raw_rows, fetched_at, cache_used = self._invoke(operation, day)
        rows = []
        for raw in raw_rows:
            symbol = str(_field(raw, "symbol", "代码", "股票代码")).strip()[-6:].zfill(6)
            if not symbol.isdigit():
                raise ProviderUnavailableError("external endpoint invalid stock symbol")
            rows.append(
                MarketPoolEvent(
                    symbol=symbol,
                    name=str(_field(raw, "name", "名称")).strip(),
                    trade_date=_date(
                        _field(raw, "trade_date", "日期", required=False) or day,
                        day,
                    ),
                    event_type=event_type,
                    first_event_at=_event_time(
                        _field(raw, "first_event_at", "首次封板时间", required=False), day
                    ),
                    last_event_at=_event_time(
                        _field(raw, "last_event_at", "最后封板时间", required=False), day
                    ),
                    sealed_amount=_decimal(
                        _field(raw, "sealed_amount", "封板资金", required=False),
                        "sealed_amount",
                        required=False,
                    ),
                    sealed_amount_unit="CNY",
                    turnover_rate=_decimal(
                        _field(raw, "turnover_rate", "换手率", required=False),
                        "turnover_rate",
                        required=False,
                    ),
                    turnover_rate_unit="percent",
                    consecutive_days=int(
                        _field(raw, "consecutive_days", "连板数", required=False) or 0
                    ),
                    industry_name=(
                        str(value).strip()
                        if (value := _field(raw, "industry_name", "所属行业", required=False))
                        else None
                    ),
                    reason_summary=(
                        str(value).strip()
                        if (value := _field(raw, "reason_summary", "涨停原因", required=False))
                        else None
                    ),
                    source=f"public-a-share-adapter/{ADAPTER_VERSION}",
                    observed_at=observed_at,
                    fetched_at=fetched_at,
                )
            )
        return ObservedRows(
            rows,
            observed_at=observed_at,
            fetched_at=fetched_at,
            cache_used=cache_used,
        )

    def get_limit_up_pool(self, day: date) -> ObservedRows:
        return self._pool(day, operation="limit_up_pool", event_type="LIMIT_UP")

    def get_broken_limit_pool(self, day: date) -> ObservedRows:
        return self._pool(day, operation="broken_limit_pool", event_type="BROKEN_LIMIT")


__all__ = ["AStockDiscoveryProvider"]
