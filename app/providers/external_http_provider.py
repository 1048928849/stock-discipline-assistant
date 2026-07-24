from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import httpx

from app.config import Settings
from app.data_hub.contracts import (
    DailyBar,
    MarketDataProvider,
    NewsProvider,
    ProviderMetadata,
    ProviderUnavailableError,
    Quote,
)


class ProfessionalMarketApiProvider(MarketDataProvider):
    """标准HTTP行情契约：启用后调用 /health、/market/quote、/market/history 等端点。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.metadata = ProviderMetadata(
            provider_id="professional_market_api",
            supported_capabilities=(
                "market.quote",
                "market.daily",
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
        return Quote(
            symbol=symbol,
            name=str(row.get("name") or symbol),
            price=Decimal(str(row["price"])),
            source="professional_market_api",
            source_api="/market/quote",
            fetched_at=datetime.fromisoformat(row.get("fetched_at") or datetime.now().isoformat()),
        )

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
        rows = body.get("rows", body)
        return [
            DailyBar(
                symbol=symbol,
                trade_date=date.fromisoformat(str(row["date"])[:10]),
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])),
                source="professional_market_api_qfq",
                fetched_at=datetime.fromisoformat(
                    row.get("fetched_at") or datetime.now().isoformat()
                ),
            )
            for row in rows
        ]

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
            "fetched_at": datetime.now(),
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
            "fetched_at": datetime.now(),
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
            "fetched_at": datetime.now(),
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
