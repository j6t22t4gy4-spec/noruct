"""Shared bounded JSON contract for MCP read and action adapters."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping

from dynamic_firm.runtime.tools import ToolValidationError


_PROPERTY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_SCHEMA_METADATA = {"title", "description", "default", "examples", "$comment"}


class ExternalCapabilityError(ValueError):
    """A safe error projection for an untrusted external capability."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExternalCapabilityError("INVALID_JSON", "External capability data is not valid JSON") from exc


def sanitize_schema(raw: object) -> Mapping[str, Any]:
    """Accept only one small, deterministic JSON-schema subset from MCP."""

    nodes = 0

    def visit(value: object, depth: int, *, root: bool = False) -> dict[str, Any]:
        nonlocal nodes
        nodes += 1
        if nodes > 64 or depth > 5:
            raise ExternalCapabilityError("SCHEMA_LIMIT", "External tool schema exceeds structural limits")
        if not isinstance(value, dict):
            raise ExternalCapabilityError("UNSAFE_SCHEMA", "External tool schema nodes must be objects")
        schema_type = value.get("type")
        if schema_type not in {"object", "array", "string", "integer", "number", "boolean"}:
            raise ExternalCapabilityError("UNSAFE_SCHEMA", "External tool schema uses an unsupported type")
        result: dict[str, Any] = {"type": schema_type}
        allowed = {"type", *_SCHEMA_METADATA}
        if schema_type == "object":
            properties = value.get("properties", {})
            required = value.get("required", [])
            additional = value.get("additionalProperties", False)
            if not isinstance(properties, dict) or len(properties) > 32:
                raise ExternalCapabilityError("SCHEMA_LIMIT", "External object schema exceeds property limits")
            if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
                raise ExternalCapabilityError("UNSAFE_SCHEMA", "External object required list is invalid")
            if additional is not False and additional is not None:
                raise ExternalCapabilityError("UNSAFE_SCHEMA", "External object schema must deny extra properties")
            cleaned: dict[str, Any] = {}
            for name, child in sorted(properties.items()):
                if not isinstance(name, str) or not _PROPERTY_RE.fullmatch(name):
                    raise ExternalCapabilityError("UNSAFE_SCHEMA", "External property name is not a bounded identifier")
                cleaned[name] = visit(child, depth + 1)
            if len(set(required)) != len(required) or not set(required).issubset(cleaned):
                raise ExternalCapabilityError("UNSAFE_SCHEMA", "External required properties are inconsistent")
            result.update(properties=cleaned, required=sorted(required), additionalProperties=False)
            allowed.update({"properties", "required", "additionalProperties"})
        elif schema_type == "array":
            if "items" not in value:
                raise ExternalCapabilityError("UNSAFE_SCHEMA", "External array schema requires items")
            minimum = value.get("minItems", 0)
            maximum = value.get("maxItems", 16)
            if not isinstance(minimum, int) or not isinstance(maximum, int) or not 0 <= minimum <= maximum <= 16:
                raise ExternalCapabilityError("SCHEMA_LIMIT", "External array bounds are invalid")
            result.update(items=visit(value["items"], depth + 1), minItems=minimum, maxItems=maximum)
            allowed.update({"items", "minItems", "maxItems"})
        elif schema_type == "string":
            minimum = value.get("minLength", 0)
            maximum = value.get("maxLength", 4_096)
            if not isinstance(minimum, int) or not isinstance(maximum, int) or not 0 <= minimum <= maximum <= 4_096:
                raise ExternalCapabilityError("SCHEMA_LIMIT", "External string bounds are invalid")
            result.update(minLength=minimum, maxLength=maximum)
            allowed.update({"minLength", "maxLength", "enum"})
        elif schema_type in {"integer", "number"}:
            allowed.update({"minimum", "maximum", "enum"})
            for keyword in ("minimum", "maximum"):
                if keyword in value:
                    item = value[keyword]
                    if not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(item):
                        raise ExternalCapabilityError("UNSAFE_SCHEMA", "External numeric bounds are invalid")
                    result[keyword] = item
        else:
            allowed.add("enum")
        if "enum" in value:
            enum = value["enum"]
            if not isinstance(enum, list) or not 1 <= len(enum) <= 16:
                raise ExternalCapabilityError("SCHEMA_LIMIT", "External enum exceeds limits")
            if any(isinstance(item, (dict, list)) for item in enum):
                raise ExternalCapabilityError("UNSAFE_SCHEMA", "External enum values must be scalar")
            canonical_json(enum)
            result["enum"] = enum
        unsupported = set(value) - allowed
        if unsupported:
            raise ExternalCapabilityError("UNSAFE_SCHEMA", f"External tool schema uses unsupported keyword: {sorted(unsupported)[0]}")
        if root and schema_type != "object":
            raise ExternalCapabilityError("UNSAFE_SCHEMA", "External tool input schema must be an object")
        return result

    cleaned = visit(raw, 0, root=True)
    if len(canonical_json(cleaned)) > 16_384:
        raise ExternalCapabilityError("SCHEMA_LIMIT", "External tool schema exceeds the byte limit")
    return cleaned


def validate_value(value: object, schema: Mapping[str, Any], path: str = "arguments") -> object:
    expected = schema["type"]
    if expected == "object":
        if not isinstance(value, dict):
            raise ToolValidationError(f"{path} must be an object")
        properties = schema["properties"]
        unknown = set(value) - set(properties)
        missing = set(schema["required"]) - set(value)
        if unknown:
            raise ToolValidationError(f"{path} contains an unknown property")
        if missing:
            raise ToolValidationError(f"{path} is missing a required property")
        return {key: validate_value(item, properties[key], f"{path}.{key}") for key, item in value.items()}
    if expected == "array":
        if not isinstance(value, list):
            raise ToolValidationError(f"{path} must be an array")
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise ToolValidationError(f"{path} violates array bounds")
        return [validate_value(item, schema["items"], f"{path}[]") for item in value]
    if expected == "string":
        if not isinstance(value, str):
            raise ToolValidationError(f"{path} must be a string")
        if not schema["minLength"] <= len(value) <= schema["maxLength"]:
            raise ToolValidationError(f"{path} violates string bounds")
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ToolValidationError(f"{path} must be an integer")
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ToolValidationError(f"{path} must be a finite number")
    elif expected == "boolean" and not isinstance(value, bool):
        raise ToolValidationError(f"{path} must be a boolean")
    if "minimum" in schema and value < schema["minimum"]:
        raise ToolValidationError(f"{path} is below the minimum")
    if "maximum" in schema and value > schema["maximum"]:
        raise ToolValidationError(f"{path} is above the maximum")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolValidationError(f"{path} is outside the enum")
    return value
