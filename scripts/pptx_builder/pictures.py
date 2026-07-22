from __future__ import annotations

from typing import Any

from PIL import Image

from lib.error_codes import ToolError
from lib.geometry import contain_bbox
from lib.hashing import file_sha256
from .common import ObjectRegistry, require_asset


def add_picture_element(
    slide: Any,
    element: dict[str, Any],
    asset: dict[str, Any],
    registry: ObjectRegistry,
) -> None:
    element_id = element["element_id"]
    if element.get("style") not in ({}, None):
        raise ToolError("UNSUPPORTED_FEATURE", f"elements.{element_id}.style", "picture style must be expressed by placement")
    asset_path = require_asset(
        asset.get("asset_path"), asset.get("asset_sha256"), f"elements.{element_id}.content.asset"
    )
    placement = element.get("content", {}).get("placement", {"mode": "contain"})
    if not isinstance(placement, dict):
        raise ToolError("SPEC_INVALID", f"elements.{element_id}.content.placement", "expected object")
    allowed = {"mode", "crop", "opacity", "focus_x", "focus_y", "rotation"}
    unknown = set(placement) - allowed
    if unknown:
        raise ToolError(
            "UNSUPPORTED_FEATURE",
            f"elements.{element_id}.content.placement",
            f"unsupported fields: {', '.join(sorted(unknown))}",
        )
    opacity = placement.get("opacity", 1.0)
    if not isinstance(opacity, (int, float)) or isinstance(opacity, bool) or float(opacity) != 1.0:
        raise ToolError("UNSUPPORTED_FEATURE", f"elements.{element_id}.content.placement.opacity", str(opacity))
    mode = placement.get("mode", "contain")
    with Image.open(asset_path) as image:
        size = image.size
    target = element["slide_bbox"]
    if mode == "contain":
        bbox = contain_bbox(size, target)
        picture = slide.shapes.add_picture(str(asset_path), *bbox)
    elif mode == "cover":
        picture = slide.shapes.add_picture(str(asset_path), *target)
        asset_ratio = size[0] / size[1]
        target_ratio = target[2] / target[3]
        focus_x = placement.get("focus_x", 0.5)
        focus_y = placement.get("focus_y", 0.5)
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1
            for value in (focus_x, focus_y)
        ):
            raise ToolError("SPEC_INVALID", f"elements.{element_id}.content.placement.focus", "focus must be 0..1")
        if asset_ratio > target_ratio:
            visible = target_ratio / asset_ratio
            left = min(max(float(focus_x) - visible / 2, 0), 1 - visible)
            picture.crop_left = left
            picture.crop_right = 1 - visible - left
        elif asset_ratio < target_ratio:
            visible = asset_ratio / target_ratio
            top = min(max(float(focus_y) - visible / 2, 0), 1 - visible)
            picture.crop_top = top
            picture.crop_bottom = 1 - visible - top
    elif mode == "crop":
        crop = placement.get("crop")
        if not isinstance(crop, dict) or any(key not in crop for key in ("left", "top", "right", "bottom")):
            raise ToolError("MISSING_REQUIRED_FIELD", f"elements.{element_id}.content.placement.crop", "four crop sides required")
        picture = slide.shapes.add_picture(str(asset_path), *target)
        picture.crop_left = float(crop["left"])
        picture.crop_top = float(crop["top"])
        picture.crop_right = float(crop["right"])
        picture.crop_bottom = float(crop["bottom"])
    else:
        raise ToolError("UNSUPPORTED_FEATURE", f"elements.{element_id}.content.placement.mode", mode)
    rotation = placement.get("rotation", 0)
    if not isinstance(rotation, (int, float)) or isinstance(rotation, bool) or not -360 <= rotation <= 360:
        raise ToolError("SPEC_INVALID", f"elements.{element_id}.content.placement.rotation", "expected -360..360")
    picture.rotation = float(rotation)
    registry.register(
        element_id,
        picture,
        "picture",
        media_sha256=file_sha256(asset_path),
    )
