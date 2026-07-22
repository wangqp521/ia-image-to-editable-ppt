#!/usr/bin/env python3
"""Render source context, source crop, and extracted assets for shared review."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

from lib.atomic_write import atomic_write_json
from lib.error_codes import ToolError
from lib.hashing import canonical_json_sha256, file_sha256
from lib.schema_io import load_schema_v2


GREEN = (0, 255, 0)
ROI_COLOR = (255, 0, 255)
PANEL_WIDTH = 360
PANEL_HEIGHT = 260
LABEL_HEIGHT = 24
MARGIN = 16
GAP = 16
ICON_SCALE = 4
PICTURE_SCALE = 2.5
CONTEXT_SCALE = 2
MANIFEST_VERSION = 2
MANIFEST_METADATA_KEY = "asset_manifest_sha256"


def _fit_scaled(image: Image.Image, scale: float, size: tuple[int, int], *, icon: bool) -> Image.Image:
    scaled = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        getattr(Image, "Resampling", Image).NEAREST if icon else getattr(Image, "Resampling", Image).LANCZOS,
    )
    if scaled.width <= size[0] and scaled.height <= size[1]:
        return scaled
    result = scaled.copy()
    result.thumbnail(size, getattr(Image, "Resampling", Image).LANCZOS)
    return result


def _asset_records(spec: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    source = spec.get("clean_visual_reference", {}).get("path")
    for element in spec.get("elements", []):
        content = element.get("content") if isinstance(element, dict) else None
        asset = content.get("asset") if isinstance(content, dict) else None
        if isinstance(asset, dict) and isinstance(asset.get("asset_path"), str):
            element_id = str(element.get("element_id"))
            records.append(
                {
                    **asset,
                    "element_id": element_id,
                    "kind": element.get("kind", "picture"),
                    "source_path": asset.get("source_path", source),
                    "source_bbox": element.get("source_bbox"),
                }
            )
            seen.add(element_id)
    icons = spec.get("modules", {}).get("icons", {}).get("icons", [])
    if isinstance(icons, list):
        for item in icons:
            if not isinstance(item, dict):
                continue
            element_id = str(item.get("element_id", item.get("icon_id")))
            if element_id in seen:
                continue
            records.append({**item, "element_id": element_id, "kind": "icon"})
            seen.add(element_id)
    return records


def _cached_manifest(output: Path) -> str | None:
    if not output.is_file():
        return None
    try:
        with Image.open(output) as image:
            image.verify()
        with Image.open(output) as image:
            value = image.info.get(MANIFEST_METADATA_KEY)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, str) and len(value) == 64 else None


def _manifest_entry(record: dict[str, Any], source: Path, asset: Path, bbox: list[int]) -> dict[str, Any]:
    kind = "icon" if record.get("kind") == "icon" or record.get("crop_mode") in {
        "alpha_isolation",
        "background_preserved",
    } else str(record.get("kind", "picture"))
    scale = ICON_SCALE if kind == "icon" else PICTURE_SCALE
    return {
        "element_id": str(record.get("element_id")),
        "kind": kind,
        "source_path": str(source),
        "source_sha256": file_sha256(source),
        "source_bbox": bbox,
        "padding": record.get("padding", 0),
        "crop_mode": record.get("crop_mode", record.get("processor")),
        "background_handling": record.get("background_handling"),
        "fallback_reason": record.get("fallback_reason"),
        "alpha_mask_sha256": record.get("alpha_mask_sha256"),
        "asset_path": str(asset),
        "asset_sha256": file_sha256(asset),
        "review_scale": scale,
    }


def _manifest_hash(manifest: list[dict[str, Any]]) -> str:
    return canonical_json_sha256(
        {
            "version": MANIFEST_VERSION,
            "renderer": {
                "background": "#00FF00",
                "roi_outline": "#FF00FF",
                "icon_scale": ICON_SCALE,
                "picture_scale": PICTURE_SCALE,
                "context_scale": CONTEXT_SCALE,
                "panel": [PANEL_WIDTH, PANEL_HEIGHT],
                "label_height": LABEL_HEIGHT,
            },
            "assets": manifest,
        }
    )


def render_asset_review(spec: dict[str, Any], output: Path | str) -> dict[str, Any]:
    records = _asset_records(spec)
    if not records:
        raise ToolError("MISSING_REQUIRED_FIELD", "assets", "no extracted assets")
    manifest: list[dict[str, Any]] = []
    rows: list[tuple[dict[str, Any], Image.Image, Image.Image, Image.Image]] = []
    for index, record in enumerate(records):
        source_path = Path(str(record.get("source_path", ""))).expanduser().resolve()
        asset_path = Path(str(record.get("asset_path", ""))).expanduser().resolve()
        bbox = record.get("source_bbox")
        if not source_path.is_file() or not asset_path.is_file():
            raise ToolError("SPEC_INVALID", f"assets[{index}]", "source or asset missing")
        if record.get("asset_sha256") != file_sha256(asset_path):
            raise ToolError("ASSET_HASH_MISMATCH", f"assets[{index}]", "stale asset")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(type(value) is not int for value in bbox)
            or bbox[2] <= 0
            or bbox[3] <= 0
        ):
            raise ToolError("SPEC_INVALID", f"assets[{index}].source_bbox", "expected integer XYWH")
        x, y, width, height = bbox
        with Image.open(source_path) as opened:
            source = opened.convert("RGB")
            if x < 0 or y < 0 or x + width > source.width or y + height > source.height:
                raise ToolError("SPEC_INVALID", f"assets[{index}].source_bbox", "crop exceeds source")
            crop = source.crop((x, y, x + width, y + height))
            margin_x = max(20, math.ceil(width / 2))
            margin_y = max(20, math.ceil(height / 2))
            left, top = max(0, x - margin_x), max(0, y - margin_y)
            right, bottom = min(source.width, x + width + margin_x), min(source.height, y + height + margin_y)
            context = source.crop((left, top, right, bottom))
            draw = ImageDraw.Draw(context)
            draw.rectangle((x - left, y - top, x - left + width - 1, y - top + height - 1), outline=ROI_COLOR)
        with Image.open(asset_path) as opened_asset:
            asset = opened_asset.copy()
        entry = _manifest_entry(record, source_path, asset_path, bbox)
        manifest.append(entry)
        rows.append((entry, context, crop, asset))

    manifest_sha256 = _manifest_hash(manifest)
    destination = Path(output).expanduser().resolve()
    reused = _cached_manifest(destination) == manifest_sha256
    if not reused:
        width = MARGIN * 2 + PANEL_WIDTH * 3
        row_height = PANEL_HEIGHT + LABEL_HEIGHT + MARGIN
        sheet = Image.new("RGB", (width, MARGIN + row_height * len(rows)), "white")
        draw = ImageDraw.Draw(sheet)
        for row, (entry, context, crop, asset) in enumerate(rows):
            top = MARGIN + row * row_height
            icon = entry["kind"] == "icon"
            panels = (
                ("context+bbox", context, CONTEXT_SCALE, False),
                (f"source {entry['review_scale']}x", crop, entry["review_scale"], icon),
                (f"asset on green {entry['review_scale']}x", asset, entry["review_scale"], icon),
            )
            for column, (label, image, scale, nearest) in enumerate(panels):
                left = MARGIN + column * PANEL_WIDTH
                panel_top = top + LABEL_HEIGHT
                panel = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), GREEN if column == 2 else "#EEEEEE")
                fitted = _fit_scaled(image, scale, (PANEL_WIDTH - 20, PANEL_HEIGHT - 20), icon=nearest)
                position = ((PANEL_WIDTH - fitted.width) // 2, (PANEL_HEIGHT - fitted.height) // 2)
                if fitted.mode == "RGBA":
                    panel.paste(fitted.convert("RGB"), position, fitted.getchannel("A"))
                else:
                    panel.paste(fitted.convert("RGB"), position)
                sheet.paste(panel, (left, panel_top))
                draw.text((left + 4, top + 4), f"{entry['element_id']} · {label}", fill="black")
        destination.parent.mkdir(parents=True, exist_ok=True)
        metadata = PngInfo()
        metadata.add_text(MANIFEST_METADATA_KEY, manifest_sha256)
        sheet.save(destination, format="PNG", pnginfo=metadata)
    return {
        "valid": True,
        "spec_sha256": canonical_json_sha256(spec),
        "path": str(destination),
        "sha256": file_sha256(destination),
        "manifest_sha256": manifest_sha256,
        "manifest": manifest,
        "items": [
            {"element_id": item["element_id"], "kind": item["kind"], "review_scale": item["review_scale"]}
            for item in manifest
        ],
        "item_count": len(manifest),
        "reused": reused,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = render_asset_review(load_schema_v2(args.spec), args.output)
        if args.report:
            atomic_write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ToolError, OSError) as exc:
        error = exc if isinstance(exc, ToolError) else ToolError("SPEC_INVALID", "$", str(exc))
        report = {"valid": False, "errors": [error.as_dict()]}
        if args.report:
            atomic_write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
