#!/usr/bin/env python3
"""Overlay a labeled coordinate grid on one source image."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, UnidentifiedImageError
from PIL.PngImagePlugin import PngInfo


SLIDE_SIZE_IN = (13.333333, 7.5)
MANIFEST_VERSION = 1
MANIFEST_METADATA_KEY = "coordinate_overlay_manifest_sha256"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _draw_grid(image: Image.Image, cols: int, rows: int, labels: str) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    for col in range(cols + 1):
        x = round(col * (image.width - 1) / cols)
        draw.line((x, 0, x, image.height - 1), fill=(0, 120, 255, 128), width=1)
        if labels in {"x", "both"}:
            draw.text((min(x + 2, max(0, image.width - 38)), 2), str(x), fill=(0, 70, 180, 255))
    for row in range(rows + 1):
        y = round(row * (image.height - 1) / rows)
        draw.line((0, y, image.width - 1, y), fill=(255, 70, 70, 128), width=1)
        if labels in {"y", "both"}:
            draw.text((2, min(y + 2, max(0, image.height - 14))), str(y), fill=(180, 20, 20, 255))


def coordinate_overlay_manifest(
    source_path: Path | str,
    *,
    cols: int = 32,
    rows: int = 18,
    labels: str = "both",
) -> dict[str, Any]:
    """Return the canonical identity used to bind one coordinate overlay."""
    if cols <= 0 or rows <= 0:
        raise ValueError("cols and rows must be positive")
    if labels not in {"none", "x", "y", "both"}:
        raise ValueError("labels must be none, x, y, or both")

    source_path = Path(source_path).expanduser().resolve()
    with Image.open(source_path) as opened:
        mode = opened.mode
        has_alpha = "A" in opened.getbands()
        width, height = opened.size

    scale = min(SLIDE_SIZE_IN[0] / width, SLIDE_SIZE_IN[1] / height)
    content_width = width * scale
    content_height = height * scale
    source = {
        "path": str(source_path),
        "sha256": _sha256(source_path),
        "pixel_size": [width, height],
        "mode": mode,
        "has_alpha": has_alpha,
    }
    mapping = {
        "mode": "direct_16_9" if abs(width / height - 16 / 9) <= 0.001 else "contain",
        "slide_size_in": list(SLIDE_SIZE_IN),
        "scale_in_per_px": round(scale, 9),
        "content_size_in": [round(content_width, 6), round(content_height, 6)],
        "offset_in": [
            round((SLIDE_SIZE_IN[0] - content_width) / 2, 6),
            round((SLIDE_SIZE_IN[1] - content_height) / 2, 6),
        ],
    }
    grid = {"cols": cols, "rows": rows, "labels": labels}
    payload = {
        "version": MANIFEST_VERSION,
        "renderer": "create_coordinate_overlay.py",
        "source": source,
        "mapping": mapping,
        "grid": grid,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "source": source,
        "mapping": mapping,
        "grid": grid,
        MANIFEST_METADATA_KEY: hashlib.sha256(encoded).hexdigest(),
    }


def create_coordinate_overlay(
    source_path: Path | str,
    output_path: Path | str,
    *,
    cols: int = 32,
    rows: int = 18,
    labels: str = "both",
) -> dict[str, Any]:
    """Create a coordinate overlay and return source/mapping metadata."""
    if cols <= 0 or rows <= 0:
        raise ValueError("cols and rows must be positive")
    if labels not in {"none", "x", "y", "both"}:
        raise ValueError("labels must be none, x, y, or both")

    manifest = coordinate_overlay_manifest(
        source_path,
        cols=cols,
        rows=rows,
        labels=labels,
    )
    source_path = Path(source_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    with Image.open(source_path) as opened:
        image = opened.convert("RGBA")

    _draw_grid(image, cols, rows, labels)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pnginfo = PngInfo()
    pnginfo.add_text(
        MANIFEST_METADATA_KEY,
        manifest[MANIFEST_METADATA_KEY],
    )
    image.save(output_path, pnginfo=pnginfo)
    return {
        **manifest,
        "overlay": str(output_path),
        "overlay_sha256": _sha256(output_path),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cols", type=int, default=32)
    parser.add_argument("--rows", type=int, default=18)
    parser.add_argument("--labels", choices=("none", "x", "y", "both"), default="both")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = create_coordinate_overlay(
            args.source,
            args.output,
            cols=args.cols,
            rows=args.rows,
            labels=args.labels,
        )
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
