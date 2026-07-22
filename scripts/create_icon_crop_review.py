#!/usr/bin/env python3
"""Render source crops beside final icon assets on a uniform green screen."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo


GREEN = (0, 255, 0)
ROI_COLOR = (255, 0, 255)
SCALE = 4
CONTEXT_SCALE = 2
GAP = 16
MARGIN = 16
LABEL_HEIGHT = 18
RESAMPLING = getattr(Image, "Resampling", Image).NEAREST
MANIFEST_VERSION = 1
MANIFEST_METADATA_KEY = "icon_manifest_sha256"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _source_descriptor(
    item: dict[str, Any], index: int, base_dir: Path
) -> dict[str, Any]:
    source_path_value = item.get("source_path")
    source_bbox = item.get("source_bbox")
    padding = item.get("padding")
    if not isinstance(source_path_value, str):
        raise ValueError(f"icon {index + 1} is missing source_path")
    if (
        not isinstance(source_bbox, list)
        or len(source_bbox) != 4
        or any(type(value) is not int for value in source_bbox)
        or source_bbox[2] <= 0
        or source_bbox[3] <= 0
    ):
        raise ValueError(f"icon {index + 1} has invalid source_bbox; expected XYWH")
    if not isinstance(padding, int) or padding < 0:
        raise ValueError(f"icon {index + 1} has invalid padding")

    source_path = _resolve_path(source_path_value, base_dir)
    with Image.open(source_path) as image:
        image.load()
        left = source_bbox[0] - padding
        top = source_bbox[1] - padding
        right = source_bbox[0] + source_bbox[2] + padding
        bottom = source_bbox[1] + source_bbox[3] + padding
        if left < 0 or top < 0 or right > image.width or bottom > image.height:
            raise ValueError(f"icon {index + 1} source crop exceeds source bounds")
    return {
        "path": str(source_path),
        "sha256": _sha256(source_path),
        "source_bbox": source_bbox,
        "padding": padding,
    }


def _icon_manifest_sha256(entries: list[dict[str, Any]]) -> str:
    payload = {
        "version": MANIFEST_VERSION,
        "renderer": {
            "background": "#00FF00",
            "roi_outline": "#FF00FF",
            "scale": SCALE,
            "context_scale": CONTEXT_SCALE,
            "gap": GAP,
            "margin": MARGIN,
            "label_height": LABEL_HEIGHT,
            "resampling": "nearest",
        },
        "icons": entries,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cached_manifest_sha256(output_path: Path) -> str | None:
    if not output_path.is_file():
        return None
    try:
        with Image.open(output_path) as image:
            image.verify()
        with Image.open(output_path) as image:
            value = image.info.get(MANIFEST_METADATA_KEY)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, str) and len(value) == 64 else None


def _load_source_evidence(
    item: dict[str, Any], index: int, base_dir: Path
) -> tuple[Image.Image, Image.Image]:
    source_path_value = item.get("source_path")
    source_bbox = item.get("source_bbox")
    padding = item.get("padding")
    if not isinstance(source_path_value, str):
        raise ValueError(f"icon {index + 1} is missing source_path")
    if (
        not isinstance(source_bbox, list)
        or len(source_bbox) != 4
        or any(type(value) is not int for value in source_bbox)
        or source_bbox[2] <= 0
        or source_bbox[3] <= 0
    ):
        raise ValueError(f"icon {index + 1} has invalid source_bbox; expected XYWH")
    if not isinstance(padding, int) or padding < 0:
        raise ValueError(f"icon {index + 1} has invalid padding")

    source_path = _resolve_path(source_path_value, base_dir)
    with Image.open(source_path) as image:
        image.load()
        left = source_bbox[0] - padding
        top = source_bbox[1] - padding
        right = source_bbox[0] + source_bbox[2] + padding
        bottom = source_bbox[1] + source_bbox[3] + padding
        if left < 0 or top < 0 or right > image.width or bottom > image.height:
            raise ValueError(f"icon {index + 1} source crop exceeds source bounds")
        source = image.convert("RGB")
        crop = source.crop((left, top, right, bottom))
        margin_x = max(4, math.ceil((right - left) / 2))
        margin_y = max(4, math.ceil((bottom - top) / 2))
        context_left = max(0, left - margin_x)
        context_top = max(0, top - margin_y)
        context_right = min(source.width, right + margin_x)
        context_bottom = min(source.height, bottom + margin_y)
        context = source.crop((context_left, context_top, context_right, context_bottom))
        draw = ImageDraw.Draw(context)
        draw.rectangle(
            (
                left - context_left,
                top - context_top,
                right - context_left - 1,
                bottom - context_top - 1,
            ),
            outline=ROI_COLOR,
            width=1,
        )
        return context, crop


def _load_asset(item: dict[str, Any], index: int, base_dir: Path) -> tuple[Image.Image, Path, str]:
    asset_path_value = item.get("asset_path")
    declared_sha256 = item.get("asset_sha256")
    crop_mode = item.get("crop_mode")
    if not isinstance(asset_path_value, str):
        raise ValueError(f"icon {index + 1} is missing asset_path")
    if not isinstance(declared_sha256, str) or len(declared_sha256) != 64:
        raise ValueError(f"icon {index + 1} is missing a valid asset_sha256")
    asset_path = _resolve_path(asset_path_value, base_dir)
    actual_sha256 = _sha256(asset_path)
    if declared_sha256.lower() != actual_sha256:
        raise ValueError(f"icon {index + 1} asset_sha256 mismatch")

    with Image.open(asset_path) as image:
        image.load()
        if crop_mode == "alpha_isolation":
            if image.mode != "RGBA":
                raise ValueError(f"icon {index + 1} alpha_isolation asset must be RGBA")
            alpha = image.getchannel("A")
            minimum, maximum = alpha.getextrema()
            if minimum != 0 or maximum == 0:
                raise ValueError(
                    f"icon {index + 1} alpha_isolation asset must contain transparent background and visible foreground"
                )
            asset = image.copy()
        elif crop_mode == "background_preserved":
            if image.mode == "RGBA":
                if image.getchannel("A").getextrema() != (255, 255):
                    raise ValueError(
                        f"icon {index + 1} background_preserved asset must be RGB or fully opaque RGBA"
                    )
                asset = image.convert("RGB")
            elif image.mode == "RGB":
                asset = image.copy()
            else:
                raise ValueError(
                    f"icon {index + 1} background_preserved asset must be RGB or fully opaque RGBA"
                )
        else:
            raise ValueError(
                f"icon {index + 1} crop_mode must be alpha_isolation or background_preserved"
            )
    return asset, asset_path, actual_sha256


def _asset_on_green(asset: Image.Image) -> Image.Image:
    scaled = asset.resize((asset.width * SCALE, asset.height * SCALE), RESAMPLING)
    preview = Image.new("RGB", scaled.size, GREEN)
    if scaled.mode == "RGBA":
        preview.paste(scaled.convert("RGB"), (0, 0), scaled.getchannel("A"))
    else:
        preview.paste(scaled.convert("RGB"), (0, 0))
    return preview


def _labeled_panel(panel: Image.Image, label: str) -> Image.Image:
    preview = Image.new("RGB", (panel.width, panel.height + LABEL_HEIGHT), GREEN)
    draw = ImageDraw.Draw(preview)
    draw.rectangle((0, 0, panel.width - 1, LABEL_HEIGHT - 1), fill=(0, 0, 0))
    draw.text((4, 3), label, fill=(255, 255, 255))
    preview.paste(panel.convert("RGB"), (0, LABEL_HEIGHT))
    return preview


def _source_and_asset_preview(
    context: Image.Image,
    source: Image.Image,
    asset: Image.Image,
    index: int,
    icon_id: str,
) -> Image.Image:
    if source.size != asset.size:
        raise ValueError(f"icon {index + 1} asset size must match source crop")
    context_scaled = context.resize(
        (context.width * CONTEXT_SCALE, context.height * CONTEXT_SCALE), RESAMPLING
    )
    source_scaled = source.resize(
        (source.width * SCALE, source.height * SCALE), RESAMPLING
    )
    context_panel = _labeled_panel(context_scaled, f"{icon_id} | context+bbox")
    source_panel = _labeled_panel(source_scaled, "source crop")
    asset_panel = _labeled_panel(_asset_on_green(asset), "asset on green")
    preview = Image.new(
        "RGB",
        (
            context_panel.width + GAP + source_panel.width + GAP + asset_panel.width,
            max(context_panel.height, source_panel.height, asset_panel.height),
        ),
        GREEN,
    )
    preview.paste(context_panel, (0, 0))
    preview.paste(source_panel, (context_panel.width + GAP, 0))
    preview.paste(
        asset_panel,
        (context_panel.width + GAP + source_panel.width + GAP, 0),
    )
    return preview


def create_icon_crop_review(spec_path: Path, output_path: Path) -> dict[str, Any]:
    spec_path = Path(spec_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    icons = spec.get("modules", {}).get("icons", {}).get("icons")
    if not isinstance(icons, list) or not icons:
        raise ValueError("modules.icons.icons must contain at least one icon")

    prepared: list[dict[str, Any]] = []
    evidence_assets: list[dict[str, str]] = []
    icon_ids: list[str] = []
    transparent_icon_count = 0
    background_preserved_icon_count = 0
    manifest_entries: list[dict[str, Any]] = []
    for index, item in enumerate(icons):
        if not isinstance(item, dict):
            raise ValueError(f"icon {index + 1} must be an object")
        icon_id = item.get("icon_id")
        if not isinstance(icon_id, str) or not icon_id.strip():
            icon_id = f"icon-{index + 1}"
        source_descriptor = _source_descriptor(item, index, spec_path.parent)
        asset, asset_path, actual_sha256 = _load_asset(item, index, spec_path.parent)
        crop_mode = item.get("crop_mode")
        prepared.append(
            {
                "index": index,
                "item": item,
                "icon_id": icon_id,
                "asset": asset,
            }
        )
        icon_ids.append(icon_id)
        evidence_assets.append({"path": str(asset_path), "sha256": actual_sha256})
        manifest_entries.append(
            {
                "icon_id": icon_id,
                "source": source_descriptor,
                "crop_mode": crop_mode,
                "asset_path": str(asset_path),
                "asset_sha256": actual_sha256,
                "background_handling": item.get("background_handling"),
                "fallback_reason": item.get("fallback_reason"),
                "alpha_mask_sha256": item.get("alpha_mask_sha256"),
            }
        )
        if crop_mode == "alpha_isolation":
            transparent_icon_count += 1
        else:
            background_preserved_icon_count += 1

    manifest_sha256 = _icon_manifest_sha256(manifest_entries)
    reused = _cached_manifest_sha256(output_path) == manifest_sha256
    if not reused:
        previews: list[Image.Image] = []
        for entry in prepared:
            context, source = _load_source_evidence(
                entry["item"], entry["index"], spec_path.parent
            )
            previews.append(
                _source_and_asset_preview(
                    context,
                    source,
                    entry["asset"],
                    entry["index"],
                    entry["icon_id"],
                )
            )

        columns = math.ceil(math.sqrt(len(previews)))
        rows = math.ceil(len(previews) / columns)
        cell_width = max(image.width for image in previews) + GAP
        cell_height = max(image.height for image in previews) + GAP
        canvas = Image.new(
            "RGB",
            (MARGIN * 2 + columns * cell_width, MARGIN * 2 + rows * cell_height),
            GREEN,
        )
        for index, preview in enumerate(previews):
            column = index % columns
            row = index // columns
            x = MARGIN + column * cell_width + (cell_width - preview.width) // 2
            y = MARGIN + row * cell_height + (cell_height - preview.height) // 2
            canvas.paste(preview, (x, y))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        pnginfo = PngInfo()
        pnginfo.add_text(MANIFEST_METADATA_KEY, manifest_sha256)
        canvas.save(output_path, pnginfo=pnginfo)
    return {
        "ok": True,
        "icon_count": len(prepared),
        "transparent_icon_count": transparent_icon_count,
        "background_preserved_icon_count": background_preserved_icon_count,
        "spec_sha256": _sha256(spec_path),
        "icon_manifest_sha256": manifest_sha256,
        "reused": reused,
        "assets": evidence_assets,
        "output": str(output_path),
        "background": "#00FF00",
        "scale": SCALE,
        "context_scale": CONTEXT_SCALE,
        "roi_outline": "#FF00FF",
        "panels": ["context_with_bbox", "source_crop", "asset_on_green"],
        "icon_ids": icon_ids,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render ROI context, source crop, and final icon asset evidence."
    )
    parser.add_argument("spec", type=Path, help="Current page-reconstruction.json")
    parser.add_argument("--output", required=True, type=Path, help="Output PNG path")
    args = parser.parse_args(argv)
    try:
        result = create_icon_crop_review(args.spec, args.output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
