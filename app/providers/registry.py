from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from app.providers.base import DataProvider


class ProviderRegistry:
    """按能力注册并排序数据源；未配置或禁用的数据源会被安全跳过。"""

    def __init__(self) -> None:
        self._providers: dict[str, DataProvider] = {}
        self._capabilities: dict[str, list[str]] = defaultdict(list)

    def register(self, provider: DataProvider) -> None:
        self._providers[provider.provider_id] = provider
        for capability in provider.metadata.supported_capabilities:
            ids = self._capabilities[capability]
            if provider.provider_id not in ids:
                ids.append(provider.provider_id)
            ids.sort(key=lambda item: self._providers[item].metadata.priority)

    def providers_for(self, capability: str) -> list[DataProvider]:
        return [self._providers[item] for item in self._capabilities.get(capability, [])]

    def all(self) -> Iterable[DataProvider]:
        return sorted(self._providers.values(), key=lambda item: item.metadata.priority)

    def public_status(self, probe: bool = False) -> list[dict]:
        rows = []
        for provider in self.all():
            health = provider.health_check(probe=probe)
            rows.append(
                {
                    **provider.metadata.public_dict(),
                    **provider.credential_status(),
                    "health_status": health.get("status", provider.metadata.health_status),
                    "health_message": health.get("message"),
                }
            )
        return rows
