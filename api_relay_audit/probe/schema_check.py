"""ProbeReport JSON Schema validator (PRD §6.2 A3).

The validator deliberately implements the small subset of JSON Schema
needed by ``references/schema.json`` (type / required / enum / const /
$ref to ``#/$defs/...`` / ``anyOf``). Pulling in ``jsonschema`` would
violate the standalone audit.py "zero external dep" property; the
modular distribution can drop in ``jsonschema.validate`` in a follow-up
if ergonomics require it, but the contract surface and error messages
are stable here.

Usage::

    from api_relay_audit.probe.schema_check import validate, SchemaValidationError

    validate(payload)  # raises SchemaValidationError on mismatch

CLI::

    python -m api_relay_audit.probe.schema_check path/to/probe.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


_SCHEMA_PATH = Path(__file__).resolve().parent / "references" / "schema.json"


class SchemaValidationError(ValueError):
    """Raised when a payload does not conform to the ProbeReport schema."""


def _load_schema() -> dict:
    with _SCHEMA_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_ref(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise SchemaValidationError(f"unsupported $ref form: {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise SchemaValidationError(f"$ref target not found: {ref!r}")
        node = node[part]
    return node


_PY_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _check(node: dict, schema: dict, path: str, root: dict) -> None:
    if "$ref" in schema:
        schema = _resolve_ref(schema["$ref"], root)

    if "anyOf" in schema:
        errors = []
        for sub in schema["anyOf"]:
            try:
                _check(node, sub, path, root)
                return
            except SchemaValidationError as e:
                errors.append(str(e))
        raise SchemaValidationError(
            f"at {path or '<root>'}: anyOf branches all failed: {errors!r}"
        )

    expected_type = schema.get("type")
    if expected_type is not None:
        py_type = _PY_TYPE_MAP.get(expected_type)
        if py_type is None:
            raise SchemaValidationError(
                f"unsupported schema type {expected_type!r} at {path or '<root>'}"
            )
        # JSON's "boolean" is distinct from int even though Python bool is int subclass.
        if expected_type == "integer" and isinstance(node, bool):
            raise SchemaValidationError(
                f"at {path or '<root>'}: expected integer, got bool"
            )
        if not isinstance(node, py_type):
            raise SchemaValidationError(
                f"at {path or '<root>'}: expected type {expected_type!r}, "
                f"got {type(node).__name__}"
            )

    if "const" in schema:
        if node != schema["const"]:
            raise SchemaValidationError(
                f"at {path or '<root>'} (schema_version pin): "
                f"expected const {schema['const']!r}, got {node!r}; "
                "ProbeReport schema_version is frozen at '1.0'"
            )

    if "enum" in schema:
        if node not in schema["enum"]:
            raise SchemaValidationError(
                f"at {path or '<root>'}: value {node!r} not in enum {schema['enum']!r}"
            )

    if expected_type == "object" or (expected_type is None and isinstance(node, dict)):
        if not isinstance(node, dict):
            return
        for required_key in schema.get("required", []):
            if required_key not in node:
                raise SchemaValidationError(
                    f"at {path or '<root>'}: missing required key {required_key!r}"
                )
        for key, sub_schema in schema.get("properties", {}).items():
            if key in node:
                _check(node[key], sub_schema, f"{path}.{key}" if path else key, root)

    if "minimum" in schema and isinstance(node, (int, float)) and not isinstance(node, bool):
        if node < schema["minimum"]:
            raise SchemaValidationError(
                f"at {path or '<root>'}: {node!r} < minimum {schema['minimum']!r}"
            )


def validate(payload: Any) -> None:
    """Validate ``payload`` against the ProbeReport schema.

    Raises :class:`SchemaValidationError` with a path-qualified message
    on the first violation. Successful validation is silent.
    """
    if not isinstance(payload, dict):
        raise SchemaValidationError(
            f"ProbeReport payload must be a JSON object, got {type(payload).__name__}"
        )
    schema = _load_schema()
    _check(payload, schema, "", schema)


def _main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(
            "usage: python -m api_relay_audit.probe.schema_check <probe.json>",
            file=sys.stderr,
        )
        return 64
    target = Path(argv[0])
    try:
        with target.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"could not load {target}: {e}", file=sys.stderr)
        return 1
    try:
        validate(payload)
    except SchemaValidationError as e:
        print(f"schema validation failed: {e}", file=sys.stderr)
        return 2
    print(f"OK: {target} conforms to ProbeReport schema_version='1.0'")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
