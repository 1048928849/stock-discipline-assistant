"""Compatibility exports for callers migrating to the Data Hub."""

from sqlalchemy.orm import Session

from app.data_hub.router import (
    DataHubRouter,
    ProviderResult,
    UnifiedDataService,
    build_provider_registry,
)


def build_data_hub(db: Session) -> DataHubRouter:
    return DataHubRouter(db, registry=build_provider_registry())


__all__ = [
    "DataHubRouter",
    "ProviderResult",
    "UnifiedDataService",
    "build_data_hub",
    "build_provider_registry",
]
