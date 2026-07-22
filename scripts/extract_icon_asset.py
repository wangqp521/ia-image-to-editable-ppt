#!/usr/bin/env python3
"""Create one deterministic icon asset from an explicit XYWH source crop."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError


ALLOWED_CROP_MODES = {"alpha_isolation", "background_preserved"}
MAX_TOLERANCE = 64


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_bbox(
    bbox_xywh: tuple[int, int, int, int],
    source_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    if len(bbox_xywh) != 4 or any(type(value) is not int for value in bbox_xywh):
        raise ValueError("bbox_xywh must contain four integers")
    x, y, width, height = bbox_xywh
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ValueError("bbox_xywh must have non-negative origin and positive size")
    if x + width > source_size[0] or y + height > source_size[1]:
        raise ValueError("bbox_xywh must stay inside source image")
    return x, y, width, height


def _validate_output_path(output_path: Path) -> None:
    if output_path.suffix.lower() != ".png":
        raise ValueError("output must be a PNG inside assets/icons")
    if output_path.parent.name != "icons" or output_path.parent.parent.name != "assets":
        raise ValueError("output must be inside an assets/icons directory")


def _corner_palette(crop: Image.Image) -> tuple[tuple[int, int, int], ...]:
    patch = max(1, min(3, crop.width // 4, crop.height // 4))
    origins = (
        (0, 0),
        (crop.width - patch, 0),
        (0, crop.height - patch),
        (crop.width - patch, crop.height - patch),
    )
    colors: set[tuple[int, int, int]] = set()
    for origin_x, origin_y in origins:
        for y in range(origin_y, origin_y + patch):
            for x in range(origin_x, origin_x + patch):
                colors.add(crop.getpixel((x, y))[:3])
    return tuple(sorted(colors))


def _near_palette(
    rgb: tuple[int, int, int],
    palette: Iterable[tuple[int, int, int]],
    tolerance: int,
) -> bool:
    limit = tolerance * tolerance
    return any(
        sum((rgb[channel] - color[channel]) ** 2 for channel in range(3)) <= limit
        for color in palette
    )


def _edge_connected_background(crop: Image.Image, tolerance: int) -> bytearray:
    width, height = crop.size
    palette = _corner_palette(crop)
    candidates = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            pixel = crop.getpixel((x, y))
            if pixel[3] == 0 or _near_palette(pixel[:3], palette, tolerance):
                candidates[y * width + x] = 1

    background = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque()

    def enqueue(x: int, y: int) -> None:
        offset = y * width + x
        if candidates[offset] and not background[offset]:
            background[offset] = 1
            queue.append((x, y))

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)
    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)

    while queue:
        x, y = queue.popleft()
        if x > 0:
            enqueue(x - 1, y)
        if x + 1 < width:
            enqueue(x + 1, y)
        if y > 0:
            enqueue(x, y - 1)
        if y + 1 < height:
            enqueue(x, y + 1)
    return background


def _touching_edges(
    foreground_bbox: tuple[int, int, int, int],
    size: tuple[int, int],
) -> dict[str, bool]:
    left, top, right, bottom = foreground_bbox
    return {
        "top": top == 0,
        "right": right == size[0],
        "bottom": bottom == size[1],
        "left": left == 0,
    }


def _alpha_isolated_asset(
    crop: Image.Image,
    tolerance: int,
) -> tuple[Image.Image, dict[str, Any]]:
    background = _edge_connected_background(crop, tolerance)
    alpha = bytearray(crop.getchannel("A").tobytes())
    for offset, is_background in enumerate(background):
        if is_background:
            alpha[offset] = 0

    alpha_image = Image.frombytes("L", crop.size, bytes(alpha))
    minimum, maximum = alpha_image.getextrema()
    if maximum == 0:
        raise ValueError("alpha_isolation produced no visible foreground; check the bbox")
    if minimum == 255:
        raise ValueError("alpha_isolation produced no transparent background; check the bbox")
    foreground_bbox = alpha_image.getbbox()
    if foreground_bbox is None:
        raise ValueError("alpha_isolation produced no visible foreground; check the bbox")
    touches_edge = _touching_edges(foreground_bbox, crop.size)
    touched = [edge for edge, touching in touches_edge.items() if touching]
    if touched:
        raise ValueError(
            "visible foreground touches crop edge: " + ", ".join(touched) + "; expand the bbox"
        )

    asset = crop.copy()
    asset.putalpha(alpha_image)
    metadata = {
        "alpha_mask_sha256": _bytes_sha256(bytes(alpha)),
        "visible_pixels": sum(1 for value in alpha if value > 0),
        "alpha_extrema": [minimum, maximum],
        "foreground_bbox": list(foreground_bbox),
        "touches_edge": touches_edge,
    }
    return asset, metadata


def extract_icon_asset(
    source_path: Path | str,
    output_path: Path | str,
    bbox_xywh: tuple[int, int, int, int],
    *,
    icon_id: str,
    crop_mode: str,
    tolerance: int = 24,
) -> dict[str, Any]:
    """Extract one icon without changing any source-crop RGB value."""
    raw_source = Path(source_path).expanduser()
    if raw_source.is_symlink():
        raise ValueError("source_path must not be a symbolic link")
    source = raw_source.resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("source_path must be a readable image file")
    _validate_output_path(output)
    if not isinstance(icon_id, str) or not icon_id.strip():
        raise ValueError("icon_id must be a non-empty string")
    if crop_mode not in ALLOWED_CROP_MODES:
        raise ValueError("crop_mode must be alpha_isolation or background_preserved")
    if type(tolerance) is not int or tolerance < 0 or tolerance > MAX_TOLERANCE:
        raise ValueError(f"tolerance must be an integer from 0 to {MAX_TOLERANCE}")

    with Image.open(source) as opened:
        opened.load()
        bbox = _validate_bbox(bbox_xywh, opened.size)
        x, y, width, height = bbox
        crop = opened.convert("RGBA").crop((x, y, x + width, y + height))

    if crop_mode == "alpha_isolation":
        asset, mode_metadata = _alpha_isolated_asset(crop, tolerance)
    else:
        asset = crop.convert("RGB")
        mode_metadata = {
            "alpha_mask_sha256": None,
            "visible_pixels": width * height,
            "alpha_extrema": [255, 255],
            "foreground_bbox": [0, 0, width, height],
            "touches_edge": {"top": True, "right": True, "bottom": True, "left": True},
        }

    if asset.convert("RGB").tobytes() != crop.convert("RGB").tobytes():
        raise RuntimeError("internal error: icon RGB values changed")
    output.parent.mkdir(parents=True, exist_ok=True)
    asset.save(output, format="PNG")
    return {
        "ok": True,
        "icon_id": icon_id,
        "crop_mode": crop_mode,
        "source": str(source),
        "source_sha256": _file_sha256(source),
        "bbox_format": "xywh",
        "source_bbox": list(bbox),
        "output": str(output),
        "asset_sha256": _file_sha256(output),
        "size": [width, height],
        "rgb_preserved": True,
        **mode_metadata,
    }


def _parse_bbox_xywh(value: str) -> tuple[int, int, int, int]:
    try:
        parts = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox must be X,Y,W,H integers") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be X,Y,W,H integers")
    return parts


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract one lossless icon asset from an explicit XYWH crop."
    )
    parser.add_argument("source", type=Path, help="Clean reference image")
    parser.add_argument("--icon-id", required=True, help="Stable icon identifier")
    parser.add_argument(
        "--bbox-xywh",
        required=True,
        type=_parse_bbox_xywh,
        metavar="X,Y,W,H",
        help="Source crop including a background margin",
    )
    parser.add_argument(
        "--crop-mode",
        required=True,
        choices=sorted(ALLOWED_CROP_MODES),
    )
    parser.add_argument("--tolerance", type=int, default=24)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = extract_icon_asset(
            args.source,
            args.output,
            args.bbox_xywh,
            icon_id=args.icon_id,
            crop_mode=args.crop_mode,
            tolerance=args.tolerance,
        )
    except (OSError, ValueError, RuntimeError, UnidentifiedImageError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
