from __future__ import annotations

from typing import Any

from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Pt

from lib.error_codes import ToolError
from .common import ObjectRegistry, rgb
from .ooxml import set_table_cell_border


HORIZONTAL = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT, "justify": PP_ALIGN.JUSTIFY}
VERTICAL = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}


def _apply_cell_style(cell: Any, contract: dict[str, Any], path: str) -> None:
    allowed = {
        "row", "column", "row_span", "column_span", "text", "fill", "margins",
        "horizontal_alignment", "vertical_alignment", "text_style", "borders",
    }
    unknown = set(contract) - allowed
    if unknown:
        raise ToolError("UNSUPPORTED_FEATURE", path, f"unsupported cell fields: {', '.join(sorted(unknown))}")
    fill = contract.get("fill")
    if fill is not None:
        if not isinstance(fill, str):
            raise ToolError("SPEC_INVALID", f"{path}.fill", "expected #RRGGBB")
        cell.fill.solid()
        cell.fill.fore_color.rgb = rgb(fill, f"{path}.fill")
    margins = contract.get("margins", {})
    if not isinstance(margins, dict) or set(margins) - {"left", "right", "top", "bottom"}:
        raise ToolError("SPEC_INVALID", f"{path}.margins", "invalid margins")
    for side in ("left", "right", "top", "bottom"):
        if side in margins:
            setattr(cell, f"margin_{side}", int(margins[side]))
    horizontal = contract.get("horizontal_alignment", "left")
    vertical = contract.get("vertical_alignment", "top")
    if horizontal not in HORIZONTAL:
        raise ToolError("UNSUPPORTED_FEATURE", f"{path}.horizontal_alignment", str(horizontal))
    if vertical not in VERTICAL:
        raise ToolError("UNSUPPORTED_FEATURE", f"{path}.vertical_alignment", str(vertical))
    cell.vertical_anchor = VERTICAL[vertical]
    for paragraph in cell.text_frame.paragraphs:
        paragraph.alignment = HORIZONTAL[horizontal]
    text_style = contract.get("text_style")
    if text_style is not None:
        if not isinstance(text_style, dict):
            raise ToolError("SPEC_INVALID", f"{path}.text_style", "expected object")
        allowed_text = {"font_name", "font_size", "font_weight", "color", "italic"}
        unknown_text = set(text_style) - allowed_text
        if unknown_text:
            raise ToolError("UNSUPPORTED_FEATURE", f"{path}.text_style", f"unsupported fields: {', '.join(sorted(unknown_text))}")
        for paragraph in cell.text_frame.paragraphs:
            if not paragraph.runs and paragraph.text:
                run = paragraph.add_run()
                run.text = paragraph.text
            for run in paragraph.runs:
                run.font.name = text_style.get("font_name", "Arial")
                run.font.size = Pt(text_style.get("font_size", 12))
                run.font.bold = text_style.get("font_weight", 400) >= 600
                run.font.italic = bool(text_style.get("italic", False))
                run.font.color.rgb = rgb(text_style.get("color", "#000000"), f"{path}.text_style.color")
    borders = contract.get("borders")
    if borders is not None:
        if not isinstance(borders, dict) or set(borders) - {"left", "right", "top", "bottom"}:
            raise ToolError("SPEC_INVALID", f"{path}.borders", "expected four named sides")
        tc_pr = cell._tc.get_or_add_tcPr()
        for side, border in borders.items():
            set_table_cell_border(tc_pr, side, border, f"{path}.borders.{side}")


