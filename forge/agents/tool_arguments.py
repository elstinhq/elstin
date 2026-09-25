"""Checking a model's tool-call arguments against the tool's own JSON
schema before the tool runs, after repairing the cases whose intent is
unambiguous. forge/agents/loop.py calls both for every tool call.

Why this exists: a tool function receives the model's arguments as Python
keyword arguments, and nothing checked their types. Weaker models and some
gateways send numbers and booleans as strings, and Python reads those
wrongly rather than failing:

- `write_file_tool(overwrite="false")` — the string "false" is truthy, so
  an existing file the model was told to leave alone was overwritten;
- `edit_file_tool(allow_multiple="false")` replaced every occurrence;
- `read_file_tool(limit="200")` failed with "'>' not supported between
  instances of 'str' and 'int'", which tells the model nothing useful.

Repair rules apply only where the schema makes the intent unambiguous —
the same rule set as opencode's tool-input-repair plugin
(github.com/anomalyco/opencode, MIT):

- a numeric string for an `integer`/`number` → the number;
- "true"/"false" (any case) for a `boolean` → the bool;
- a JSON-encoded string for an `array`/`object` → the decoded value;
- `null` or `{}` for an optional, plainly typed property → omitted, so the
  function's own default applies.

A property typed with anyOf/oneOf/no type at all is left untouched.
Whatever is still wrong after repair is reported by `argument_errors`, and
the loop answers the call with that error instead of running the tool.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

# Deep enough for every forge tool (apply_edits_tool's edits[] items are
# depth 2); a bound so a pathological schema cannot recurse forever.
_MAX_DEPTH = 6

_INTEGER_RE = re.compile(r"[+-]?\d+")


def _types(schema: dict[str, Any]) -> list[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list):
        return [t for t in declared if isinstance(t, str)]
    return []


def _is(value: Any, json_type: str) -> bool:
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "object":
        return isinstance(value, dict)
    if json_type == "null":
        return value is None
    return True  # an unknown type keyword constrains nothing here


def _matches(value: Any, types: list[str]) -> bool:
    return not types or any(_is(value, t) for t in types)


def _repair(value: Any, schema: Any, depth: int) -> Any:
    if depth > _MAX_DEPTH or not isinstance(schema, dict):
        return value
    types = _types(schema)
    if not types:
        return value
    if _matches(value, types):
        if isinstance(value, dict) and "object" in types:
            return _repair_object(value, schema, depth)
        if isinstance(value, list) and "array" in types:
            return _repair_array(value, schema, depth)
        return value

    concrete = [t for t in types if t != "null"]
    if len(concrete) != 1:
        return value  # more than one candidate type: not unambiguous
    target = concrete[0]

    if target == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    if not isinstance(value, str):
        return value
    text = value.strip()
    if target == "integer":
        return int(text) if _INTEGER_RE.fullmatch(text) else value
    if target == "number":
        if _INTEGER_RE.fullmatch(text):
            return int(text)
        try:
            number = float(text)
        except ValueError:
            return value
        return number if math.isfinite(number) else value
    if target == "boolean":
        lowered = text.lower()
        return True if lowered == "true" else False if lowered == "false" else value
    if target in ("array", "object"):
        try:
            decoded = json.loads(text)
        except ValueError:
            return value
        if target == "array" and isinstance(decoded, list):
            return _repair_array(decoded, schema, depth)
        if target == "object" and isinstance(decoded, dict):
            return _repair_object(decoded, schema, depth)
    return value


def _is_plain(schema: dict[str, Any]) -> bool:
    """A single concrete type and nothing that could make null or {} a
    legitimate value — only then is a null/{} provably a placeholder."""
    declared = schema.get("type")
    return (
        isinstance(declared, str)
        and declared != "null"
        and not any(key in schema for key in ("anyOf", "oneOf", "allOf", "enum", "const", "$ref", "nullable"))
    )


def _repair_object(value: dict[str, Any], schema: dict[str, Any], depth: int) -> dict[str, Any]:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    repaired: dict[str, Any] = {}
    for key, item in value.items():
        prop = properties.get(key)
        if not isinstance(prop, dict):
            repaired[key] = item
            continue
        placeholder = item is None or (item == {} and prop.get("type") != "object")
        if placeholder and key not in required and _is_plain(prop):
            continue
        repaired[key] = _repair(item, prop, depth + 1)
    return repaired


def _repair_array(value: list[Any], schema: dict[str, Any], depth: int) -> list[Any]:
    items = schema.get("items")
    if not isinstance(items, dict):
        return value
    return [_repair(item, items, depth + 1) for item in value]


def repair_arguments(arguments: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """`arguments` with every unambiguous type slip corrected. Never
    raises and never invents a value; with no usable schema it returns
    `arguments` unchanged."""
    if not isinstance(schema, dict) or not isinstance(arguments, dict):
        return arguments
    return _repair_object(arguments, schema, 0)


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    kind = {bool: "boolean", int: "integer", float: "number", str: "string", list: "array", dict: "object"}.get(type(value), type(value).__name__)
    shown = json.dumps(value, default=str)
    return f"{kind} {shown[:60]}{'…' if len(shown) > 60 else ''}"


def _collect_errors(value: Any, schema: Any, path: str, depth: int, errors: list[str]) -> None:
    if depth > _MAX_DEPTH or not isinstance(schema, dict):
        return
    types = _types(schema)
    if not _matches(value, types):
        errors.append(f"{path} must be {' or '.join(types)}, got {_describe(value)}")
        return
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path} must be one of {json.dumps(enum)}, got {_describe(value)}")
        return
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        prefix = f"{path}." if path else ""
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{prefix}{name} is required")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    allowed = ", ".join(sorted(properties)) or "none"
                    errors.append(f"{prefix}{name} is not an accepted argument (accepted: {allowed})")
        for name, item in value.items():
            if name in properties:
                _collect_errors(item, properties[name], f"{prefix}{name}", depth + 1, errors)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _collect_errors(item, schema["items"], f"{path}[{index}]", depth + 1, errors)


def argument_errors(arguments: dict[str, Any], schema: dict[str, Any] | None) -> list[str]:
    """Every way `arguments` violates `schema` — missing required
    arguments, wrong types, values outside an enum, and (only where the
    schema sets additionalProperties: false) unknown arguments. Empty when
    the call is valid or there is no usable schema."""
    if not isinstance(schema, dict):
        return []
    errors: list[str] = []
    _collect_errors(arguments, schema, "", 0, errors)
    return errors
