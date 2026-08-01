"""Fail-closed element and multipart contracts for the schema v2 compiler."""

from __future__ import annotations

import copy
from typing import Any

from .capabilities import BUILDABLE_KINDS, require_supported_value
from .error_codes import ContractIssue, ToolError
from .geometry import (
    DRAWINGML_PERCENT_SCALE,
    bbox_contains,
    bbox_overlaps,
    bbox_union,
    quantize_drawingml_percentage,
    valid_drawingml_rotation,
    valid_font_size_pt,
    valid_nonnegative_coordinate32,
    validate_bbox,
)
from .schema_contracts import (
    ELEMENT_FIELDS,
    KIND_CONTENT_FIELDS,
    KIND_REQUIRED_CONTENT_FIELDS,
    KIND_REQUIRED_STYLE_FIELDS,
    KIND_STYLE_FIELDS,
    MULTIPART_CONTENT_FIELDS,
    MULTIPART_TEXT_STYLE_FIELDS,
    PART_CONTENT_FIELDS,
    PART_FIELDS,
    PART_STYLE_FIELDS,
    TABLE_BORDER_FIELDS,
    TABLE_BORDER_SIDES,
    TABLE_CELL_FIELDS,
    TABLE_FONT_FIELDS,
    TABLE_MARGIN_FIELDS,
)

EXPECTED_OBJECT_TYPES = {
    "text": frozenset({"sp"}),
    "shape": frozenset({"sp"}),
    "line": frozenset({"cxnSp"}),
    "table": frozenset({"graphicFrame"}),
    "matrix": frozenset({"sp"}),
    "status": frozenset({"sp"}),
    "picture": frozenset({"pic"}),
    "icon": frozenset({"pic"}),
}

def expected_object_types(kind: str) -> frozenset[str]:
    """Return OOXML object types permitted for a buildable element kind."""
    return EXPECTED_OBJECT_TYPES.get(kind, frozenset())


def _issue(
    code: str,
    path: str,
    detail: str,
    capability: str | None = None,
) -> ContractIssue:
    return ContractIssue(code, path, detail, capability)


def _element_path(element: Any) -> str:
    if isinstance(element, dict) and isinstance(element.get("element_id"), str) and element["element_id"]:
        return f"elements.{element['element_id']}"
    return "elements.<unknown>"


def _unknown_field_issue(path: str, fields: Any, allowed: frozenset[str]) -> list[ContractIssue]:
    if not isinstance(fields, dict):
        return [_issue("UNSUPPORTED_CAPABILITY", path, "payload must be an object")]
    unknown = sorted(set(fields) - allowed)
    if not unknown:
        return []
    return [
        _issue(
            "UNSUPPORTED_CAPABILITY",
            path,
            f"unknown fields: {', '.join(unknown)}",
        )
    ]


