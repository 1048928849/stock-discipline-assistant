from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.data_hub.contracts import ProviderMetadata, SocialClueProvider
from app.providers.x_provider import TWScrapeProvider


class XSocialClueProvider(SocialClueProvider):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = TWScrapeProvider(settings)
        self.metadata = ProviderMetadata(
            provider_id="x_social",
            supported_capabilities=("social.company_clues",),
            required_credentials=("X_COOKIE",),
            enabled=bool(settings.x_cookie),
            priority=100,
            health_status="configured" if settings.x_cookie else "not_configured",
            realtime_supported=False,
            timeout=30,
            retry=1,
            rate_limit="低频、凭据与平台限制",
        )

    @property
    def configured(self) -> bool:
        return self.client.configured

    def health_check(self, probe: bool = False) -> dict:
        if not self.configured:
            return {"status": "not_configured", "message": "未配置X Cookie，安全跳过"}
        return {
            "status": "configured",
            "message": "凭据已配置；为避免无意外部请求，健康检查不主动抓取",
        }

    def company_social_clues(
        self, symbol: str, company_name: str | None, start: datetime, end: datetime
    ) -> list[dict]:
        queries = [symbol]
        if company_name:
            queries.append(company_name)
        posts = self.search(queries, limit=20)
        return [
            item
            | {
                "information_type": "未经正式确认的社交线索",
                "confidence": "low",
            }
            for item in posts
            if start <= item["published_at"] <= end
        ]

    def search(self, queries: list[str], limit: int = 20) -> list[dict]:
        return [
            {
                "post_id": item.post_id,
                "author": item.author,
                "content": item.content,
                "published_at": item.published_at,
                "metrics": item.metrics,
                "url": item.url,
            }
            for item in self.client.collect(queries, limit=limit)
        ]