def add_table_element(slide: Any, element: dict[str, Any], registry: ObjectRegistry) -> None:
    content = element["content"]
    rows = content.get("rows")
    columns = content.get("columns")
    row_heights = content.get("row_heights")
    column_widths = content.get("column_widths")
    cells = content.get("cells")
    if (
        type(rows) is not int
        or type(columns) is not int
        or rows <= 0
        or columns <= 0
        or not isinstance(row_heights, list)
        or len(row_heights) != rows
        or not isinstance(column_widths, list)
        or len(column_widths) != columns
        or not isinstance(cells, list)
    ):
        raise ToolError("SPEC_INVALID", f"elements.{element['element_id']}.content", "invalid table contract")
    x, y, width, height = element["slide_bbox"]
    if sum(row_heights) != height or sum(column_widths) != width:
        raise ToolError("SPEC_INVALID", f"elements.{element['element_id']}.content", "table sizes must match bbox")
    shape = slide.shapes.add_table(rows, columns, x, y, width, height)
    table = shape.table
    for index, value in enumerate(row_heights):
        table.rows[index].height = int(value)
    for index, value in enumerate(column_widths):
        table.columns[index].width = int(value)
    occupied: set[tuple[int, int]] = set()
    for index, contract in enumerate(cells):
        row = contract.get("row")
        column = contract.get("column")
        row_span = contract.get("row_span", 1)
        column_span = contract.get("column_span", 1)
        if any(type(value) is not int for value in (row, column, row_span, column_span)):
            raise ToolError("SPEC_INVALID", f"elements.{element['element_id']}.content.cells[{index}]", "integer coordinates required")
        coordinates = {
            (r, c)
            for r in range(row, row + row_span)
            for c in range(column, column + column_span)
        }
        if not coordinates or any(r < 0 or c < 0 or r >= rows or c >= columns for r, c in coordinates) or occupied & coordinates:
            raise ToolError("SPEC_INVALID", f"elements.{element['element_id']}.content.cells[{index}]", "overlapping or out-of-range cell")
        occupied.update(coordinates)
        cell = table.cell(row, column)
        if row_span > 1 or column_span > 1:
            cell.merge(table.cell(row + row_span - 1, column + column_span - 1))
        cell.text = str(contract.get("text", ""))
        _apply_cell_style(cell, contract, f"elements.{element['element_id']}.content.cells[{index}]")
    if occupied != {(r, c) for r in range(rows) for c in range(columns)}:
        raise ToolError("SPEC_INVALID", f"elements.{element['element_id']}.content.cells", "cells must cover table")
    registry.register(element["element_id"], shape, "table")


def _inside(child: list[int], parent: list[int]) -> bool:
    return (
        child[0] >= parent[0]
        and child[1] >= parent[1]
        and child[0] + child[2] <= parent[0] + parent[2]
        and child[1] + child[3] <= parent[1] + parent[3]
    )


def add_multipart_element(slide: Any, element: dict[str, Any], registry: ObjectRegistry) -> None:
    element_id = element["element_id"]
    collection_name = "cells" if element["kind"] == "matrix" else "segments"
    parts = element.get("content", {}).get(collection_name)
    if not isinstance(parts, list) or not parts:
        raise ToolError(
            "MISSING_REQUIRED_FIELD",
            f"elements.{element_id}.content.{collection_name}",
            "explicit parts required",
        )
    seen: set[str] = set()
    for index, contract in enumerate(parts):
        path = f"elements.{element_id}.content.{collection_name}[{index}]"
        if not isinstance(contract, dict):
            raise ToolError("SPEC_INVALID", path, "expected object")
        part = contract.get("part")
        bbox = contract.get("slide_bbox")
        if not isinstance(part, str) or not part or part in seen:
            raise ToolError("SPEC_INVALID", f"{path}.part", "unique part required")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(type(value) is not int for value in bbox)
            or bbox[2] <= 0
            or bbox[3] <= 0
            or not _inside(bbox, element["slide_bbox"])
        ):
            raise ToolError("SPEC_INVALID", f"{path}.slide_bbox", "part must stay inside parent")
        seen.add(part)
        shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *bbox)
        fill = contract.get("fill")
        if not isinstance(fill, str):
            raise ToolError("MISSING_REQUIRED_FIELD", f"{path}.fill", "explicit fill required")
        shape.fill.solid()
        shape.fill.fore_color.rgb = rgb(fill, f"{path}.fill")
        line = contract.get("line")
        if line is None:
            shape.line.fill.background()
        elif isinstance(line, str):
            shape.line.color.rgb = rgb(line, f"{path}.line")
        else:
            raise ToolError("SPEC_INVALID", f"{path}.line", "line must be color or null")
        text = contract.get("text")
        if text is not None:
            text_style = contract.get("text_style")
            if not isinstance(text, str) or not isinstance(text_style, dict):
                raise ToolError("MISSING_REQUIRED_FIELD", f"{path}.text_style", "explicit text style required")
            frame = shape.text_frame
            frame.clear()
            run = frame.paragraphs[0].add_run()
            run.text = text
            font_name = text_style.get("font_name")
            font_size = text_style.get("font_size")
            color = text_style.get("color")
            if not isinstance(font_name, str) or not isinstance(font_size, (int, float)) or not isinstance(color, str):
                raise ToolError("SPEC_INVALID", f"{path}.text_style", "font_name/font_size/color required")
            run.font.name = font_name
            run.font.size = Pt(font_size)
            run.font.color.rgb = rgb(color, f"{path}.text_style.color")
        registry.register(element_id, shape, "shape", part=part, text_summary=text)
