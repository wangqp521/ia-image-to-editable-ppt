"""Narrow schema v2 loading and element-index helpers."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .error_codes import ToolError


class NonStandardJsonNumberError(ValueError):
    """Raised when JSON uses JavaScript-style NaN or Infinity tokens."""


def reject_nonstandard_json_number(token: str) -> None:
    """Reject constants that RFC-compliant JSON does not permit."""
    raise NonStandardJsonNumberError(f"non-standard JSON number: {token}")


def _child_json_path(path: str, key: Any) -> str:
    if isinstance(key, str) and key.isidentifier():
        return key if path == "$" else f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def non_finite_number_paths(value: Any, path: str = "$") -> list[str]:
    """Return JSON paths of all nested non-finite float values.

    Booleans are deliberately not numbers for this schema contract.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return [path]
    if isinstance(value, list):
        paths: list[str] = []
        for index, item in enumerate(value):
            paths.extend(non_finite_number_paths(item, f"{path}[{index}]"))
        return paths
    if isinstance(value, dict):
        paths = []
        for key, item in value.items():
            paths.extend(
                non_finite_number_paths(item, _child_json_path(path, key))
            )
        return paths
    return []


def require_finite_schema_numbers(value: Any) -> None:
    """Raise a stable compiler error for the first non-finite schema number."""
    paths = non_finite_number_paths(value)
    if paths:
        raise ToolError(
            "SPEC_NUMBER_NON_FINITE",
            paths[0],
            "number must be finite",
        )


def load_schema_v2(path: str | Path) -> dict[str, Any]:
    """Load one JSON schema v2 document, rejecting malformed root documents."""
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                parse_constant=reject_nonstandard_json_number,
            )
    except OSError as exc:
        raise ToolError("SCHEMA_READ_FAILED", str(source), str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise ToolError("SCHEMA_JSON_INVALID", str(source), str(exc)) from exc
    except NonStandardJsonNumberError as exc:
        raise ToolError("SCHEMA_JSON_INVALID", str(source), str(exc)) from exc
    if not isinstance(value, dict):
        raise ToolError("SCHEMA_ROOT_INVALID", "$", "schema root must be an object")
    require_finite_schema_numbers(value)
    if value.get("schema_version") != 2:
        raise ToolError(
            "SCHEMA_VERSION_UNSUPPORTED",
            "schema_version",
            "expected schema_version 2",
        )
    return value


def index_elements(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index schema elements by unique, non-empty ``element_id``."""
    elements = spec.get("elements")
    if not isinstance(elements, list):
        raise ToolError("SCHEMA_ELEMENTS_INVALID", "elements", "elements must be an array")
    indexed: dict[str, dict[str, Any]] = {}
    for index, element in enumerate(elements):
        path = f"elements[{index}]"
        if not isinstance(element, dict):
            raise ToolError("SCHEMA_ELEMENT_INVALID", path, "element must be an object")
        element_id = element.get("element_id")
        if not isinstance(element_id, str) or not element_id:
            raise ToolError("SCHEMA_ELEMENT_ID_INVALID", f"{path}.element_id", "element_id must be non-empty")
        if element_id in indexed:
            raise ToolError("SCHEMA_ELEMENT_ID_DUPLICATE", f"{path}.element_id", "element_id must be unique")
        indexed[element_id] = element
    return indexed
