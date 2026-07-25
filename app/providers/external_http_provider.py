from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Callable

import httpx

from app.config import Settings
from app.data_hub.contracts import (
    DailyBar,
    MarketDataProvider,
    NewsProvider,
    ProviderMetadata,
    ProviderUnavailableError,
    Quote,
    validate_daily_bar_contract,
)
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    shanghai_now,
    to_shanghai_aware,
)


class ProfessionalMarketApiProvider(MarketDataProvider):
    """标准HTTP行情契约：启用后调用 /health、/market/quote、/market/history 等端点。"""

    def __init__(
        self,
        settings: Settings,
        *,
        calendar: TradingCalendar | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.calendar = calendar or get_trading_calendar()
        self.now_fn = now_fn or shanghai_now
        self.metadata = ProviderMetadata(
            provider_id="professional_market_api",
            supported_capabilities=(
                "market.quote.realtime",
                "market.daily.qfq",
                "market.index_daily",
                "market.sector_daily",
            ),
            required_credentials=("PROFESSIONAL_MARKET_API_URL", "PROFESSIONAL_MARKET_API_KEY"),
            enabled=settings.professional_market_api_enabled,
            priority=settings.professional_market_api_priority,
            health_status="not_configured",
            realtime_supported=True,
            timeout=settings.provider_timeout_seconds,
            retry=settings.provider_max_retries,
            rate_limit="供应商合同决定",
        )

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.professional_market_api_url
            and self.settings.professional_market_api_key
        )

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.settings.professional_market_api_key}"}

    @staticmethod
    def _aware_timestamp(value, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ProviderUnavailableError(
                f"professional market contract has invalid {field}"
            ) from exc
        if parsed.tzinfo is None:
            raise ProviderUnavailableError(
                f"professional market contract requires timezone-aware {field}"
            )
        return parsed

    def _now(self) -> datetime:
        value = self.now_fn()
        return to_shanghai_aware(
            value,
            naive_is_shanghai=value.tzinfo is None,
        )

    def _get(self, path: str, params: dict | None = None):
        if not self.metadata.enabled:
            raise ProviderUnavailableError("专业行情Provider已禁用")
        if not self.configured:
            raise ProviderUnavailableError("专业行情API凭据不完整，已安全跳过")
        response = httpx.get(
            self.settings.professional_market_api_url.rstrip("/") + path,
            params=params,
            headers=self._headers,
            timeout=self.metadata.timeout,
        )
        response.raise_for_status()
        return response.json()

    def health_check(self, probe: bool = False) -> dict:
        if not self.metadata.enabled:
            return {"status": "disabled", "message": "已禁用"}
        if not self.configured:
            return {"status": "not_configured", "message": "URL或API Key未配置，安全跳过"}
        if not probe:
            return {"status": "configured", "message": "凭据已配置，尚未主动探测"}
        try:
            self._get("/health")
            return {"status": "healthy", "message": "标准HTTP健康检查成功"}
        except Exception as exc:
            return {"status": "unhealthy", "message": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def get_quote(self, symbol: str) -> Quote:
        row = self._get("/market/quote", {"symbol": symbol})
        if not isinstance(row, dict):
            raise ProviderUnavailableError(
                "professional quote contract requires an object"
            )
        required = {"price", "quote_type", "observed_at", "fetched_at", "price_unit"}
        missing = {field for field in required if row.get(field) in (None, "")}
        if missing:
            raise ProviderUnavailableError(
                f"professional quote contract missing fields: {sorted(missing)}"
            )
        if row["quote_type"] != "realtime":
            raise ProviderUnavailableError(
                "professional quote contract requires quote_type=realtime"
            )
        fetched_at = to_shanghai_aware(
            self._aware_timestamp(row["fetched_at"], "fetched_at")
        )
        observed_at = to_shanghai_aware(
            self._aware_timestamp(row["observed_at"], "observed_at")
        )
        try:
            return Quote(
                symbol=str(row.get("symbol") or symbol),
                name=str(row.get("name") or symbol),
                price=Decimal(str(row["price"])),
                quote_type=row["quote_type"],
                observed_at=observed_at,
                price_unit=str(row["price_unit"]),
                source="professional_market_api",
                source_api="/market/quote",
                fetched_at=fetched_at,
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ProviderUnavailableError(
                "professional quote contract has invalid field types"
            ) from exc

    def get_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        body = self._get(
            "/market/history",
            {
                "symbol": symbol,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "period": "daily",
                "adjust": "qfq",
            },
        )
        if isinstance(body, dict):
            rows = body.get("rows", body)
        elif isinstance(body, list):
            rows = body
        else:
            raise ProviderUnavailableError(
                "professional history contract requires an object or rows list"
            )
        if not isinstance(rows, list):
            raise ProviderUnavailableError(
                "professional history contract requires a rows list"
            )
        bars = []
        raw_timezone_semantics: set[tuple[str, str]] = set()
        required = {
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "adjustment",
            "price_unit",
            "volume_unit",
            "fetched_at",
        }
        for row in rows:
            if not isinstance(row, dict):
                raise ProviderUnavailableError(
                    "professional history contract requires object rows"
                )
            missing = {field for field in required if row.get(field) in (None, "")}
            if missing:
                raise ProviderUnavailableError(
                    f"professional history contract missing fields: {sorted(missing)}"
                )
            try:
                trade_date = date.fromisoformat(str(row["date"])[:10])
                raw_fetched_at = self._aware_timestamp(
                    row["fetched_at"], "fetched_at"
                )
                if row.get("observed_at"):
                    raw_observed_at = self._aware_timestamp(
                        row["observed_at"], "observed_at"
                    )
                else:
                    raw_observed_at = self.calendar.session_close_at(trade_date)
                raw_timezone_semantics.add(
                    (str(raw_observed_at.tzinfo), str(raw_fetched_at.tzinfo))
                )
                observed_at = to_shanghai_aware(raw_observed_at)
                fetched_at = to_shanghai_aware(raw_fetched_at)
                bar = DailyBar(
                    symbol=str(row.get("symbol") or symbol),
                    trade_date=trade_date,
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=Decimal(str(row["volume"])),
                    adjustment=str(row["adjustment"]),
                    price_unit=str(row["price_unit"]),
                    volume_unit=str(row["volume_unit"]),
                    observed_at=observed_at,
                    source=str(row.get("source") or "professional_market_api_qfq"),
                    fetched_at=fetched_at,
                )
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ProviderUnavailableError(
                    "professional history contract has invalid field types"
                ) from exc
            bars.append(bar)
        if len(raw_timezone_semantics) != 1:
            raise ProviderUnavailableError(
                "professional history contract has mixed timezone semantics"
            )
        validate_daily_bar_contract(
            bars,
            capability="market.daily.qfq",
            expected_symbol=symbol,
            evaluated_at=self._now(),
            calendar=self.calendar,
        )
        return bars

    def _benchmark(self, path: str, key: str, start: date, end: date) -> dict:
        body = self._get(
            path, {key: key, "start": start.isoformat(), "end": end.isoformat()}
        )
        return {
            "rows": [
                {
                    "date": date.fromisoformat(str(row["date"])[:10]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume") or 0),
                }
                for row in body.get("rows", body)
            ],
            "source": f"professional_market_api{path}",
            "fetched_at": self._now(),
        }

    def get_index_history(self, symbol: str, start: date, end: date) -> dict:
        body = self._get(
            "/market/index-history",
            {"symbol": symbol, "start": start.isoformat(), "end": end.isoformat()},
        )
        return {
            "rows": [
                {
                    "date": date.fromisoformat(str(row["date"])[:10]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume") or 0),
                }
                for row in body.get("rows", body)
            ],
            "source": "professional_market_api/index-history",
            "fetched_at": self._now(),
        }

    def get_sector_history(self, industry: str, start: date, end: date) -> dict:
        body = self._get(
            "/market/sector-history",
            {"industry": industry, "start": start.isoformat(), "end": end.isoformat()},
        )
        return {
            "rows": [
                {
                    "date": date.fromisoformat(str(row["date"])[:10]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume") or 0),
                }
                for row in body.get("rows", body)
            ],
            "source": "professional_market_api/sector-history",
            "fetched_at": self._now(),
        }


class ConfiguredNewsApiProvider(NewsProvider):
    """标准HTTP新闻契约：GET /news?symbol=&start=&end=，返回带来源和发布时间的rows。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.metadata = ProviderMetadata(
            provider_id="configured_news_api",
            supported_capabilities=("news.company",),
            required_credentials=("NEWS_API_URL", "NEWS_API_KEY"),
            enabled=settings.news_api_enabled,
            priority=settings.news_api_priority,
            health_status="not_configured",
            realtime_supported=False,
            timeout=settings.provider_timeout_seconds,
            retry=settings.provider_max_retries,
            rate_limit="供应商合同决定",
        )

    @property
    def configured(self) -> bool:
        return bool(self.settings.news_api_url and self.settings.news_api_key)

    def health_check(self, probe: bool = False) -> dict:
        if not self.metadata.enabled:
            return {"status": "disabled", "message": "已禁用"}
        if not self.configured:
            return {"status": "not_configured", "message": "URL或API Key未配置，安全跳过"}
        if not probe:
            return {"status": "configured", "message": "凭据已配置，尚未主动探测"}
        try:
            response = httpx.get(
                self.settings.news_api_url.rstrip("/") + "/health",
                headers={"Authorization": f"Bearer {self.settings.news_api_key}"},
                timeout=self.metadata.timeout,
            )
            response.raise_for_status()
            return {"status": "healthy", "message": "标准HTTP健康检查成功"}
        except Exception as exc:
            return {"status": "unhealthy", "message": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def company_news(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        if not self.metadata.enabled or not self.configured:
            raise ProviderUnavailableError("新闻API未配置或已禁用，安全跳过")
        response = httpx.get(
            self.settings.news_api_url.rstrip("/") + "/news",
            params={"symbol": symbol, "start": start.isoformat(), "end": end.isoformat()},
            headers={"Authorization": f"Bearer {self.settings.news_api_key}"},
            timeout=self.metadata.timeout,
        )
        response.raise_for_status()
        body = response.json()
        rows = body.get("rows", body)
        if not isinstance(rows, list):
            raise ProviderUnavailableError("新闻API返回结构不符合标准契约")
        return rows
