"""The single XYWH bounding-box implementation for compiler contracts."""

from __future__ import annotations

import math
from typing import Any, Iterable

from .error_codes import ToolError


DRAWINGML_PERCENT_SCALE = 100_000
DRAWINGML_ANGLE_SCALE = 60_000
DRAWINGML_FULL_CIRCLE = 360 * DRAWINGML_ANGLE_SCALE
DRAWINGML_LINE_WIDTH_MAX = 20_116_800
OOXML_COORDINATE32_MAX = 2_147_483_647
OOXML_FONT_SIZE_PT_MIN = 1
OOXML_FONT_SIZE_PT_MAX = 4_000
NEAR_FULL_PAGE_MIN_AREA_RATIO = 0.95
NEAR_FULL_PAGE_MAX_MARGIN_RATIO = 0.01


def quantize_drawingml_percentage(value: int | float) -> int:
    """Return the exact integer written for a DrawingML percentage."""
    return round(value * DRAWINGML_PERCENT_SCALE)


def quantize_drawingml_angle(value: int | float) -> int:
    """Return the exact integer written for a DrawingML degree angle."""
    return round(value * DRAWINGML_ANGLE_SCALE)


def valid_drawingml_rotation(value: Any) -> bool:
    """Return whether a degree value has a faithful DrawingML representation."""
    if (
        type(value) not in {int, float}
        or not -360 < value < 360
        or not math.isfinite(float(value))
    ):
        return False
    quantized = quantize_drawingml_angle(value)
    return (
        -DRAWINGML_FULL_CIRCLE < quantized < DRAWINGML_FULL_CIRCLE
        and (value == 0 or quantized != 0)
    )


def valid_font_size_pt(value: Any) -> bool:
    """Return whether a point size is finite and representable by DrawingML."""
    return (
        type(value) in {int, float}
        and OOXML_FONT_SIZE_PT_MIN <= value <= OOXML_FONT_SIZE_PT_MAX
        and math.isfinite(float(value))
    )


def valid_nonnegative_coordinate32(value: Any) -> bool:
    """Return whether a value is a non-negative 32-bit OOXML coordinate."""
    return type(value) is int and 0 <= value <= OOXML_COORDINATE32_MAX


def validate_bbox(value: Any, path: str) -> list[int]:
    """Return a validated ``[x, y, width, height]`` integer bounding box."""
    if not isinstance(value, list) or len(value) != 4:
        raise ToolError("INVALID_BBOX", path, "bbox must be [x, y, width, height]")
    if any(type(component) is not int for component in value):
        raise ToolError("INVALID_BBOX", path, "bbox values must be integers")
    x, y, width, height = value
    if x < 0 or y < 0:
        raise ToolError("INVALID_BBOX", path, "bbox origin must be non-negative")
    if width <= 0 or height <= 0:
        raise ToolError("INVALID_BBOX", path, "bbox width and height must be positive")
    return [x, y, width, height]


def _validate_numeric_bbox(value: Any, path: str) -> list[int | float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ToolError("INVALID_BBOX", path, "bbox must be [x, y, width, height]")
    if any(
        type(component) not in {int, float}
        or not math.isfinite(float(component))
        for component in value
    ):
        raise ToolError("INVALID_BBOX", path, "bbox values must be finite numbers")
    if value[2] <= 0 or value[3] <= 0:
        raise ToolError("INVALID_BBOX", path, "bbox width and height must be positive")
    return list(value)


def is_near_full_page_bbox(candidate: Any, page_frame: Any) -> bool:
    """Return whether *candidate* is a near-full-page XYWH rectangle.

    The shared risk boundary is inclusive: candidate area is at least 95% of
    the page frame and each uncovered edge margin is no more than 1% of the
    corresponding page dimension. Overscan therefore remains a full-page risk.
    """
    x, y, width, height = _validate_numeric_bbox(candidate, "candidate_bbox")
    page_x, page_y, page_width, page_height = _validate_numeric_bbox(
        page_frame, "page_frame_bbox"
    )
    area_ratio = (width * height) / (page_width * page_height)
    margins = (
        (x - page_x, page_width),
        (y - page_y, page_height),
        (page_x + page_width - (x + width), page_width),
        (page_y + page_height - (y + height), page_height),
    )
    return (
        area_ratio >= NEAR_FULL_PAGE_MIN_AREA_RATIO
        and all(
            margin <= dimension * NEAR_FULL_PAGE_MAX_MARGIN_RATIO
            for margin, dimension in margins
        )
    )


def bbox_contains(outer: Any, inner: Any) -> bool:
    """Return whether *outer* completely contains *inner*."""
    outer_x, outer_y, outer_width, outer_height = validate_bbox(outer, "outer")
    inner_x, inner_y, inner_width, inner_height = validate_bbox(inner, "inner")
    return (
        outer_x <= inner_x
        and outer_y <= inner_y
        and outer_x + outer_width >= inner_x + inner_width
        and outer_y + outer_height >= inner_y + inner_height
    )


def bbox_overlaps(first: Any, second: Any) -> bool:
    """Return whether two boxes have positive-area intersection."""
    first_x, first_y, first_width, first_height = validate_bbox(first, "first")
    second_x, second_y, second_width, second_height = validate_bbox(second, "second")
    return (
        max(first_x, second_x) < min(first_x + first_width, second_x + second_width)
        and max(first_y, second_y) < min(first_y + first_height, second_y + second_height)
    )


def bbox_union(boxes: Iterable[Any]) -> list[int]:
    """Return the smallest XYWH box covering every supplied box."""
    validated = [validate_bbox(box, f"boxes[{index}]") for index, box in enumerate(boxes)]
    if not validated:
        raise ToolError("INVALID_BBOX", "boxes", "bbox union requires at least one box")
    left = min(box[0] for box in validated)
    top = min(box[1] for box in validated)
    right = max(box[0] + box[2] for box in validated)
    bottom = max(box[1] + box[3] for box in validated)
    return [left, top, right - left, bottom - top]
