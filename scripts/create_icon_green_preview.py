#!/usr/bin/env python3
"""Render the current page's final RGBA icon assets once on a green canvas."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, UnidentifiedImageError


GREEN = (0, 255, 0)
SCALE = 4
MARGIN = 16
GAP = 16
LABEL_HEIGHT = 24
RESAMPLING = Image.Resampling.NEAREST


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_icon(item: dict[str, Any], index: int) -> tuple[str, Image.Image]:
    icon_id = item.get("icon_id")
    if not isinstance(icon_id, str) or not icon_id.strip():
        raise ValueError(f"icon {index + 1} is missing icon_id")
    if item.get("crop_mode") != "alpha_isolation":
        raise ValueError(f"icon {index + 1} crop_mode must be alpha_isolation")

    asset_path_value = item.get("asset_path")
    declared_sha256 = item.get("asset_sha256")
    if not isinstance(asset_path_value, str):
        raise ValueError(f"icon {index + 1} is missing asset_path")
    if not isinstance(declared_sha256, str) or len(declared_sha256) != 64:
        raise ValueError(f"icon {index + 1} is missing a valid asset_sha256")

    asset_path = Path(asset_path_value).expanduser()
    if (
        not asset_path.is_absolute()
        or asset_path.is_symlink()
        or not asset_path.is_file()
    ):
        raise ValueError(f"icon {index + 1} asset_path must be an absolute readable file")
    asset_path = asset_path.resolve()
    if _sha256(asset_path).lower() != declared_sha256.lower():
        raise ValueError(f"icon {index + 1} asset_sha256 mismatch")

    with Image.open(asset_path) as image:
        image.load()
        if image.mode != "RGBA":
            raise ValueError(f"icon {index + 1} asset must use RGBA mode")
        alpha = image.getchannel("A")
        minimum, maximum = alpha.getextrema()
        if minimum != 0 or maximum == 0:
            raise ValueError(
                f"icon {index + 1} asset must contain transparent background and visible foreground"
            )
        return icon_id, image.copy()


def _panel(icon_id: str, asset: Image.Image) -> Image.Image:
    scaled = asset.resize((asset.width * SCALE, asset.height * SCALE), RESAMPLING)
    panel = Image.new("RGB", (scaled.width, scaled.height + LABEL_HEIGHT), GREEN)
    draw = ImageDraw.Draw(panel)
    draw.rectangle((0, 0, panel.width - 1, LABEL_HEIGHT - 1), fill=(0, 0, 0))
    draw.text((5, 5), icon_id, fill=(255, 255, 255))
    panel.paste(
        scaled.convert("RGB"),
        (0, LABEL_HEIGHT),
        scaled.getchannel("A"),
    )
    return panel


def create_icon_green_preview(
    spec_path: Path | str,
    output_path: Path | str,
) -> dict[str, Any]:
    spec_path = Path(spec_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    icons = spec.get("modules", {}).get("icons", {}).get("icons")
    if not isinstance(icons, list) or not icons:
        raise ValueError("modules.icons.icons must contain at least one icon")

    loaded = [_load_icon(item, index) for index, item in enumerate(icons)]
    panels = [_panel(icon_id, asset) for icon_id, asset in loaded]
    columns = math.ceil(math.sqrt(len(panels)))
    rows = math.ceil(len(panels) / columns)
    cell_width = max(panel.width for panel in panels)
    cell_height = max(panel.height for panel in panels)
    canvas = Image.new(
        "RGB",
        (
            MARGIN * 2 + columns * cell_width + (columns - 1) * GAP,
            MARGIN * 2 + rows * cell_height + (rows - 1) * GAP,
        ),
        GREEN,
    )
    for index, panel in enumerate(panels):
        column = index % columns
        row = index // columns
        x = MARGIN + column * (cell_width + GAP) + (cell_width - panel.width) // 2
        y = MARGIN + row * (cell_height + GAP) + (cell_height - panel.height) // 2
        canvas.paste(panel, (x, y))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG")
    return {
        "ok": True,
        "icon_count": len(loaded),
        "output": str(output_path),
        "output_sha256": _sha256(output_path),
        "background": "#00FF00",
        "scale": SCALE,
        "icon_ids": [icon_id for icon_id, _ in loaded],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show the current page's final alpha-isolated icons once on green."
    )
    parser.add_argument("spec", type=Path, help="Current page-reconstruction.json")
    parser.add_argument("--output", required=True, type=Path, help="Output PNG path")
    args = parser.parse_args(argv)
    try:
        result = create_icon_green_preview(args.spec, args.output)
    except (OSError, ValueError, json.JSONDecodeError, UnidentifiedImageError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
