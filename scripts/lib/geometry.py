from __future__ import annotations

from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any

from .error_codes import ToolError


def _is_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def validate_xywh(value: Any, path: str) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 4
        or any(not _is_number(item) for item in value)
    ):
        raise ToolError("SPEC_INVALID", path, "expected four numeric XYWH values")
    x, y, width, height = (float(item) for item in value)
    if width <= 0 or height <= 0:
        raise ToolError("SPEC_INVALID", path, "width and height must be positive")
    return x, y, width, height


def map_xywh_to_slide(source_bbox: Any, canvas: Mapping[str, Any]) -> list[int]:
    x, y, width, height = validate_xywh(source_bbox, "source_bbox")
    frame_x, frame_y, frame_width, frame_height = validate_xywh(
        canvas.get("page_frame_bbox"), "canvas.page_frame_bbox"
    )
    slide_size = canvas.get("slide_size_emu")
    if (
        not isinstance(slide_size, Sequence)
        or isinstance(slide_size, (str, bytes))
        or len(slide_size) != 2
        or any(not _is_number(item) or item <= 0 for item in slide_size)
    ):
        raise ToolError(
            "SPEC_INVALID", "canvas.slide_size_emu", "expected two positive numbers"
        )
    if (
        x < frame_x
        or y < frame_y
        or x + width > frame_x + frame_width
        or y + height > frame_y + frame_height
    ):
        raise ToolError("BBOX_OUT_OF_RANGE", "source_bbox", "bbox exceeds page frame")
    slide_width, slide_height = (float(item) for item in slide_size)
    return [
        round((x - frame_x) * slide_width / frame_width),
        round((y - frame_y) * slide_height / frame_height),
        round(width * slide_width / frame_width),
        round(height * slide_height / frame_height),
    ]


def contain_bbox(
    asset_size: Sequence[int], target_bbox: Sequence[int]
) -> list[int]:
    asset_width, asset_height = asset_size
    x, y, width, height = target_bbox
    scale = min(width / asset_width, height / asset_height)
    rendered_width = round(asset_width * scale)
    rendered_height = round(asset_height * scale)
    return [
        round(x + (width - rendered_width) / 2),
        round(y + (height - rendered_height) / 2),
        rendered_width,
        rendered_height,
    ]
