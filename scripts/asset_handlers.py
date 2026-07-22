"""Deterministic handlers for non-icon raster assets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from lib.error_codes import ToolError
from lib.geometry import validate_xywh
from lib.hashing import bytes_sha256, file_sha256


@dataclass(frozen=True)
class AssetJob:
    element_id: str
    kind: str
    processor: str
    source_path: Path
    source_bbox: tuple[int, int, int, int]
    output_path: Path
    field_path: str
    padding: int = 0
    tolerance: int | None = None
    mask_path: Path | None = None


@dataclass(frozen=True)
class AssetResult:
    element_id: str
    processor: str
    output_path: Path
    asset_sha256: str
    alpha_mask_sha256: str | None
    width: int
    height: int
    source_bbox: tuple[int, int, int, int]
    touches_edge: dict[str, bool]
    source_sha256: str

    def as_report(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "processor": self.processor,
            "source_bbox": list(self.source_bbox),
            "output": str(self.output_path),
            "asset_sha256": self.asset_sha256,
            "alpha_mask_sha256": self.alpha_mask_sha256,
            "final_width": self.width,
            "final_height": self.height,
            "touches_edge": self.touches_edge,
            "source_sha256": self.source_sha256,
            "status": "succeeded",
        }


def _load_crop(job: AssetJob) -> Image.Image:
    if job.source_path.is_symlink() or not job.source_path.is_file():
        raise ToolError("SPEC_INVALID", job.field_path, "source must be a readable file")
    try:
        with Image.open(job.source_path) as opened:
            opened.load()
            x, y, width, height = validate_xywh(job.source_bbox, job.field_path)
            values = (int(x), int(y), int(width), int(height))
            if values != job.source_bbox:
                raise ToolError("SPEC_INVALID", job.field_path, "bbox must contain integers")
            left = values[0] - job.padding
            top = values[1] - job.padding
            right = values[0] + values[2] + job.padding
            bottom = values[1] + values[3] + job.padding
            if left < 0 or top < 0 or right > opened.width or bottom > opened.height:
                raise ToolError("BBOX_OUT_OF_RANGE", job.field_path, "crop exceeds source")
            mode = "RGBA" if opened.mode == "RGBA" else "RGB"
            return opened.convert(mode).crop((left, top, right, bottom))
    except UnidentifiedImageError as exc:
        raise ToolError("SPEC_INVALID", job.field_path, "source is not an image") from exc


def _save_result(job: AssetJob, image: Image.Image, alpha_hash: str | None) -> AssetResult:
    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(job.output_path, format="PNG")
    return AssetResult(
        element_id=job.element_id,
        processor=job.processor,
        output_path=job.output_path,
        asset_sha256=file_sha256(job.output_path),
        alpha_mask_sha256=alpha_hash,
        width=image.width,
        height=image.height,
        source_bbox=job.source_bbox,
        touches_edge={"top": True, "right": True, "bottom": True, "left": True},
        source_sha256=file_sha256(job.source_path),
    )


def extract_source_patch(job: AssetJob) -> AssetResult:
    crop = _load_crop(job)
    return _save_result(job, crop, None)


def extract_background_preserved(job: AssetJob) -> AssetResult:
    crop = _load_crop(job).convert("RGB")
    return _save_result(job, crop, None)


def extract_with_explicit_mask(job: AssetJob) -> AssetResult:
    crop = _load_crop(job).convert("RGBA")
    if job.mask_path is None or job.mask_path.is_symlink() or not job.mask_path.is_file():
        raise ToolError("MISSING_REQUIRED_FIELD", f"{job.field_path}.mask_path", "mask required")
    try:
        with Image.open(job.mask_path) as opened:
            opened.load()
            mask = opened.getchannel("A") if opened.mode == "RGBA" else opened.convert("L")
    except (OSError, UnidentifiedImageError) as exc:
        raise ToolError("SPEC_INVALID", f"{job.field_path}.mask_path", "unreadable mask") from exc
    if mask.size != crop.size:
        raise ToolError("SPEC_INVALID", f"{job.field_path}.mask_path", "mask size mismatch")
    minimum, maximum = mask.getextrema()
    if minimum == maximum:
        raise ToolError("ALPHA_EXTRACTION_UNSAFE", f"{job.field_path}.mask_path", "mask must contain visible and transparent pixels")
    alpha_bytes = mask.tobytes()
    crop.putalpha(mask)
    return _save_result(job, crop, bytes_sha256(alpha_bytes))
