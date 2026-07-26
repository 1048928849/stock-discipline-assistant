"""Deterministic candidate discovery domain."""

from app.discovery.algorithm import discover_candidates
from app.discovery.contracts import DiscoveryConfig, DiscoverySnapshot

__all__ = ["DiscoveryConfig", "DiscoverySnapshot", "discover_candidates"]