def _deep_merge(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _multipart_sequence(content: Any, path: str) -> tuple[dict[str, Any], list[Any], str] | None:
    if not isinstance(content, dict):
        return None
    defaults = content.get("part_defaults")
    has_parts = "parts" in content
    has_repeat = "repeat_sequence" in content
    if not isinstance(defaults, dict) or has_parts == has_repeat:
        return None
    sequence_name = "parts" if has_parts else "repeat_sequence"
    sequence = content.get(sequence_name)
    if not isinstance(sequence, list) or not sequence:
        return None
    return defaults, sequence, sequence_name


def expand_multipart_parts(element: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand explicit or repeat parts without mutating the schema element.

    ``repeat_sequence`` is intentionally an explicit ordered list of part
    exceptions.  Each item owns its absolute ``slide_bbox`` so repeated
    rendering cannot accumulate positional drift.
    """
    path = _element_path(element)
    if not isinstance(element, dict) or element.get("kind") not in {"matrix", "status"}:
        raise ToolError("PART_CONTRACT_INVALID", path, "multipart expansion requires matrix or status")
    sequence_data = _multipart_sequence(element.get("content"), f"{path}.content")
    if sequence_data is None:
        raise ToolError("PART_CONTRACT_INVALID", f"{path}.content", "requires part_defaults and exactly one part sequence")
    defaults, sequence, sequence_name = sequence_data
    expanded: list[dict[str, Any]] = []
    for index, item in enumerate(sequence):
        item_path = f"{path}.content.{sequence_name}[{index}]"
        if not isinstance(item, dict):
            raise ToolError("PART_CONTRACT_INVALID", item_path, "part must be an object")
        expanded.append(_deep_merge(defaults, item))
    return expanded


def _validate_round_rect(style: dict[str, Any], path: str) -> list[ContractIssue]:
    if style.get("shape_type") != "roundRect":
        return []
    adjustments = style.get("adjustments")
    invalid_shape = (
        not isinstance(adjustments, list)
        or len(adjustments) != 1
        or type(adjustments[0]) not in {int, float}
        or not 0 < adjustments[0] <= 0.5
    )
    quantized = (
        None
        if invalid_shape
        else quantize_drawingml_percentage(adjustments[0])
    )
    if (
        invalid_shape
        or quantized is None
        or not 1 <= quantized <= DRAWINGML_PERCENT_SCALE // 2
    ):
        return [
            _issue(
                "UNSUPPORTED_CAPABILITY",
                f"{path}.adjustments",
                "roundRect adjustment must quantize from 1 to 50000",
                "shape.roundRect.adjustment",
            )
        ]
    return []


def _validate_part_style_capabilities(
    style: dict[str, Any], path: str
) -> list[ContractIssue]:
    text_style = style.get("text_style")
    if "text_style" in style:
        issues = _unknown_field_issue(
            f"{path}.text_style", text_style, MULTIPART_TEXT_STYLE_FIELDS
        )
        if issues:
            return issues
        assert isinstance(text_style, dict)
        missing = sorted(MULTIPART_TEXT_STYLE_FIELDS - set(text_style))
        if missing:
            return [_issue(
                "UNSUPPORTED_CAPABILITY",
                f"{path}.text_style",
                f"missing text_style fields: {', '.join(missing)}",
            )]
        if "margins" in text_style:
            issues = _unknown_field_issue(
                f"{path}.text_style.margins",
                text_style["margins"],
                TABLE_MARGIN_FIELDS,
            )
            if issues:
                return issues
            assert isinstance(text_style["margins"], dict)
            if set(text_style["margins"]) != TABLE_MARGIN_FIELDS:
                return [_issue(
                    "UNSUPPORTED_CAPABILITY",
                    f"{path}.text_style.margins",
                    "all four margins must be non-negative integer EMU",
                )]
            for side in sorted(TABLE_MARGIN_FIELDS):
                if not valid_nonnegative_coordinate32(text_style["margins"][side]):
                    return [_issue(
                        "UNSUPPORTED_CAPABILITY",
                        f"{path}.text_style.margins.{side}",
                        "margin must be a non-negative integer EMU no greater than 2147483647",
                    )]
        scalar_checks = (
            (
                "font_name",
                lambda value: isinstance(value, str) and bool(value),
                "font_name must be a non-empty string",
            ),
            (
                "font_size",
                valid_font_size_pt,
                "font_size must be finite and from 1 to 4000 pt",
            ),
            (
                "font_weight",
                lambda value: type(value) is int and 1 <= value <= 1000,
                "font_weight must be an integer from 1 to 1000",
            ),
            ("color", _valid_rgb, "color must be #RRGGBB"),
            (
                "italic",
                lambda value: type(value) is bool,
                "italic must be boolean",
            ),
            (
                "alignment",
                lambda value: isinstance(value, str)
                and value in {"left", "center", "right", "justify"},
                "unsupported alignment",
            ),
            (
                "vertical_alignment",
                lambda value: isinstance(value, str)
                and value in {"top", "middle", "bottom"},
                "unsupported vertical_alignment",
            ),
            ("wrap", lambda value: type(value) is bool, "wrap must be boolean"),
        )
        for field, validator, detail in scalar_checks:
            if field in text_style and not validator(text_style[field]):
                return [_issue(
                    "UNSUPPORTED_CAPABILITY",
                    f"{path}.text_style.{field}",
                    detail,
                )]
    if "rotation" in style and not valid_drawingml_rotation(style["rotation"]):
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.rotation",
            "rotation must have a faithful DrawingML value within (-360, 360)",
        )]
    if "shape_type" in style:
        try:
            require_supported_value("shape_type", style["shape_type"], f"{path}.shape_type")
        except ToolError as exc:
            return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
    line = style.get("line")
    if isinstance(line, dict) and "dash" in line:
        try:
            require_supported_value("line_dash", line["dash"], f"{path}.line.dash")
        except ToolError as exc:
            return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
    return []


def _valid_rgb(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 7
        and value.startswith("#")
        and all(character in "0123456789abcdefABCDEF" for character in value[1:])
    )


def _validate_table(element: dict[str, Any], path: str) -> list[ContractIssue]:
    content = element["content"]
    rows = content.get("rows")
    columns = content.get("columns")
    cells = content.get("cells")
    if (
        not isinstance(rows, list)
        or not rows
        or any(type(value) is not int or value <= 0 for value in rows)
    ):
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.rows", "rows must be a non-empty array of positive EMU heights")]
    if (
        not isinstance(columns, list)
        or not columns
        or any(type(value) is not int or value <= 0 for value in columns)
    ):
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.columns", "columns must be a non-empty array of positive EMU widths")]
    try:
        bbox = validate_bbox(element.get("slide_bbox"), f"{path}.slide_bbox")
    except ToolError as exc:
        return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
    if sum(rows) != bbox[3] or sum(columns) != bbox[2]:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content", "table row and column sizes must exactly equal slide_bbox")]
    if not isinstance(cells, list) or not cells:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.cells", "cells must be a non-empty array")]

    occupied: set[tuple[int, int]] = set()
    for index, cell in enumerate(cells):
        cell_path = f"{path}.content.cells[{index}]"
        issues = _unknown_field_issue(cell_path, cell, TABLE_CELL_FIELDS)
        if issues:
            return issues
        assert isinstance(cell, dict)
        missing = sorted(TABLE_CELL_FIELDS - set(cell))
        if missing:
            return [_issue("UNSUPPORTED_CAPABILITY", cell_path, f"missing cell fields: {', '.join(missing)}")]
        row = cell["row"]
        column = cell["column"]
        row_span = cell["row_span"]
        column_span = cell["column_span"]
        if any(type(value) is not int for value in (row, column, row_span, column_span)) or row_span <= 0 or column_span <= 0:
            return [_issue("UNSUPPORTED_CAPABILITY", cell_path, "cell coordinates and spans must be positive integers")]
        coordinates = {
            (row_index, column_index)
            for row_index in range(row, row + row_span)
            for column_index in range(column, column + column_span)
        }
        if (
            not coordinates
            or any(
                row_index < 0
                or column_index < 0
                or row_index >= len(rows)
                or column_index >= len(columns)
                for row_index, column_index in coordinates
            )
            or occupied & coordinates
        ):
            return [_issue("UNSUPPORTED_CAPABILITY", cell_path, "cell span is overlapping or out of range")]
        occupied.update(coordinates)
        if not isinstance(cell["text"], str):
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.text", "cell text must be a string")]
        if cell["fill"] != "noFill" and not _valid_rgb(cell["fill"]):
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.fill", "cell fill must be noFill or #RRGGBB")]
        margins = cell["margins"]
        issues = _unknown_field_issue(f"{cell_path}.margins", margins, TABLE_MARGIN_FIELDS)
        if issues:
            return issues
        assert isinstance(margins, dict)
        if set(margins) != TABLE_MARGIN_FIELDS:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.margins", "all four margins must be non-negative integer EMU")]
        for side in sorted(TABLE_MARGIN_FIELDS):
            if not valid_nonnegative_coordinate32(margins[side]):
                return [_issue(
                    "UNSUPPORTED_CAPABILITY",
                    f"{cell_path}.margins.{side}",
                    "margin must be an integer from 0 to 2147483647 EMU",
                )]
        if not isinstance(cell["alignment"], str) or cell["alignment"] not in {
            "left", "center", "right", "justify"
        }:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.alignment", "unsupported cell alignment")]
        if not isinstance(cell["vertical_alignment"], str) or cell[
            "vertical_alignment"
        ] not in {"top", "middle", "bottom"}:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.vertical_alignment", "unsupported cell vertical alignment")]
        font = cell["font"]
        issues = _unknown_field_issue(f"{cell_path}.font", font, TABLE_FONT_FIELDS)
        if issues:
            return issues
        assert isinstance(font, dict)
        if set(font) != TABLE_FONT_FIELDS:
            return [_issue("UNSUPPORTED_CAPABILITY", f"{cell_path}.font", "font contract is incomplete")]
        font_checks = (
            ("name", isinstance(font["name"], str) and bool(font["name"]), "font name must be non-empty"),
            ("size", valid_font_size_pt(font["size"]), "font size must be finite and from 1 to 4000 pt"),
            ("weight", type(font["weight"]) is int and 1 <= font["weight"] <= 1000, "font weight must be an integer from 1 to 1000"),
            ("color", _valid_rgb(font["color"]), "font color must be #RRGGBB"),
            ("italic", type(font["italic"]) is bool, "font italic must be boolean"),
        )
        for field, valid, detail in font_checks:
            if not valid:
                return [_issue(
                    "UNSUPPORTED_CAPABILITY",
                    f"{cell_path}.font.{field}",
                    detail,
                )]
        borders = cell["borders"]
        issues = _unknown_field_issue(f"{cell_path}.borders", borders, TABLE_BORDER_SIDES)
        if issues:
            return issues
        assert isinstance(borders, dict)
        for side, border in borders.items():
            border_path = f"{cell_path}.borders.{side}"
            issues = _unknown_field_issue(border_path, border, TABLE_BORDER_FIELDS)
            if issues:
                return issues
            assert isinstance(border, dict)
            if (
                set(border) != TABLE_BORDER_FIELDS
                or not _valid_rgb(border.get("color"))
                or type(border.get("width")) is not int
                or not 1 <= border["width"] <= 20_116_800
            ):
                return [_issue("UNSUPPORTED_CAPABILITY", border_path, "border requires #RRGGBB color and valid DrawingML width")]
    expected = {
        (row_index, column_index)
        for row_index in range(len(rows))
        for column_index in range(len(columns))
    }
    if occupied != expected:
        return [_issue("UNSUPPORTED_CAPABILITY", f"{path}.content.cells", "cells must completely cover the table without overlap")]
    rotation = element["style"].get("rotation", 0)
    if not valid_drawingml_rotation(rotation):
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.style.rotation",
            "rotation must have a faithful DrawingML value within (-360, 360)",
        )]
    return []


def _validate_multipart(element: dict[str, Any], path: str) -> list[ContractIssue]:
    content = element.get("content")
    issues = _unknown_field_issue(f"{path}.content", content, MULTIPART_CONTENT_FIELDS)
    if issues:
        return issues
    assert isinstance(content, dict)
    sequence_data = _multipart_sequence(content, f"{path}.content")
    if sequence_data is None:
        return [
            _issue(
                "PART_CONTRACT_INVALID",
                f"{path}.content",
                "requires part_defaults and exactly one of parts or repeat_sequence",
            )
        ]
    defaults, sequence, sequence_name = sequence_data
    defaults_issues = _unknown_field_issue(f"{path}.content.part_defaults", defaults, PART_FIELDS - {"part_id", "slide_bbox"})
    if defaults_issues:
        return defaults_issues
    for payload_name, allowed in (("style", PART_STYLE_FIELDS), ("content", PART_CONTENT_FIELDS)):
        if payload_name in defaults:
            payload_issues = _unknown_field_issue(
                f"{path}.content.part_defaults.{payload_name}", defaults[payload_name], allowed
            )
            if payload_issues:
                return payload_issues
    if "style" in defaults:
        default_capability_issues = _validate_part_style_capabilities(
            defaults["style"], f"{path}.content.part_defaults.style"
        )
        if default_capability_issues:
            return default_capability_issues
    if (
        isinstance(defaults.get("content"), dict)
        and "text" in defaults["content"]
        and not isinstance(defaults["content"]["text"], str)
    ):
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content.part_defaults.content.text",
            "part text must be a string",
        )]
    allow_overlap = content.get("allow_overlap", False)
    if type(allow_overlap) is not bool:
        return [_issue("PART_CONTRACT_INVALID", f"{path}.content.allow_overlap", "must be boolean")]

    try:
        parent_bbox = validate_bbox(element.get("slide_bbox"), f"{path}.slide_bbox")
        parts = expand_multipart_parts(element)
    except ToolError as exc:
        return [_issue(exc.code, exc.path, exc.detail, exc.capability)]

    seen_ids: set[str] = set()
    prior: list[list[int]] = []
    boxes: list[list[int]] = []
    for index, part in enumerate(parts):
        part_path = f"{path}.content.{sequence_name}[{index}]"
        raw_part = sequence[index]
        assert isinstance(raw_part, dict)
        if "style" in raw_part:
            raw_style_issues = _unknown_field_issue(
                f"{part_path}.style", raw_part["style"], PART_STYLE_FIELDS
            )
            if raw_style_issues:
                return raw_style_issues
            assert isinstance(raw_part["style"], dict)
            raw_capability_issues = _validate_part_style_capabilities(
                raw_part["style"], f"{part_path}.style"
            )
            if raw_capability_issues:
                return raw_capability_issues
        unknown = _unknown_field_issue(part_path, part, PART_FIELDS)
        if unknown:
            return unknown
        part_id = part.get("part_id")
        if not isinstance(part_id, str) or not part_id:
            return [_issue("PART_CONTRACT_INVALID", f"{part_path}.part_id", "part_id must be non-empty")]
        if part_id in seen_ids:
            return [_issue("PART_CONTRACT_INVALID", f"{part_path}.part_id", "part_id must be unique")]
        seen_ids.add(part_id)
        for payload_name, allowed in (("style", PART_STYLE_FIELDS), ("content", PART_CONTENT_FIELDS)):
            if payload_name in part:
                payload_issues = _unknown_field_issue(f"{part_path}.{payload_name}", part[payload_name], allowed)
                if payload_issues:
                    return payload_issues
        if "style" in part:
            capability_issues = _validate_part_style_capabilities(
                part["style"], f"{part_path}.style"
            )
            if capability_issues:
                return capability_issues
        if (
            isinstance(part.get("content"), dict)
            and "text" in part["content"]
            and not isinstance(part["content"]["text"], str)
        ):
            return [_issue(
                "UNSUPPORTED_CAPABILITY",
                f"{part_path}.content.text",
                "part text must be a string",
            )]
        try:
            bbox = validate_bbox(part.get("slide_bbox"), f"{part_path}.slide_bbox")
        except ToolError as exc:
            return [_issue("PART_CONTRACT_INVALID", exc.path, exc.detail)]
        if not bbox_contains(parent_bbox, bbox):
            return [_issue("PART_CONTRACT_INVALID", f"{part_path}.slide_bbox", "part bbox must be inside parent bbox")]
        if not allow_overlap and any(bbox_overlaps(bbox, previous) for previous in prior):
            return [_issue("PART_CONTRACT_INVALID", f"{part_path}.slide_bbox", "overlap requires allow_overlap: true")]
        prior.append(bbox)
        boxes.append(bbox)
    try:
        union = bbox_union(boxes)
    except ToolError as exc:
        return [_issue("PART_CONTRACT_INVALID", exc.path, exc.detail)]
    if union != parent_bbox:
        return [_issue("PART_CONTRACT_INVALID", f"{path}.content", "part union must equal parent slide_bbox")]
    return []


def validate_element_contract(element: Any) -> list[ContractIssue]:
    """Return stable fail-closed issues for one schema v2 element."""
    path = _element_path(element)
    if not isinstance(element, dict):
        return [_issue("UNSUPPORTED_CAPABILITY", path, "element must be an object")]
    unknown = sorted(set(element) - ELEMENT_FIELDS)
    if unknown:
        return [_issue("UNSUPPORTED_CAPABILITY", path, f"unknown fields: {', '.join(unknown)}")]
    missing = sorted(ELEMENT_FIELDS - set(element))
    if missing:
        return [_issue("UNSUPPORTED_CAPABILITY", path, f"missing fields: {', '.join(missing)}")]
    kind = element.get("kind")
    if not isinstance(kind, str) or kind not in BUILDABLE_KINDS:
        return [_issue("UNSUPPORTED_KIND", f"{path}.kind", "kind is not buildable")]
    style = element.get("style")
    content = element.get("content")
    style_issues = _unknown_field_issue(f"{path}.style", style, KIND_STYLE_FIELDS[kind])
    if style_issues:
        return style_issues
    content_issues = _unknown_field_issue(f"{path}.content", content, KIND_CONTENT_FIELDS[kind])
    if content_issues:
        return content_issues
    assert isinstance(style, dict)
    assert isinstance(content, dict)
    missing_style = sorted(KIND_REQUIRED_STYLE_FIELDS[kind] - set(style))
    if missing_style:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.style",
            f"missing fields: {', '.join(missing_style)}",
        )]
    missing_content = sorted(KIND_REQUIRED_CONTENT_FIELDS[kind] - set(content))
    if missing_content:
        return [_issue(
            "UNSUPPORTED_CAPABILITY",
            f"{path}.content",
            f"missing fields: {', '.join(missing_content)}",
        )]

    if kind == "shape":
        try:
            require_supported_value("shape_type", style["shape_type"], f"{path}.style.shape_type")
        except ToolError as exc:
            return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
        return _validate_round_rect(style, f"{path}.style")
    if kind == "line" and isinstance(style.get("line"), dict) and "dash" in style["line"]:
        try:
            require_supported_value("line_dash", style["line"]["dash"], f"{path}.style.line.dash")
        except ToolError as exc:
            return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
    if kind in {"picture", "icon"} and "mode" in content:
        try:
            require_supported_value("picture_mode", content["mode"], f"{path}.content.mode")
        except ToolError as exc:
            return [_issue(exc.code, exc.path, exc.detail, exc.capability)]
    if kind in {"matrix", "status"}:
        return _validate_multipart(element, path)
    if kind == "table":
        return _validate_table(element, path)
    return []
