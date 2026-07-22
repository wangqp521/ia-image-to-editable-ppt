from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .error_codes import ToolError


def load_schema_v2(path: Path | str) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError("SPEC_INVALID", str(source), str(exc)) from exc
    if not isinstance(value, dict):
        raise ToolError("SPEC_INVALID", "$", "schema root must be an object")
    if value.get("schema_version") != 2:
        raise ToolError(
            "SPEC_SCHEMA_VERSION_UNSUPPORTED",
            "schema_version",
            "expected schema_version 2",
        )
    return value


def index_elements(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    elements = spec.get("elements")
    if not isinstance(elements, list):
        raise ToolError("MISSING_REQUIRED_FIELD", "elements", "elements must be an array")
    result: dict[str, dict[str, Any]] = {}
    for index, element in enumerate(elements):
        element_id = element.get("element_id") if isinstance(element, dict) else None
        if not isinstance(element_id, str) or not element_id or element_id in result:
            raise ToolError(
                "SPEC_INVALID", f"elements[{index}].element_id", "must be unique"
            )
        result[element_id] = element
    return result
