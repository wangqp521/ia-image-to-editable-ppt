#!/usr/bin/env python3
"""Create one deterministic icon asset from an explicit XYWH source crop."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError


ALLOWED_CROP_MODES = {"alpha_isolation", "background_preserved"}
MAX_TOLERANCE = 64
EDGE_GUARD_TOLERANCE = 12
ALGORITHM_VERSION = "edge-connected-v2"


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


def _edge_models(
    crop: Image.Image,
) -> dict[str, tuple[tuple[int, int, int], ...]]:
    patch = max(1, min(3, crop.width // 4, crop.height // 4))
    top_bottom_x = (*range(patch), *range(crop.width - patch, crop.width))
    left_right_y = (*range(patch), *range(crop.height - patch, crop.height))
    return {
        "top": tuple(
            sorted(
                {
                    crop.getpixel((x, y))[:3]
                    for y in range(patch)
                    for x in top_bottom_x
                }
            )
        ),
        "right": tuple(
            sorted(
                {
                    crop.getpixel((x, y))[:3]
                    for x in range(crop.width - patch, crop.width)
                    for y in left_right_y
                }
            )
        ),
        "bottom": tuple(
            sorted(
                {
                    crop.getpixel((x, y))[:3]
                    for y in range(crop.height - patch, crop.height)
                    for x in top_bottom_x
                }
            )
        ),
        "left": tuple(
            sorted(
                {
                    crop.getpixel((x, y))[:3]
                    for x in range(patch)
                    for y in left_right_y
                }
            )
        ),
    }


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


def _raw_edge_foreground_risk(
    crop: Image.Image,
    models: dict[str, tuple[tuple[int, int, int], ...]],
) -> list[str]:
    width, height = crop.size
    edge_points = {
        "top": ((x, 0) for x in range(width)),
        "right": ((width - 1, y) for y in range(height)),
        "bottom": ((x, height - 1) for x in range(width)),
        "left": ((0, y) for y in range(height)),
    }
    risky: list[str] = []
    for side, points in edge_points.items():
        for x, y in points:
            pixel = crop.getpixel((x, y))
            if pixel[3] != 0 and not _near_palette(
                pixel[:3],
                models[side],
                EDGE_GUARD_TOLERANCE,
            ):
                risky.append(side)
                break
    return risky


def _edge_connected_background(
    crop: Image.Image,
    tolerance: int,
    models: dict[str, tuple[tuple[int, int, int], ...]],
) -> bytearray:
    width, height = crop.size
    background = bytearray(width * height)
    edge_points = {
        "top": ((x, 0) for x in range(width)),
        "right": ((width - 1, y) for y in range(height)),
        "bottom": ((x, height - 1) for x in range(width)),
        "left": ((0, y) for y in range(height)),
    }
    for side, points in edge_points.items():
        palette = models[side]
        candidates = bytearray(width * height)
        for y in range(height):
            for x in range(width):
                pixel = crop.getpixel((x, y))
                if pixel[3] == 0 or _near_palette(pixel[:3], palette, tolerance):
                    candidates[y * width + x] = 1
        visited = bytearray(width * height)
        queue: deque[tuple[int, int]] = deque()

        def enqueue(x: int, y: int) -> None:
            offset = y * width + x
            if candidates[offset] and not visited[offset]:
                visited[offset] = 1
                background[offset] = 1
                queue.append((x, y))

        for x, y in points:
            enqueue(x, y)
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
    models = _edge_models(crop)
    risky_edges = _raw_edge_foreground_risk(crop, models)
    if risky_edges:
        raise ValueError(
            "raw foreground may touch crop edge: "
            + ", ".join(risky_edges)
            + "; expand the bbox"
        )
    background = _edge_connected_background(crop, tolerance, models)
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


def _extract_from_loaded_source(
    source_rgba: Image.Image,
    bbox_xywh: tuple[int, int, int, int],
    *,
    crop_mode: str,
    tolerance: int,
) -> tuple[Image.Image, dict[str, Any]]:
    bbox = _validate_bbox(bbox_xywh, source_rgba.size)
    x, y, width, height = bbox
    crop = source_rgba.crop((x, y, x + width, y + height))
    if crop_mode == "alpha_isolation":
        asset, mode_metadata = _alpha_isolated_asset(crop, tolerance)
    else:
        asset = crop.convert("RGB")
        mode_metadata = {
            "alpha_mask_sha256": None,
            "visible_pixels": width * height,
            "alpha_extrema": [255, 255],
            "foreground_bbox": [0, 0, width, height],
            "touches_edge": {
                "top": True,
                "right": True,
                "bottom": True,
                "left": True,
            },
        }
    if asset.convert("RGB").tobytes() != crop.convert("RGB").tobytes():
        raise RuntimeError("internal error: icon RGB values changed")
    return asset, {
        "source_bbox": list(bbox),
        "size": [width, height],
        "rgb_preserved": True,
        **mode_metadata,
    }


def _save_png_atomically(asset: Image.Image, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output.stem}-",
        suffix=".png",
        dir=output.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        asset.save(temporary, format="PNG")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


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
        source_rgba = opened.convert("RGBA")
    asset, metadata = _extract_from_loaded_source(
        source_rgba,
        bbox_xywh,
        crop_mode=crop_mode,
        tolerance=tolerance,
    )
    _save_png_atomically(asset, output)
    return {
        "ok": True,
        "icon_id": icon_id,
        "crop_mode": crop_mode,
        "algorithm_version": ALGORITHM_VERSION,
        "source": str(source),
        "source_sha256": _file_sha256(source),
        "bbox_format": "xywh",
        "output": str(output),
        "asset_sha256": _file_sha256(output),
        **metadata,
    }


def extract_icon_assets_from_spec(
    spec_path: Path | str,
    output_dir: Path | str,
    *,
    tolerance: int = 24,
) -> dict[str, Any]:
    """Extract every declared icon while decoding the shared source once."""
    if type(tolerance) is not int or tolerance < 0 or tolerance > MAX_TOLERANCE:
        raise ValueError(f"tolerance must be an integer from 0 to {MAX_TOLERANCE}")
    spec_file = Path(spec_path).expanduser().resolve()
    target_dir = Path(output_dir).expanduser().resolve()
    if (
        target_dir.name != "icons"
        or target_dir.parent.name != "assets"
        or target_dir.is_symlink()
    ):
        raise ValueError("output_dir must be an assets/icons directory")
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    icons = spec.get("modules", {}).get("icons", {}).get("icons")
    if not isinstance(icons, list) or not icons:
        raise ValueError("modules.icons.icons must contain at least one icon")

    source_paths: list[Path] = []
    for index, item in enumerate(icons):
        if not isinstance(item, dict):
            raise ValueError(f"icon {index + 1} must be an object")
        raw_source = item.get("source_path")
        if not isinstance(raw_source, str):
            raise ValueError(f"icon {index + 1} is missing source_path")
        source_path = Path(raw_source).expanduser()
        if source_path.is_symlink():
            raise ValueError("source_path must not be a symbolic link")
        source_paths.append(source_path.resolve())
    source = source_paths[0]
    if any(path != source for path in source_paths[1:]):
        raise ValueError("batch icons must share one source_path")
    if not source.is_file():
        raise ValueError("source_path must be a readable image file")

    source_sha256 = _file_sha256(source)
    processor_sha256 = _file_sha256(Path(__file__).resolve())
    target_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    seen_icon_ids: set[str] = set()
    with Image.open(source) as opened:
        opened.load()
        source_rgba = opened.convert("RGBA")
    for index, item in enumerate(icons):
        icon_id = item.get("icon_id")
        try:
            if not isinstance(icon_id, str) or not icon_id.strip():
                raise ValueError(f"icon {index + 1} has invalid icon_id")
            if icon_id in seen_icon_ids:
                raise ValueError(f"duplicate icon_id: {icon_id}")
            seen_icon_ids.add(icon_id)
            crop_mode = item.get("crop_mode")
            if crop_mode not in ALLOWED_CROP_MODES:
                raise ValueError(
                    "crop_mode must be alpha_isolation or background_preserved"
                )
            source_bbox = item.get("source_bbox")
            if not isinstance(source_bbox, list):
                raise ValueError("source_bbox must be an XYWH array")
            output_value = item.get("asset_path")
            if not isinstance(output_value, str):
                raise ValueError("asset_path must be a string")
            output = Path(output_value).expanduser().resolve()
            _validate_output_path(output)
            if output.parent != target_dir:
                raise ValueError("asset_path must be inside the requested output_dir")
            asset, metadata = _extract_from_loaded_source(
                source_rgba,
                tuple(source_bbox),
                crop_mode=crop_mode,
                tolerance=tolerance,
            )
            _save_png_atomically(asset, output)
            results.append(
                {
                    "ok": True,
                    "icon_id": icon_id,
                    "crop_mode": crop_mode,
                    "algorithm_version": ALGORITHM_VERSION,
                    "source": str(source),
                    "source_sha256": source_sha256,
                    "bbox_format": "xywh",
                    "output": str(output),
                    "asset_sha256": _file_sha256(output),
                    **metadata,
                }
            )
        except (OSError, ValueError, RuntimeError, UnidentifiedImageError) as exc:
            failures.append(
                {
                    "icon_id": icon_id if isinstance(icon_id, str) else f"icon-{index + 1}",
                    "error": str(exc),
                }
            )
    return {
        "ok": not failures,
        "source": str(source),
        "source_sha256": source_sha256,
        "processor_sha256": processor_sha256,
        "algorithm_version": ALGORITHM_VERSION,
        "results": results,
        "failures": failures,
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
        description="Extract one icon or every icon declared by a page spec."
    )
    parser.add_argument("source", nargs="?", type=Path, help="Clean reference image")
    parser.add_argument("--spec", type=Path, help="Schema v2 page reconstruction spec")
    parser.add_argument("--icon-id", help="Stable icon identifier")
    parser.add_argument(
        "--bbox-xywh",
        type=_parse_bbox_xywh,
        metavar="X,Y,W,H",
        help="Source crop including a background margin",
    )
    parser.add_argument(
        "--crop-mode",
        choices=sorted(ALLOWED_CROP_MODES),
    )
    parser.add_argument("--tolerance", type=int, default=24)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.spec is not None:
            if args.source is not None or args.output_dir is None:
                raise ValueError("batch mode requires --spec and --output-dir only")
            if any(
                value is not None
                for value in (
                    args.icon_id,
                    args.bbox_xywh,
                    args.crop_mode,
                    args.output,
                )
            ):
                raise ValueError("single-icon arguments are not allowed with --spec")
            result = extract_icon_assets_from_spec(
                args.spec,
                args.output_dir,
                tolerance=args.tolerance,
            )
        else:
            if any(
                value is None
                for value in (
                    args.source,
                    args.icon_id,
                    args.bbox_xywh,
                    args.crop_mode,
                    args.output,
                )
            ):
                raise ValueError(
                    "single-icon mode requires source, --icon-id, --bbox-xywh, "
                    "--crop-mode, and --output"
                )
            if args.output_dir is not None:
                raise ValueError("--output-dir is only valid with --spec")
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
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
