from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return {"$decimal": format(Decimal(str(value)).normalize(), "f")}
    if isinstance(value, Decimal):
        return {"$decimal": format(value.normalize(), "f")}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        normalized = {str(key): _canonical_value(child) for key, child in value.items()}
        return {key: normalized[key] for key in sorted(normalized)}
    if hasattr(value, "model_dump"):
        return _canonical_value(value.model_dump(mode="python"))
    raise TypeError(f"unsupported canonical hash value: {type(value).__name__}")


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["canonical_hash"]
