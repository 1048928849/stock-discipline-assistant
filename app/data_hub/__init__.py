"""Data Hub package.

Import concrete contracts from ``app.data_hub.contracts`` and runtime routing
from the application composition root. Keeping this module empty prevents a
type-only import from loading ORM and provider adapters.
"""

__all__: list[str] = []
