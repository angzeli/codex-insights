"""Small standard-library validator for the packaged V1 schema subset."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from importlib.resources import files
from typing import Any


class SchemaValidationError(ValueError):
    """Raised when generated ledger data violates a packaged schema."""


def validate_generated_document(value: object, schema_name: str) -> None:
    """Validate generated JSON without adding a runtime JSON Schema dependency."""

    resource = files("codex_insights.daily_ledger.schemas").joinpath(schema_name)
    schema: Any = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise SchemaValidationError(f"Schema is not an object: {schema_name}")
    _validate(value, schema, schema, path="$")


def _validate(
    value: object,
    schema: dict[str, object],
    root: dict[str, object],
    *,
    path: str,
) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        target = _resolve_reference(reference, root)
        _validate(value, target, root, path=path)
        return
    if "const" in schema and value != schema["const"]:
        raise SchemaValidationError(f"{path} does not match const")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise SchemaValidationError(f"{path} is not an allowed value")
    expected = schema.get("type")
    if expected is not None and not _matches_type(value, expected):
        raise SchemaValidationError(f"{path} has the wrong JSON type")
    minimum = schema.get("minimum")
    if isinstance(minimum, int) and isinstance(value, int) and value < minimum:
        raise SchemaValidationError(f"{path} is below the minimum")
    minimum_length = schema.get("minLength")
    if isinstance(minimum_length, int) and isinstance(value, str) and len(value) < minimum_length:
        raise SchemaValidationError(f"{path} is shorter than minLength")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and isinstance(value, str) and re.search(pattern, value) is None:
        raise SchemaValidationError(f"{path} does not match pattern")
    format_name = schema.get("format")
    if isinstance(format_name, str) and isinstance(value, str):
        _validate_format(value, format_name, path)
    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            missing = [key for key in required if isinstance(key, str) and key not in value]
            if missing:
                raise SchemaValidationError(f"{path} is missing: {', '.join(missing)}")
        properties = schema.get("properties")
        property_map = properties if isinstance(properties, dict) else {}
        for key, item in value.items():
            child_schema = property_map.get(key)
            if isinstance(child_schema, dict):
                _validate(item, child_schema, root, path=f"{path}.{key}")
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                raise SchemaValidationError(f"{path}.{key} is not allowed")
            if isinstance(additional, dict):
                _validate(item, additional, root, path=f"{path}.{key}")
    if isinstance(value, list):
        if schema.get("uniqueItems") is True:
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(encoded) != len(set(encoded)):
                raise SchemaValidationError(f"{path} contains duplicate items")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate(item, items, root, path=f"{path}[{index}]")


def _resolve_reference(reference: str, root: dict[str, object]) -> dict[str, object]:
    if not reference.startswith("#/"):
        raise SchemaValidationError(f"Unsupported schema reference: {reference}")
    current: object = root
    for part in reference[2:].split("/"):
        if not isinstance(current, dict) or part not in current:
            raise SchemaValidationError(f"Unresolved schema reference: {reference}")
        current = current[part]
    if not isinstance(current, dict):
        raise SchemaValidationError(f"Schema reference is not an object: {reference}")
    return current


def _matches_type(value: object, expected: object) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    return any(_matches_one_type(value, str(name)) for name in names)


def _matches_one_type(value: object, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    return False


def _validate_format(value: str, format_name: str, path: str) -> None:
    try:
        if format_name == "date":
            date.fromisoformat(value)
        elif format_name == "date-time":
            datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaValidationError(f"{path} is not a valid {format_name}") from exc
