from __future__ import annotations

import hashlib
import inspect
import json
import textwrap
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


def parameter_hash(parameters: dict[str, Any]) -> str:
    return canonical_hash(parameters)


def implementation_hash(strategy_type: type) -> str:
    try:
        source = inspect.getsource(strategy_type)
    except (OSError, TypeError) as exc:
        raise ValueError("strategy implementation source is not inspectable") from exc
    normalized = "\n".join(
        line.rstrip() for line in textwrap.dedent(source).strip().splitlines()
    )
    identity = f"{strategy_type.__module__}:{strategy_type.__qualname__}\n{normalized}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValueError(f"unsupported parameter schema type: {expected}")


def _validate_value(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = schema.get("type")
    if expected and not _matches_type(value, expected):
        raise ValueError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed enum value")
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} exceeds maximum")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{path} exceeds maxLength")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_value(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"{path} is missing required parameters: {missing}")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ValueError(f"{path} contains additional parameters: {extras}")
        for name, child in value.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                _validate_value(child, child_schema, f"{path}.{name}")


def validate_parameter_schema(
    parameters: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any]:
    if schema.get("type") != "object":
        raise ValueError("parameter_schema root type must be object")
    _validate_value(parameters, schema, "parameters")
    return parameters


__all__ = [
    "canonical_hash",
    "implementation_hash",
    "parameter_hash",
    "validate_parameter_schema",
]
