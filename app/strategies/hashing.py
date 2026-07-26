from __future__ import annotations

import hashlib
import inspect
import textwrap
from decimal import Decimal
from typing import Any

from app.domain.hashing import canonical_hash

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
