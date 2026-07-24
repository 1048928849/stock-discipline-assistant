"""Compatibility exports for callers migrating to the Data Hub."""

from sqlalchemy.orm import Session

from app.composition.data_hub import (
    build_data_hub as _build_data_hub,
    build_provider_registry,
)
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter, ProviderResult


def build_data_hub(
    db: Session, registry: ProviderRegistry | None = None
) -> DataHubRouter:
    return _build_data_hub(
        db,
        registry=registry or build_provider_registry(),
    )


def UnifiedDataService(
    db: Session, registry: ProviderRegistry | None = None
) -> DataHubRouter:
    """Compatibility constructor delegated to the single composition root."""

    return build_data_hub(db, registry=registry)


__all__ = [
    "DataHubRouter",
    "ProviderResult",
    "UnifiedDataService",
    "build_data_hub",
    "build_provider_registry",
]
