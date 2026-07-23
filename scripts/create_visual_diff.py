#!/usr/bin/env python3
"""Create lightweight visual-difference evidence for one source/preview pair."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat


RESAMPLING = getattr(Image, "Resampling", Image).LANCZOS
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reported_file(
    report: dict[str, Any],
    key: str,
    *,
    report_path: Path,
) -> tuple[Path, str]:
    value = report.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"render report {key} must be an object")
    raw_path = value.get("path")
    expected_sha = value.get("sha256")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError(f"render report {key} path is missing")
    if not isinstance(expected_sha, str) or not SHA256_PATTERN.fullmatch(expected_sha):
        raise ValueError(f"render report {key} sha256 is invalid")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = report_path.parent / path
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"render report {key} file does not exist: {path}")
    actual_sha = _sha256(path)
    if actual_sha != expected_sha:
        raise ValueError(
            f"render report {key} sha256 mismatch: expected {expected_sha}, got {actual_sha}"
        )
    return path, expected_sha


def load_render_report(path: Path | str) -> tuple[dict[str, Any], Path]:
    """Load a render report and verify its minimum immutable file bindings."""
    report_path = Path(path).expanduser().resolve()
    if not report_path.is_file():
        raise ValueError(f"render report does not exist: {report_path}")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"render report is not valid JSON: {error}") from error
    if not isinstance(report, dict):
        raise ValueError("render report root must be an object")
    pptx = report.get("pptx")
    pptx_sha = pptx.get("sha256") if isinstance(pptx, dict) else None
    if not isinstance(pptx_sha, str) or not SHA256_PATTERN.fullmatch(pptx_sha):
        raise ValueError("render report pptx sha256 is invalid")
    renderer = report.get("renderer")
    if not isinstance(renderer, dict) or renderer.get("backend") != "libreoffice":
        raise ValueError("render report renderer must be libreoffice")
    _reported_file(report, "pdf", report_path=report_path)
    preview_path, _ = _reported_file(report, "preview", report_path=report_path)
    preview_size = report["preview"].get("size")
    if not (
        isinstance(preview_size, list)
        and len(preview_size) == 2
        and all(isinstance(item, int) and item > 0 for item in preview_size)
    ):
        raise ValueError("render report preview size is invalid")
    with Image.open(preview_path) as preview:
        if list(preview.size) != preview_size:
            raise ValueError(
                f"render report preview size mismatch: expected {preview_size}, "
                f"got {list(preview.size)}"
            )
    return report, report_path


def _align_preview(reference: Image.Image, preview: Image.Image) -> tuple[Image.Image, str]:
    if preview.size == reference.size:
        return preview, "same_dimensions"
    reference_ratio = reference.width / reference.height
    preview_ratio = preview.width / preview.height
    if abs(reference_ratio - preview_ratio) <= 0.001:
        return preview.resize(reference.size, RESAMPLING), "resized_to_reference"
    contained = preview.copy()
    contained.thumbnail(reference.size, RESAMPLING)
    canvas = Image.new("RGB", reference.size, "white")
    offset = ((reference.width - contained.width) // 2, (reference.height - contained.height) // 2)
    canvas.paste(contained, offset)
    return canvas, "contained_to_reference"


def _edge_mask(image: Image.Image) -> Image.Image:
    return (
        image.convert("L")
        .filter(ImageFilter.FIND_EDGES)
        .point(lambda value: 255 if value >= 24 else 0)
        .convert("1")
    )


def _mask_f1(left: Image.Image, right: Image.Image) -> float:
    left_pixels = {
        index for index, value in enumerate(left.get_flattened_data()) if value
    }
    right_pixels = {
        index for index, value in enumerate(right.get_flattened_data()) if value
    }
    if not left_pixels and not right_pixels:
        return 1.0
    overlap = len(left_pixels & right_pixels)
    precision = overlap / len(right_pixels) if right_pixels else 0.0
    recall = overlap / len(left_pixels) if left_pixels else 0.0
    if precision + recall == 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 6)


def _foreground_metrics(
    reference: Image.Image, preview: Image.Image, difference: Image.Image
) -> tuple[float, float]:
    reference_luma = reference.convert("L")
    preview_luma = preview.convert("L")
    foreground_indices = {
        index
        for index, (left, right) in enumerate(
            zip(
                reference_luma.get_flattened_data(),
                preview_luma.get_flattened_data(),
            )
        )
        if left < 245 or right < 245
    }
    total = reference.width * reference.height
    ratio = len(foreground_indices) / total if total else 0.0
    if not foreground_indices:
        return 1.0, round(ratio, 6)
    difference_luma = difference.convert("L")
    pixels = difference_luma.get_flattened_data()
    foreground_error = sum(pixels[index] for index in foreground_indices) / len(
        foreground_indices
    )
    similarity = max(0.0, min(1.0, 1 - foreground_error / 255))
    return round(similarity, 6), round(ratio, 6)


def _metrics(reference: Image.Image, preview: Image.Image, changed_threshold: int) -> dict[str, float]:
    difference = ImageChops.difference(reference, preview)
    mean_channels = ImageStat.Stat(difference).mean[:3]
    mean_absolute_error = sum(mean_channels) / 3
    similarity = max(0.0, min(1.0, 1 - mean_absolute_error / 255))
    grayscale = difference.convert("L")
    histogram = grayscale.histogram()
    changed = sum(histogram[changed_threshold + 1 :])
    total = reference.width * reference.height
    foreground_similarity, foreground_pixel_ratio = _foreground_metrics(
        reference, preview, difference
    )
    return {
        "similarity": round(similarity, 6),
        "mean_absolute_error": round(mean_absolute_error, 6),
        "changed_pixel_ratio": round(changed / total if total else 0.0, 6),
        "foreground_similarity": foreground_similarity,
        "foreground_pixel_ratio": foreground_pixel_ratio,
        "edge_f1": _mask_f1(_edge_mask(reference), _edge_mask(preview)),
    }


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return safe or "region"


def _region_bbox(value: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        x, y, w, h = (int(round(float(item))) for item in value)
    except (TypeError, ValueError):
        return None
    left = max(0, min(width, x))
    top = max(0, min(height, y))
    right = max(left, min(width, x + w))
    bottom = max(top, min(height, y + h))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _save_region_evidence(
    reference: Image.Image,
    preview: Image.Image,
    regions: list[dict[str, Any]],
    output_dir: Path,
    changed_threshold: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    region_dir = output_dir / "regions"
    region_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, region in enumerate(regions):
        if not isinstance(region, dict):
            continue
        region_id = str(region.get("region_id") or f"region-{index + 1:03d}")
        bbox = _region_bbox(region.get("source_bbox"), reference.width, reference.height)
        if bbox is None:
            skipped.append({"region_id": region_id, "reason": "bbox_out_of_bounds"})
            continue
        raw = region.get("source_bbox")
        if raw[0] < 0 or raw[1] < 0 or raw[0] + raw[2] > reference.width or raw[1] + raw[3] > reference.height:
            skipped.append({"region_id": region_id, "reason": "bbox_out_of_bounds"})
            continue
        source_crop = reference.crop(bbox)
        preview_crop = preview.crop(bbox)
        source_crop = source_crop.resize(
            (source_crop.width * 2, source_crop.height * 2), Image.Resampling.NEAREST
        )
        preview_crop = preview_crop.resize(
            (preview_crop.width * 2, preview_crop.height * 2), Image.Resampling.NEAREST
        )
        gap = 24
        label_height = 48
        comparison = Image.new(
            "RGB",
            (source_crop.width * 2 + gap, source_crop.height + label_height),
            "#F2F2F2",
        )
        comparison.paste(source_crop, (0, label_height))
        comparison.paste(preview_crop, (source_crop.width + gap, label_height))
        draw = ImageDraw.Draw(comparison)
        draw.text((8, 16), "reference (200%)", fill="black")
        draw.text((source_crop.width + gap + 8, 16), "preview (200%)", fill="black")
        filename = f"{index + 1:03d}-{_safe_name(region_id)}.png"
        evidence_path = region_dir / filename
        comparison.save(evidence_path)
        results.append(
            {
                "region_id": region_id,
                "source_bbox": list(bbox),
                "evidence": str(evidence_path.resolve()),
                "evidence_sha256": _sha256(evidence_path),
                "scale_percent": 200,
                "metrics": _metrics(
                    reference.crop(bbox), preview.crop(bbox), changed_threshold
                ),
            }
        )
    return results, skipped


def _foreground_ratio(image: Image.Image) -> float:
    grayscale = image.convert("L")
    pixels = grayscale.get_flattened_data()
    total = image.width * image.height
    if total == 0:
        return 0.0
    return sum(1 for value in pixels if value < 245) / total


def _region_presence(
    reference: Image.Image,
    preview: Image.Image,
    regions: list[dict[str, Any]],
) -> dict[str, Any]:
    missing: list[str] = []
    checks: list[dict[str, Any]] = []
    for index, region in enumerate(regions):
        if not isinstance(region, dict):
            continue
        element_ids = region.get("element_ids")
        if not isinstance(element_ids, list) or not element_ids:
            continue
        bbox = _region_bbox(region.get("source_bbox"), reference.width, reference.height)
        if bbox is None:
            continue
        source_ratio = _foreground_ratio(reference.crop(bbox))
        if source_ratio < 0.02:
            continue
        preview_ratio = _foreground_ratio(preview.crop(bbox))
        region_id = str(region.get("region_id") or f"region-{index + 1:03d}")
        is_missing = preview_ratio < 0.002 and preview_ratio < source_ratio * 0.10
        if is_missing:
            missing.append(region_id)
        checks.append(
            {
                "region_id": region_id,
                "source_foreground_ratio": round(source_ratio, 6),
                "preview_foreground_ratio": round(preview_ratio, 6),
                "missing": is_missing,
            }
        )
    return {
        "checked": len(checks),
        "missing": missing,
        "status": "failed" if missing else "passed",
        "checks": checks,
    }


def build_visual_diff(
    reference_path: Path | str,
    preview_path: Path | str,
    output_dir: Path | str,
    *,
    regions: list[dict[str, Any]] | None = None,
    minimum_similarity: float | None = None,
    changed_threshold: int = 8,
    profile: str = "strict",
) -> dict[str, Any]:
    """Create visual evidence and return the JSON-serializable report."""
    reference_path = Path(reference_path).expanduser().resolve()
    preview_path = Path(preview_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    region_dir = output_dir / "regions"
    if region_dir.is_symlink():
        region_dir.unlink()
    elif region_dir.exists():
        shutil.rmtree(region_dir)
    if not 0 <= changed_threshold <= 254:
        raise ValueError("changed_threshold must be between 0 and 254")
    if minimum_similarity is not None and not 0 <= minimum_similarity <= 1:
        raise ValueError("minimum_similarity must be between 0 and 1")
    if profile not in {"rapid", "reviewed", "strict"}:
        raise ValueError("profile must be rapid, reviewed, or strict")

    with Image.open(reference_path) as source_image:
        reference = source_image.convert("RGB")
    with Image.open(preview_path) as preview_image:
        preview_original = preview_image.convert("RGB")
    preview, alignment = _align_preview(reference, preview_original)

    difference = ImageChops.difference(reference, preview)
    overlay = Image.blend(reference, preview, 0.5)
    amplified = difference.point(lambda value: min(255, value * 4))
    overlay_path = output_dir / "overlay.png"
    difference_path = output_dir / "diff.png"
    overlay.save(overlay_path)
    amplified.save(difference_path)

    full_page = _metrics(reference, preview, changed_threshold)
    raw_similarity = 1 - sum(ImageStat.Stat(difference).mean[:3]) / 3 / 255
    if minimum_similarity is None:
        tripwire = {
            "available": False,
            "minimum_similarity": None,
            "triggered": None,
            "reason": "no_approved_baseline",
            "note": "Tripwire can block delivery but cannot automatically approve visual fidelity.",
        }
    else:
        triggered = raw_similarity < minimum_similarity
        tripwire = {
            "available": True,
            "minimum_similarity": minimum_similarity,
            "triggered": triggered,
            "reason": "below_minimum_similarity" if triggered else None,
            "note": "Tripwire can block delivery but cannot automatically approve visual fidelity.",
        }
    evidence_regions = [] if profile == "rapid" else regions or []
    if evidence_regions:
        region_results, skipped_regions = _save_region_evidence(
            reference,
            preview,
            evidence_regions,
            output_dir,
            changed_threshold,
        )
    else:
        region_results, skipped_regions = [], []
    report = {
        "verification_profile": profile,
        "reference": {"path": str(reference_path), "sha256": _sha256(reference_path)},
        "preview": {"path": str(preview_path), "sha256": _sha256(preview_path)},
        "reference_size": list(reference.size),
        "preview_size": list(preview_original.size),
        "alignment": alignment,
        "changed_threshold": changed_threshold,
        "full_page": full_page,
        "tripwire": tripwire,
        "overlay": str(overlay_path),
        "diff": str(difference_path),
        "evidence": {
            "overlay": {
                "path": str(overlay_path),
                "sha256": _sha256(overlay_path),
            },
            "diff": {
                "path": str(difference_path),
                "sha256": _sha256(difference_path),
            },
        },
        "regions": region_results,
        "skipped_regions": skipped_regions,
        "region_summary": {"requested": len(evidence_regions), "generated": len(region_results), "skipped": len(skipped_regions)},
    }
    report_path = output_dir / "visual-diff.json"
    report["report"] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def build_visual_diff_from_render_report(
    reference_path: Path | str,
    render_report_path: Path | str,
    output_dir: Path | str,
    *,
    regions: list[dict[str, Any]] | None = None,
    minimum_similarity: float | None = None,
    changed_threshold: int = 8,
    profile: str = "strict",
) -> dict[str, Any]:
    """Create visual evidence using only the preview bound by a render report."""
    render_report, resolved_report_path = load_render_report(render_report_path)
    preview_path, _ = _reported_file(
        render_report, "preview", report_path=resolved_report_path
    )
    report = build_visual_diff(
        reference_path,
        preview_path,
        output_dir,
        regions=regions,
        minimum_similarity=minimum_similarity,
        changed_threshold=changed_threshold,
        profile=profile,
    )
    reference_path = Path(reference_path).expanduser().resolve()
    with Image.open(reference_path) as source_image:
        reference = source_image.convert("RGB")
    with Image.open(preview_path) as preview_image:
        preview_original = preview_image.convert("RGB")
    preview, _ = _align_preview(reference, preview_original)
    report.update(
        {
            "pptx_sha256": render_report["pptx"]["sha256"],
            "render_report": {
                "path": str(resolved_report_path),
                "sha256": _sha256(resolved_report_path),
            },
            "renderer": render_report["renderer"],
            "pdf_sha256": render_report["pdf"]["sha256"],
            "region_presence": _region_presence(
                reference, preview, regions or []
            ),
        }
    )
    report_path = Path(report["report"])
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="Clean visual reference image")
    parser.add_argument(
        "--render-report",
        type=Path,
        required=True,
        help="render-report.json produced by render_preview.py",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--spec", type=Path, help="Optional page-reconstruction.json for region crops")
    parser.add_argument("--minimum-similarity", type=float)
    parser.add_argument("--changed-threshold", type=int, default=8)
    parser.add_argument("--profile", choices=("rapid", "reviewed", "strict"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    regions: list[dict[str, Any]] = []
    spec_profile: str | None = None
    if args.spec:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        value = spec.get("regions", []) if isinstance(spec, dict) else []
        if isinstance(value, list):
            regions = value
        if isinstance(spec, dict) and isinstance(spec.get("verification_profile"), str):
            spec_profile = spec["verification_profile"]
    report = build_visual_diff_from_render_report(
        args.reference,
        args.render_report,
        args.output_dir,
        regions=regions,
        minimum_similarity=args.minimum_similarity,
        changed_threshold=args.changed_threshold,
        profile=args.profile or spec_profile or "strict",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["tripwire"]["triggered"] or report["region_presence"]["status"] == "failed":
        return 2
    return 1 if report["region_summary"]["skipped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
