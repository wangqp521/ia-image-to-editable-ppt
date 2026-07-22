#!/usr/bin/env python3
"""Fill only deterministic schema-v2 reconstruction fields."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from lib.atomic_write import atomic_write_json
from lib.error_codes import ToolError
from lib.geometry import map_xywh_to_slide
from lib.hashing import canonical_json_sha256, file_sha256
from lib.schema_io import index_elements, load_schema_v2


def _require_semantics(spec: dict[str, Any]) -> None:
    required = ("canvas", "elements", "regions", "reading_order", "modules")
    for key in required:
        if key not in spec:
            raise ToolError("MISSING_REQUIRED_FIELD", key, "main agent must provide this field")
    element_map = index_elements(spec)
    reading_order = spec["reading_order"]
    if (
        not isinstance(reading_order, list)
        or len(reading_order) != len(set(reading_order))
        or set(reading_order) != set(element_map)
    ):
        raise ToolError(
            "MISSING_REQUIRED_FIELD",
            "reading_order",
            "main agent must provide one explicit order covering every element",
        )
    for index, element in enumerate(spec["elements"]):
        for field in ("element_id", "kind", "source_bbox", "layer", "style", "content"):
            if field not in element:
                raise ToolError(
                    "MISSING_REQUIRED_FIELD",
                    f"elements[{index}].{field}",
                    "main agent must provide semantic fields",
                )


def _set_derived(
    container: dict[str, Any],
    field: str,
    expected: Any,
    path: str,
    changed: list[str],
    unchanged: list[str],
) -> None:
    if field not in container:
        container[field] = expected
        changed.append(path)
        return
    if container[field] != expected:
        raise ToolError(
            "SPEC_DERIVED_FIELD_CONFLICT",
            path,
            f"existing value {container[field]!r} differs from derived value {expected!r}",
        )
    unchanged.append(path)


def _sync_typography(
    spec: dict[str, Any],
    element_map: dict[str, dict[str, Any]],
    changed: list[str],
    unchanged: list[str],
) -> None:
    typography = spec.get("modules", {}).get("typography")
    if not isinstance(typography, dict):
        return
    items = typography.get("items")
    if not isinstance(items, list):
        raise ToolError("SPEC_INVALID", "modules.typography.items", "expected an array")
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("element_id") not in element_map:
            raise ToolError(
                "SPEC_INVALID", f"modules.typography.items[{index}]", "unknown element"
            )
        element = element_map[item["element_id"]]
        text_box = item.get("text_box")
        if not isinstance(text_box, dict):
            raise ToolError(
                "MISSING_REQUIRED_FIELD",
                f"modules.typography.items[{index}].text_box",
                "text box semantics are required",
            )
        for field, value in zip(("x", "y", "w", "h"), element["slide_bbox"]):
            _set_derived(
                text_box,
                field,
                value,
                f"modules.typography.items[{index}].text_box.{field}",
                changed,
                unchanged,
            )


def _sync_icons(
    spec: dict[str, Any],
    element_map: dict[str, dict[str, Any]],
    changed: list[str],
    unchanged: list[str],
) -> None:
    icons_module = spec.get("modules", {}).get("icons")
    if not isinstance(icons_module, dict):
        return
    icons = icons_module.get("icons")
    if not isinstance(icons, list):
        raise ToolError("SPEC_INVALID", "modules.icons.icons", "expected an array")
    for index, icon in enumerate(icons):
        if not isinstance(icon, dict) or icon.get("element_id") not in element_map:
            raise ToolError("SPEC_INVALID", f"modules.icons.icons[{index}]", "unknown element")
        element = element_map[icon["element_id"]]
        _set_derived(
            icon,
            "slide_bbox",
            element["slide_bbox"],
            f"modules.icons.icons[{index}].slide_bbox",
            changed,
            unchanged,
        )
        source_path = icon.get("source_path")
        if isinstance(source_path, str) and Path(source_path).is_file():
            _set_derived(
                icon,
                "source_sha256",
                file_sha256(source_path),
                f"modules.icons.icons[{index}].source_sha256",
                changed,
                unchanged,
            )


def scaffold_spec(
    spec: dict[str, Any], preflight: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if preflight.get("valid") is not True:
        raise ToolError("PREFLIGHT_STALE", "preflight", "runtime preflight must pass")
    if spec.get("schema_version") != 2:
        raise ToolError(
            "SPEC_SCHEMA_VERSION_UNSUPPORTED", "schema_version", "expected schema_version 2"
        )
    _require_semantics(spec)
    before_hash = canonical_json_sha256(spec)
    updated = copy.deepcopy(spec)
    changed: list[str] = []
    unchanged: list[str] = []

    for index, element in enumerate(updated["elements"]):
        expected = map_xywh_to_slide(element["source_bbox"], updated["canvas"])
        _set_derived(
            element,
            "slide_bbox",
            expected,
            f"elements[{index}].slide_bbox",
            changed,
            unchanged,
        )

    for index, region in enumerate(updated["regions"]):
        if not isinstance(region, dict) or "source_bbox" not in region:
            raise ToolError(
                "MISSING_REQUIRED_FIELD",
                f"regions[{index}].source_bbox",
                "main agent must provide region bounds",
            )
        expected = map_xywh_to_slide(region["source_bbox"], updated["canvas"])
        _set_derived(
            region,
            "slide_bbox",
            expected,
            f"regions[{index}].slide_bbox",
            changed,
            unchanged,
        )

    element_map = index_elements(updated)
    _sync_typography(updated, element_map, changed, unchanged)
    _sync_icons(updated, element_map, changed, unchanged)
    report = {
        "valid": True,
        "schema_version": 2,
        "spec_sha256_before": before_hash,
        "spec_sha256_after": canonical_json_sha256(updated),
        "changed": changed,
        "unchanged": unchanged,
        "missing": [],
        "blocked": [],
    }
    return updated, report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--preflight-report", type=Path, required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--in-place", action="store_true")
    destination.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        spec = load_schema_v2(args.spec)
        preflight = json.loads(args.preflight_report.read_text(encoding="utf-8"))
        if not isinstance(preflight, dict):
            raise ToolError("PREFLIGHT_STALE", str(args.preflight_report), "expected object")
        updated, report = scaffold_spec(spec, preflight)
        destination = args.spec if args.in_place else args.output
        if destination is None:
            raise ToolError("SPEC_INVALID", "--output", "output path is required")
        atomic_write_json(destination, updated)
        atomic_write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ToolError, OSError, json.JSONDecodeError) as exc:
        error = exc if isinstance(exc, ToolError) else ToolError("SPEC_INVALID", "$", str(exc))
        report = {"valid": False, "errors": [error.as_dict()]}
        atomic_write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
