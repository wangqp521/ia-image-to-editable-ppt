#!/usr/bin/env python3
"""Validate one page-reconstruction.json before generation or delivery."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


ALLOWED_KINDS = {
    "text",
    "shape",
    "line",
    "table",
    "matrix",
    "status",
    "icon",
    "picture",
    "diagram",
    "chart",
    "special_text",
}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_MODULES = {"page_layout", "typography", "icons", "special_text", "picture_framing", "graphics", "diagram", "chart", "high_risk"}
GATE_RESULTS = {"passed", "changes_required", "not_verifiable"}
VISUAL_REVIEW_COVERAGE_FIELDS = {
    "canvas_and_regions",
    "objects_and_geometry",
    "text_and_typography",
    "tables_and_matrices",
    "graphics_connectors_charts",
    "pictures_crop_layers",
    "high_risk_regions",
}
VISUAL_REVIEW_COVERAGE_RESULTS = {"checked", "not_applicable", "not_reviewable"}
VERIFICATION_PROFILES = {"rapid", "reviewed", "strict"}
PROFILE_DELIVERY_STATUSES = {
    "rapid": {"pending", "rapid_validated", "rapid_validation_failed"},
    "reviewed": {"pending", "reviewed_passed", "reviewed_failed"},
    "strict": {"pending", "strict_gate_passed", "strict_gate_failed"},
}
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
RGB_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_IMAGE_PIXELS = 100_000_000
COORDINATE_MANIFEST_METADATA_KEY = "coordinate_overlay_manifest_sha256"
PDF_PAGE_SIZE_PT = (960.0, 540.0)
PDF_PAGE_SIZE_TOLERANCE_PT = 1.0
_LOCAL_MODULE_CACHE: dict[str, Any] = {}


def _error(errors: list[dict[str, str]], code: str, path: str, detail: str) -> None:
    errors.append({"code": code, "path": path, "detail": detail})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _pdf_page_size_matches(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(PDF_PAGE_SIZE_PT)
        and all(_is_number(item) and math.isfinite(float(item)) for item in value)
        and all(
            abs(float(actual) - expected) <= PDF_PAGE_SIZE_TOLERANCE_PT
            for actual, expected in zip(value, PDF_PAGE_SIZE_PT)
        )
    )


def _verification_profile(spec: dict[str, Any]) -> str:
    value = spec.get("verification_profile")
    return "strict" if value is None else value


def _validate_verification_identity(
    spec: dict[str, Any],
    profile: str,
    errors: list[dict[str, str]],
) -> None:
    explicit_profile = spec.get("verification_profile")
    if explicit_profile is not None and explicit_profile not in VERIFICATION_PROFILES:
        _error(
            errors,
            "SPEC_VERIFICATION_PROFILE_INVALID",
            "verification_profile",
            "verification_profile must be rapid, reviewed, or strict",
        )
        return
    delivery_status = spec.get("delivery_status")
    if explicit_profile is None and delivery_status is None:
        return
    if delivery_status not in PROFILE_DELIVERY_STATUSES.get(profile, set()):
        _error(
            errors,
            "SPEC_DELIVERY_STATUS_INVALID",
            "delivery_status",
            f"delivery_status is invalid for {profile} verification",
        )


def _valid_size(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(_is_number(item) and item > 0 for item in value)
    )


def _valid_bbox(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(_is_number(item) for item in value)
        and value[2] > 0
        and value[3] > 0
    )


def _slide_bbox_unit_suspect(
    source_bbox: Any,
    slide_bbox: Any,
    canvas: Any,
    kind: Any,
) -> bool:
    if not _valid_bbox(source_bbox) or not _valid_bbox(slide_bbox) or not isinstance(canvas, dict):
        return False
    visual_size = canvas.get("visual_size")
    slide_size = canvas.get("slide_size_emu")
    if not _valid_size(visual_size) or not _valid_size(slide_size):
        return False
    dimensions = (0,) if kind == "line" and source_bbox[2] >= source_bbox[3] else (1,) if kind == "line" else (0, 1)
    for dimension in dimensions:
        source_ratio = source_bbox[dimension + 2] / visual_size[dimension]
        slide_ratio = slide_bbox[dimension + 2] / slide_size[dimension]
        if source_ratio <= 0:
            continue
        relative_scale = slide_ratio / source_ratio
        if relative_scale < 0.05 or relative_scale > 20:
            return True
    return False


def _bbox_in_bounds(bbox: Any, size: Any) -> bool:
    return _valid_bbox(bbox) and _valid_size(size) and bbox[0] >= 0 and bbox[1] >= 0 and bbox[0] + bbox[2] <= size[0] and bbox[1] + bbox[3] <= size[1]


def _bbox_mapping_invalid(source_bbox: Any, slide_bbox: Any, canvas: Any) -> bool:
    if not _valid_bbox(source_bbox) or not _valid_bbox(slide_bbox) or not isinstance(canvas, dict):
        return False
    frame = canvas.get("page_frame_bbox")
    slide_size = canvas.get("slide_size_emu")
    if not _valid_bbox(frame) or not _valid_size(slide_size):
        return False
    source_norm = [
        (source_bbox[0] - frame[0]) / frame[2],
        (source_bbox[1] - frame[1]) / frame[3],
        source_bbox[2] / frame[2],
        source_bbox[3] / frame[3],
    ]
    slide_norm = [
        slide_bbox[0] / slide_size[0],
        slide_bbox[1] / slide_size[1],
        slide_bbox[2] / slide_size[0],
        slide_bbox[3] / slide_size[1],
    ]
    return any(abs(left - right) > 0.01 for left, right in zip(source_norm, slide_norm))


def _validate_reference(
    value: Any,
    path: str,
    errors: list[dict[str, str]],
    expected_size: Any = None,
) -> None:
    if not isinstance(value, dict):
        _error(errors, "SPEC_REFERENCE_INVALID", path, "reference must be an object")
        return
    source_path = value.get("path")
    digest = value.get("sha256")
    if not isinstance(source_path, str) or not source_path or not Path(source_path).is_absolute():
        _error(errors, "SPEC_REFERENCE_PATH_INVALID", f"{path}.path", "path must be absolute")
        return
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        _error(errors, "SPEC_REFERENCE_SHA256_INVALID", f"{path}.sha256", "sha256 must contain 64 hex characters")
        return
    source = Path(source_path).expanduser()
    if source.is_symlink() or not source.is_file():
        _error(errors, "SPEC_REFERENCE_NOT_FOUND", f"{path}.path", "reference must be a readable non-symlink file")
        return
    resolved = source.resolve()
    if _file_sha256(resolved).lower() != digest.lower():
        _error(errors, "SPEC_REFERENCE_HASH_MISMATCH", f"{path}.sha256", "reference sha256 does not match current file")
    if expected_size is None:
        return
    try:
        with Image.open(resolved) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("image dimensions exceed the supported limit")
            image.load()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        _error(errors, "SPEC_REFERENCE_IMAGE_INVALID", f"{path}.path", "reference must be a decodable image within resource limits")
        return
    if _valid_size(expected_size) and (width, height) != tuple(expected_size):
        _error(
            errors,
            "SPEC_REFERENCE_DIMENSIONS_MISMATCH",
            path,
            f"decoded image size {(width, height)} does not match {tuple(expected_size)}",
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_local_script(filename: str) -> Any:
    cached = _LOCAL_MODULE_CACHE.get(filename)
    if cached is not None:
        return cached
    script_path = Path(__file__).resolve().with_name(filename)
    module_spec = importlib.util.spec_from_file_location(
        f"ia_prebuild_evidence_{script_path.stem}",
        script_path,
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"cannot load {script_path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    _LOCAL_MODULE_CACHE[filename] = module
    return module


def _validate_png_evidence(
    evidence: Any,
    *,
    path: str,
    missing_code: str,
    stale_code: str,
    metadata_key: str,
    errors: list[dict[str, str]],
) -> tuple[Path, str] | None:
    if not isinstance(evidence, dict):
        _error(errors, missing_code, path, "current prebuild visual evidence is required")
        return None
    required = {"path", "sha256", "inspection"}
    if not required.issubset(evidence):
        _error(errors, missing_code, path, "evidence requires path, sha256, and inspection")
        return None
    if evidence.get("inspection") != "passed":
        _error(
            errors,
            "SPEC_PREBUILD_VISUAL_INSPECTION_NOT_PASSED",
            f"{path}.inspection",
            "prebuild visual evidence must be displayed, inspected, and passed",
        )
    evidence_path_value = evidence.get("path")
    evidence_hash = evidence.get("sha256")
    if (
        not isinstance(evidence_path_value, str)
        or not evidence_path_value
        or not Path(evidence_path_value).is_absolute()
        or not isinstance(evidence_hash, str)
        or not SHA256_PATTERN.fullmatch(evidence_hash)
    ):
        _error(errors, stale_code, path, "evidence path and sha256 must be current and absolute")
        return None
    evidence_path = Path(evidence_path_value).expanduser()
    if evidence_path.is_symlink() or not evidence_path.is_file() or evidence_path.suffix.lower() != ".png":
        _error(errors, stale_code, f"{path}.path", "evidence must be a readable non-symlink PNG")
        return None
    resolved = evidence_path.resolve()
    if _file_sha256(resolved).lower() != evidence_hash.lower():
        _error(errors, stale_code, f"{path}.sha256", "evidence sha256 does not match the current PNG")
        return None
    try:
        with Image.open(resolved) as image:
            image.load()
            metadata_value = image.info.get(metadata_key)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        _error(errors, stale_code, f"{path}.path", "evidence must be a decodable PNG")
        return None
    if not isinstance(metadata_value, str) or not SHA256_PATTERN.fullmatch(metadata_value):
        _error(errors, stale_code, path, f"PNG metadata {metadata_key} is missing or invalid")
        return None
    return resolved, metadata_value.lower()


def _validate_coordinate_overlay_evidence(
    page_layout: Any,
    clean_visual_reference: Any,
    errors: list[dict[str, str]],
) -> None:
    path = "modules.page_layout.coordinate_overlay_evidence"
    evidence = page_layout.get("coordinate_overlay_evidence") if isinstance(page_layout, dict) else None
    checked = _validate_png_evidence(
        evidence,
        path=path,
        missing_code="SPEC_COORDINATE_OVERLAY_EVIDENCE_MISSING",
        stale_code="SPEC_COORDINATE_OVERLAY_EVIDENCE_STALE",
        metadata_key=COORDINATE_MANIFEST_METADATA_KEY,
        errors=errors,
    )
    if checked is None or not isinstance(evidence, dict):
        return
    source_path = clean_visual_reference.get("path") if isinstance(clean_visual_reference, dict) else None
    source_sha256 = clean_visual_reference.get("sha256") if isinstance(clean_visual_reference, dict) else None
    grid = evidence.get("grid")
    declared_manifest = evidence.get("manifest_sha256")
    if (
        not isinstance(source_path, str)
        or evidence.get("source_sha256") != source_sha256
        or not isinstance(grid, dict)
        or type(grid.get("cols")) is not int
        or type(grid.get("rows")) is not int
        or grid.get("labels") not in {"none", "x", "y", "both"}
        or not isinstance(declared_manifest, str)
        or not SHA256_PATTERN.fullmatch(declared_manifest)
    ):
        _error(errors, "SPEC_COORDINATE_OVERLAY_EVIDENCE_STALE", path, "coordinate evidence binding is incomplete or stale")
        return
    try:
        coordinate_module = _load_local_script("create_coordinate_overlay.py")
        expected = coordinate_module.coordinate_overlay_manifest(
            source_path,
            cols=grid["cols"],
            rows=grid["rows"],
            labels=grid["labels"],
        )[COORDINATE_MANIFEST_METADATA_KEY]
    except (OSError, ValueError, UnidentifiedImageError, RuntimeError):
        _error(errors, "SPEC_COORDINATE_OVERLAY_EVIDENCE_STALE", path, "cannot recompute coordinate overlay manifest")
        return
    metadata_manifest = checked[1]
    if declared_manifest.lower() != expected.lower() or metadata_manifest != expected.lower():
        _error(errors, "SPEC_COORDINATE_OVERLAY_EVIDENCE_STALE", path, "coordinate overlay does not bind the current source and grid")


def _module_element_references(value: Any) -> set[str]:
    references: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "element_id" and isinstance(child, str):
                references.add(child)
            elif key == "element_ids" and isinstance(child, list):
                references.update(item for item in child if isinstance(item, str))
            else:
                references.update(_module_element_references(child))
    elif isinstance(value, list):
        for child in value:
            references.update(_module_element_references(child))
    return references


def _validate_gate_artifact(
    value: Any,
    path: str,
    errors: list[dict[str, str]],
) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        _error(errors, "SPEC_GATE_ARTIFACT_MISSING", path, "artifact identity is required")
        return None
    artifact_path = value.get("path")
    digest = value.get("sha256")
    if not isinstance(artifact_path, str) or not artifact_path or not Path(artifact_path).is_absolute():
        _error(errors, "SPEC_GATE_ARTIFACT_INVALID", f"{path}.path", "artifact path must be absolute")
        return None
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        _error(errors, "SPEC_GATE_ARTIFACT_INVALID", f"{path}.sha256", "artifact sha256 must contain 64 hex characters")
        return None
    resolved = Path(artifact_path).expanduser().resolve()
    if not resolved.is_file():
        _error(errors, "SPEC_GATE_ARTIFACT_NOT_FOUND", f"{path}.path", "artifact file does not exist")
        return None
    actual = _file_sha256(resolved)
    if actual.lower() != digest.lower():
        _error(errors, "SPEC_GATE_ARTIFACT_HASH_MISMATCH", path, "artifact sha256 does not match current file")
        return None
    return str(resolved), actual.lower()


def _load_json_artifact(
    artifact: tuple[str, str] | None,
    *,
    code: str,
    path: str,
    errors: list[dict[str, str]],
) -> dict[str, Any] | None:
    if artifact is None:
        return None
    try:
        payload = json.loads(Path(artifact[0]).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _error(errors, code, path, "artifact must contain valid JSON")
        return None
    if not isinstance(payload, dict):
        _error(errors, code, path, "artifact root must be an object")
        return None
    return payload


def _render_file_identity(
    value: Any,
    *,
    path: str,
    errors: list[dict[str, str]],
) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        _error(errors, "SPEC_RENDER_REPORT_INVALID", path, "file identity is required")
        return None
    raw_path = value.get("path")
    digest = value.get("sha256")
    if (
        not isinstance(raw_path, str)
        or not Path(raw_path).is_absolute()
        or not isinstance(digest, str)
        or not SHA256_PATTERN.fullmatch(digest)
    ):
        _error(errors, "SPEC_RENDER_REPORT_INVALID", path, "path and sha256 are invalid")
        return None
    resolved = Path(raw_path).expanduser().resolve()
    if not resolved.is_file() or _file_sha256(resolved).lower() != digest.lower():
        _error(errors, "SPEC_RENDER_REPORT_INVALID", path, "reported file is missing or stale")
        return None
    return str(resolved), digest.lower()


def _validate_render_report(
    artifact: tuple[str, str] | None,
    visual_pptx: tuple[str, str] | None,
    preview: tuple[str, str] | None,
    preflight_artifact: tuple[str, str] | None,
    errors: list[dict[str, str]],
) -> dict[str, Any] | None:
    report = _load_json_artifact(
        artifact,
        code="SPEC_RENDER_REPORT_INVALID",
        path="visual_gate.render_report",
        errors=errors,
    )
    preflight = _load_json_artifact(
        preflight_artifact,
        code="SPEC_RUNTIME_PREFLIGHT_INVALID",
        path="runtime_preflight",
        errors=errors,
    )
    if report is None:
        return None
    if report.get("schema_version") != 1:
        _error(
            errors,
            "SPEC_RENDER_REPORT_INVALID",
            "visual_gate.render_report.schema_version",
            "expected render report schema_version 1",
        )
    pptx = report.get("pptx")
    reported_pptx_sha = pptx.get("sha256") if isinstance(pptx, dict) else None
    if visual_pptx is None or reported_pptx_sha != visual_pptx[1]:
        _error(
            errors,
            "SPEC_RENDER_PPTX_MISMATCH",
            "visual_gate.render_report.pptx",
            "render report must bind the current visual-gate PPTX",
        )
    reported_preview = _render_file_identity(
        report.get("preview"),
        path="visual_gate.render_report.preview",
        errors=errors,
    )
    if (
        preview is None
        or reported_preview is None
        or reported_preview != preview
        or report.get("preview", {}).get("size") != [1920, 1080]
    ):
        _error(
            errors,
            "SPEC_RENDER_PREVIEW_MISMATCH",
            "visual_gate.render_report.preview",
            "render report must bind the current 1920x1080 preview",
        )
    _render_file_identity(
        report.get("pdf"),
        path="visual_gate.render_report.pdf",
        errors=errors,
    )
    _render_file_identity(
        report.get("font_report"),
        path="visual_gate.render_report.font_report",
        errors=errors,
    )
    pdf = report.get("pdf")
    if (
        not isinstance(pdf, dict)
        or pdf.get("pages") != 1
        or not _pdf_page_size_matches(pdf.get("page_size_pt"))
    ):
        _error(
            errors,
            "SPEC_RENDER_REPORT_INVALID",
            "visual_gate.render_report.pdf",
            "rendered PDF must contain one 960x540 point page",
        )
    renderer = report.get("renderer")
    rasterizer = report.get("rasterizer")
    runtime_matches = (
        isinstance(preflight, dict)
        and preflight.get("valid") is True
        and preflight.get("renderer_backend") == "libreoffice"
        and preflight.get("preview_size") == [1920, 1080]
        and isinstance(renderer, dict)
        and renderer.get("backend") == "libreoffice"
        and renderer.get("isolated_profile") is True
        and renderer.get("path")
        == preflight.get("executables", {}).get("soffice", {}).get("path")
        and renderer.get("version")
        == preflight.get("executables", {}).get("soffice", {}).get("version")
        and renderer.get("executable_sha256")
        == preflight.get("executables", {}).get("soffice", {}).get("sha256")
        and renderer.get("fontconfig_path")
        == preflight.get("fontconfig", {}).get("path")
        and renderer.get("fontconfig_sha256")
        == preflight.get("fontconfig", {}).get("sha256")
        and isinstance(rasterizer, dict)
        and rasterizer.get("path")
        == preflight.get("executables", {}).get("pdftoppm", {}).get("path")
        and rasterizer.get("version")
        == preflight.get("executables", {}).get("pdftoppm", {}).get("version")
        and rasterizer.get("executable_sha256")
        == preflight.get("executables", {}).get("pdftoppm", {}).get("sha256")
        and rasterizer.get("output_size") == [1920, 1080]
    )
    if not runtime_matches:
        _error(
            errors,
            "SPEC_RENDER_RUNTIME_MISMATCH",
            "visual_gate.render_report.renderer",
            "render report must match the fixed stable LibreOffice preflight identity",
        )
    return report


def _validate_font_traces_against_render(
    typography: Any,
    render_report: dict[str, Any] | None,
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(typography, dict) or not isinstance(render_report, dict):
        return
    pdf_sha = render_report.get("pdf", {}).get("sha256")
    resolved_fonts = render_report.get("font_report", {}).get("resolved_fonts")
    for index, item in enumerate(typography.get("items", [])):
        trace = item.get("fallback_trace") if isinstance(item, dict) else None
        if trace is None:
            continue
        if (
            not isinstance(trace, dict)
            or trace.get("pdf_sha256") != pdf_sha
            or trace.get("resolved_fonts") != resolved_fonts
        ):
            _error(
                errors,
                "SPEC_FONT_TRACE_RENDER_MISMATCH",
                f"modules.typography.items[{index}].fallback_trace",
                "font fallback trace must bind the current rendered PDF and resolved-font list",
            )


def _validate_validator_report(
    artifact: tuple[str, str] | None,
    expected_native_list_contracts: int,
    expected_pptx_sha256: str | None,
    errors: list[dict[str, str]],
) -> None:
    if artifact is None:
        return
    path = Path(artifact[0])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _error(
            errors,
            "SPEC_VALIDATOR_REPORT_INVALID",
            "editability_gate.validator",
            "validator artifact must contain valid JSON",
        )
        return
    if not isinstance(payload, dict):
        _error(
            errors,
            "SPEC_VALIDATOR_REPORT_INVALID",
            "editability_gate.validator",
            "validator report root must be an object",
        )
        return
    report_errors = payload.get("errors")
    if payload.get("valid") is not True or not isinstance(report_errors, list) or report_errors:
        _error(
            errors,
            "SPEC_VALIDATOR_REPORT_FAILED",
            "editability_gate.validator",
            "validator report must have valid=true and an empty errors array",
        )
    report_pptx_sha256 = payload.get("pptx_sha256")
    if (
        not isinstance(report_pptx_sha256, str)
        or not SHA256_PATTERN.fullmatch(report_pptx_sha256)
        or expected_pptx_sha256 is None
        or report_pptx_sha256.lower() != expected_pptx_sha256.lower()
    ):
        _error(
            errors,
            "SPEC_VALIDATOR_PPTX_MISMATCH",
            "editability_gate.validator.pptx_sha256",
            "validator report must bind the current editability-gate PPTX sha256",
        )
    checked = payload.get("native_list_contracts_checked")
    if not isinstance(checked, int) or isinstance(checked, bool) or checked != expected_native_list_contracts:
        _error(
            errors,
            "SPEC_NATIVE_LIST_VALIDATION_MISSING",
            "editability_gate.validator.native_list_contracts_checked",
            f"expected {expected_native_list_contracts} checked native-list TextBox contracts",
        )


def _validate_image_artifact(
    artifact: tuple[str, str] | None,
    code: str,
    path: str,
    errors: list[dict[str, str]],
) -> bool:
    if artifact is None:
        return False
    try:
        with Image.open(artifact[0]) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("image dimensions exceed the supported limit")
            image.load()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        _error(errors, code, path, "artifact must be a decodable image within resource limits")
        return False
    return True


def _visual_evidence_identity(
    value: Any,
    path: str,
    errors: list[dict[str, str]],
) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        _error(errors, "SPEC_VISUAL_DIFF_EVIDENCE_INCOMPLETE", path, "evidence identity is required")
        return None
    artifact = _validate_gate_artifact(value, path, errors)
    if artifact is None:
        return None
    if not _validate_image_artifact(
        artifact,
        "SPEC_VISUAL_DIFF_EVIDENCE_INCOMPLETE",
        path,
        errors,
    ):
        return None
    return artifact


def _validate_visual_diff_report(
    artifact: tuple[str, str] | None,
    spec: dict[str, Any],
    preview: tuple[str, str] | None,
    render_report: tuple[str, str] | None,
    visual_pptx: tuple[str, str] | None,
    errors: list[dict[str, str]],
    *,
    require_all_regions: bool = True,
) -> set[str]:
    verified: set[str] = set()
    if artifact is None:
        return verified
    try:
        payload = json.loads(Path(artifact[0]).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _error(errors, "SPEC_VISUAL_DIFF_REPORT_INVALID", "visual_gate.report", "visual diff report must contain valid JSON")
        return verified
    if not isinstance(payload, dict):
        _error(errors, "SPEC_VISUAL_DIFF_REPORT_INVALID", "visual_gate.report", "visual diff report root must be an object")
        return verified
    expected_profile = _verification_profile(spec)
    if payload.get("verification_profile") != expected_profile:
        _error(
            errors,
            "SPEC_VISUAL_DIFF_PROFILE_MISMATCH",
            "visual_gate.report.verification_profile",
            "visual diff must use the current fixed verification profile",
        )
    source_hash = spec.get("clean_visual_reference", {}).get("sha256")
    report_source = payload.get("reference")
    if not isinstance(report_source, dict) or report_source.get("sha256") != source_hash:
        _error(errors, "SPEC_VISUAL_DIFF_SOURCE_MISMATCH", "visual_gate.report.reference", "visual diff must bind the current clean visual reference")
    report_preview = payload.get("preview")
    if (
        preview is None
        or not isinstance(report_preview, dict)
        or report_preview.get("sha256") != preview[1]
    ):
        _error(errors, "SPEC_VISUAL_DIFF_PREVIEW_MISMATCH", "visual_gate.report.preview", "visual diff must bind the current preview")
    report_render = payload.get("render_report")
    if (
        render_report is None
        or not isinstance(report_render, dict)
        or report_render.get("path") != render_report[0]
        or report_render.get("sha256") != render_report[1]
        or payload.get("pptx_sha256") != (
            visual_pptx[1] if visual_pptx is not None else None
        )
    ):
        _error(
            errors,
            "SPEC_RENDER_REPORT_INVALID",
            "visual_gate.report.render_report",
            "visual diff must bind the current render report and PPTX",
        )
    region_presence = payload.get("region_presence")
    if (
        not isinstance(region_presence, dict)
        or region_presence.get("status") != "passed"
        or region_presence.get("missing") != []
    ):
        _error(
            errors,
            "SPEC_REGION_PRESENCE_FAILED",
            "visual_gate.report.region_presence",
            "region presence must pass before final delivery",
        )
    summary = payload.get("region_summary")
    if require_all_regions:
        requested = len(spec.get("regions", [])) if isinstance(spec.get("regions"), list) else 0
    else:
        requested = summary.get("requested") if isinstance(summary, dict) else None
    if (
        not isinstance(summary, dict)
        or not isinstance(requested, int)
        or isinstance(requested, bool)
        or requested < 0
        or summary.get("skipped") != 0
        or summary.get("requested") != requested
        or summary.get("generated") != requested
    ):
        _error(errors, "SPEC_VISUAL_DIFF_EVIDENCE_INCOMPLETE", "visual_gate.report.region_summary", "all requested regions must have current evidence and none may be skipped")
    evidence = payload.get("evidence")
    for key in ("overlay", "diff"):
        identity = _visual_evidence_identity(
            evidence.get(key) if isinstance(evidence, dict) else None,
            f"visual_gate.report.evidence.{key}",
            errors,
        )
        if identity:
            verified.update({identity[0], Path(identity[0]).name})
    regions = payload.get("regions")
    if not isinstance(regions, list) or len(regions) != requested:
        _error(errors, "SPEC_VISUAL_DIFF_EVIDENCE_INCOMPLETE", "visual_gate.report.regions", "region evidence count must match the spec")
    else:
        for index, region in enumerate(regions):
            identity = _visual_evidence_identity(
                {
                    "path": region.get("evidence"),
                    "sha256": region.get("evidence_sha256"),
                }
                if isinstance(region, dict)
                else None,
                f"visual_gate.report.regions[{index}]",
                errors,
            )
            if identity:
                verified.update({identity[0], Path(identity[0]).name})
    return verified


def _load_pptx_validator():
    path = Path(__file__).with_name("validate_pptx.py")
    module_spec = importlib.util.spec_from_file_location("ia_validate_pptx_for_final", path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def _rerun_pptx_validator(
    pptx: tuple[str, str] | None,
    spec: dict[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, Any] | None:
    if pptx is None:
        return None
    try:
        result = _load_pptx_validator().validate_pptx(
            Path(pptx[0]),
            expected_slides=1,
            reconstruction_spec=spec,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _error(errors, "SPEC_CURRENT_PPTX_VALIDATION_FAILED", "editability_gate.pptx", str(exc))
        return None
    if result.get("valid") is not True:
        _error(
            errors,
            "SPEC_CURRENT_PPTX_VALIDATION_FAILED",
            "editability_gate.pptx",
            f"current PPTX failed structure validation: {result.get('errors', [])}",
        )
    return result


def _validate_coverage(
    items: Any,
    text: str,
    path: str,
    code: str,
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(items, list) or not items:
        _error(errors, code, path, "segments must be a non-empty array")
        return
    cursor = 0
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            _error(errors, code, item_path, "segment must be an object")
            continue
        start = item.get("start")
        end = item.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or start != cursor or end <= start:
            _error(errors, code, item_path, f"expected continuous range beginning at {cursor}")
            return
        cursor = end
    if cursor != len(text):
        _error(errors, code, path, f"segments end at {cursor}, text length is {len(text)}")


def _validate_text_run_styles(
    runs: Any,
    path: str,
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(runs, list):
        return
    required = {"font_size", "font_weight", "color", "decoration", "letter_spacing"}
    for index, run in enumerate(runs):
        run_path = f"{path}[{index}]"
        if not isinstance(run, dict):
            continue
        missing = sorted(required - set(run))
        font_size = run.get("font_size")
        font_weight = run.get("font_weight")
        color = run.get("color")
        decoration = run.get("decoration")
        letter_spacing = run.get("letter_spacing")
        if (
            missing
            or not _is_number(font_size)
            or font_size <= 0
            or not _is_number(font_weight)
            or not 1 <= font_weight <= 1000
            or not isinstance(color, str)
            or not color
            or not isinstance(decoration, str)
            or not decoration
            or not _is_number(letter_spacing)
        ):
            detail = f"missing fields: {', '.join(missing)}" if missing else "invalid run style values"
            _error(errors, "SPEC_TEXT_RUN_STYLE_INVALID", run_path, detail)


def _validate_paragraphs(
    paragraphs: Any,
    text: str,
    text_box: Any,
    path: str,
    errors: list[dict[str, str]],
) -> None:
    _validate_coverage(
        paragraphs,
        text,
        path,
        "SPEC_PARAGRAPH_COVERAGE_INVALID",
        errors,
    )
    if not isinstance(paragraphs, list) or not paragraphs:
        return

    for index, paragraph in enumerate(paragraphs):
        paragraph_path = f"{path}[{index}]"
        if not isinstance(paragraph, dict):
            continue
        list_contract = paragraph.get("list")
        if not isinstance(list_contract, dict) or not isinstance(list_contract.get("is_list"), bool):
            _error(
                errors,
                "SPEC_PARAGRAPH_LIST_INVALID",
                f"{paragraph_path}.list",
                "list must be an object with boolean is_list",
            )
            continue
        level = list_contract.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or level < 0:
            _error(
                errors,
                "SPEC_PARAGRAPH_LIST_INVALID",
                f"{paragraph_path}.list.level",
                "level must be a non-negative integer",
            )
        if not list_contract["is_list"]:
            if list_contract.get("bullet") is not None:
                _error(
                    errors,
                    "SPEC_PARAGRAPH_LIST_INVALID",
                    f"{paragraph_path}.list.bullet",
                    "non-list paragraph bullet must be null",
                )
            continue

        required = {
            "is_list",
            "level",
            "bullet_type",
            "bullet",
            "bullet_font",
            "bullet_size_mode",
            "bullet_size_value",
            "bullet_color",
        }
        missing = sorted(required - set(list_contract))
        bullet_type = list_contract.get("bullet_type")
        bullet = list_contract.get("bullet")
        bullet_font = list_contract.get("bullet_font")
        size_mode = list_contract.get("bullet_size_mode")
        size_value = list_contract.get("bullet_size_value")
        bullet_color = list_contract.get("bullet_color")
        contract_invalid = bool(missing)
        contract_invalid = contract_invalid or bullet_type not in {"char", "auto_number", "picture"}
        contract_invalid = contract_invalid or not isinstance(bullet, str) or not bullet
        contract_invalid = contract_invalid or not isinstance(bullet_font, str) or not bullet_font
        contract_invalid = contract_invalid or size_mode not in {"follow_text", "percent", "points"}
        contract_invalid = contract_invalid or (
            size_mode == "follow_text" and size_value is not None
        )
        contract_invalid = contract_invalid or (
            size_mode in {"percent", "points"}
            and (not _is_number(size_value) or size_value <= 0)
        )
        contract_invalid = contract_invalid or not isinstance(bullet_color, str) or not bullet_color
        contract_invalid = contract_invalid or (
            isinstance(bullet_color, str)
            and bullet_color != "follow_text"
            and not RGB_PATTERN.fullmatch(bullet_color)
        )
        if contract_invalid:
            detail = f"missing fields: {', '.join(missing)}" if missing else "invalid native bullet fields"
            _error(
                errors,
                "SPEC_NATIVE_LIST_CONTRACT_INVALID",
                f"{paragraph_path}.list",
                detail,
            )
        if not _is_number(paragraph.get("margin_left")) or not _is_number(paragraph.get("indent")):
            _error(
                errors,
                "SPEC_NATIVE_LIST_INDENT_INVALID",
                paragraph_path,
                "native list paragraph requires numeric margin_left and indent in EMU",
            )

    expected_breaks = [
        paragraph.get("end")
        for paragraph in paragraphs[:-1]
        if isinstance(paragraph, dict)
    ]
    actual_breaks = text_box.get("paragraph_breaks") if isinstance(text_box, dict) else None
    if actual_breaks != expected_breaks:
        _error(
            errors,
            "SPEC_PARAGRAPH_BREAKS_INVALID",
            f"{path.rsplit('.', 1)[0]}.text_box.paragraph_breaks",
            f"expected {expected_breaks!r}",
        )


def _validate_typography(
    module: Any,
    element_map: dict[str, dict[str, Any]],
    canvas: Any,
    stage: str,
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(module, dict):
        _error(errors, "SPEC_MODULE_INVALID", "modules.typography", "module must be an object")
        return
    if module.get("slide_coordinate_unit") != "EMU":
        _error(
            errors,
            "SPEC_TYPOGRAPHY_UNIT_INVALID",
            "modules.typography.slide_coordinate_unit",
            "typography coordinates must use EMU",
        )
    items = module.get("items")
    if not isinstance(items, list) or not items:
        _error(errors, "SPEC_TYPOGRAPHY_ITEMS_INVALID", "modules.typography.items", "items must be non-empty")
        return
    seen: set[str] = set()
    required = {
        "element_id",
        "text",
        "source_font_guess",
        "selected_font",
        "fallback_reason",
        "fallback_trace",
        "runs",
        "paragraphs",
        "text_box",
        "internal_font_declaration",
        "font_declaration_verified",
    }
    for index, item in enumerate(items):
        path = f"modules.typography.items[{index}]"
        if not isinstance(item, dict):
            _error(errors, "SPEC_TYPOGRAPHY_ITEM_INVALID", path, "item must be an object")
            continue
        missing = sorted(required - set(item))
        if missing:
            _error(errors, "SPEC_TYPOGRAPHY_FIELD_MISSING", path, f"missing fields: {', '.join(missing)}")
            continue
        element_id = item.get("element_id")
        if not isinstance(element_id, str) or element_id not in element_map:
            _error(errors, "SPEC_ELEMENT_REFERENCE_INVALID", f"{path}.element_id", "unknown element_id")
        elif element_id in seen:
            _error(errors, "SPEC_TYPOGRAPHY_ELEMENT_DUPLICATE", f"{path}.element_id", element_id)
        else:
            seen.add(element_id)
        text = item.get("text")
        if not isinstance(text, str) or not text:
            _error(errors, "SPEC_TYPOGRAPHY_TEXT_INVALID", f"{path}.text", "text must be non-empty")
            continue
        removed_fields = {
            "candidates",
            "candidate_trials",
            "render_metrics",
            "font_trial_report",
        }
        for field in sorted(removed_fields.intersection(item)):
            _error(
                errors,
                "SPEC_REMOVED_FONT_WORKFLOW_FIELD",
                f"{path}.{field}",
                f"{field} was removed from the single-font typography workflow",
            )
        source_font_guess = item.get("source_font_guess")
        selected = item.get("selected_font")
        if not isinstance(selected, str) or not selected.strip():
            _error(
                errors,
                "SPEC_SELECTED_FONT_INVALID",
                f"{path}.selected_font",
                "selected_font must be a non-empty font family",
            )
        if source_font_guess == "unknown" and (
            selected != "Noto Sans CJK SC"
            or item.get("fallback_reason") != "source_font_uncertain"
        ):
            _error(
                errors,
                "SPEC_UNCERTAIN_FONT_FALLBACK_INVALID",
                path,
                "unknown source fonts require selected_font=Noto Sans CJK SC and fallback_reason=source_font_uncertain",
            )
        runs = item.get("runs")
        _validate_coverage(runs, text, f"{path}.runs", "SPEC_TEXT_RUN_COVERAGE_INVALID", errors)
        _validate_text_run_styles(runs, f"{path}.runs", errors)
        text_box = item.get("text_box")
        _validate_paragraphs(
            item.get("paragraphs"),
            text,
            text_box,
            f"{path}.paragraphs",
            errors,
        )
        if not isinstance(text_box, dict) or not all(
            _is_number(text_box.get(key)) and (text_box[key] > 0 if key in {"w", "h"} else True)
            for key in ("x", "y", "w", "h")
        ):
            _error(errors, "SPEC_TEXT_BOX_INVALID", f"{path}.text_box", "x/y/w/h must be numeric and w/h positive")
        elif isinstance(element_id, str) and element_id in element_map:
            expected = element_map[element_id].get("slide_bbox")
            actual = [text_box.get(key) for key in ("x", "y", "w", "h")]
            slide_size = canvas.get("slide_size_emu") if isinstance(canvas, dict) else None
            if _valid_bbox(expected) and _valid_bbox(actual) and _valid_size(slide_size):
                if any(abs(a - e) / slide_size[index % 2] > 0.01 for index, (a, e) in enumerate(zip(actual, expected))):
                    _error(errors, "SPEC_TEXT_BOX_MAPPING_INVALID", f"{path}.text_box", "text_box must match its element EMU bbox")
        if not isinstance(item.get("font_declaration_verified"), bool):
            _error(errors, "SPEC_FONT_VERIFICATION_INVALID", f"{path}.font_declaration_verified", "must be boolean")
        elif stage == "final" and not item["font_declaration_verified"]:
            _error(errors, "SPEC_FONT_NOT_VERIFIED", f"{path}.font_declaration_verified", "final spec requires verified font declaration")


def _validate_icons(
    module: Any,
    element_map: dict[str, dict[str, Any]],
    canvas: Any,
    clean_visual_reference: Any,
    page_id: Any,
    stage: str,
    errors: list[dict[str, str]],
) -> None:
    """Validate the narrow icon-crop contract used by this reconstruction skill."""
    if not isinstance(module, dict):
        _error(errors, "SPEC_MODULE_INVALID", "modules.icons", "module must be an object")
        return
    required_module = {
        "schema_version",
        "page_id",
        "slide_coordinate_unit",
        "clean_visual_reference",
        "clean_visual_sha256",
        "icons",
    }
    missing_module = sorted(required_module - set(module))
    if missing_module:
        _error(errors, "SPEC_ICONS_FIELD_MISSING", "modules.icons", f"missing fields: {', '.join(missing_module)}")
        return
    if module.get("schema_version") != 2:
        _error(errors, "SPEC_ICONS_SCHEMA_VERSION_INVALID", "modules.icons.schema_version", "expected schema_version 2")
    if module.get("page_id") != page_id:
        _error(errors, "SPEC_ICONS_PAGE_ID_INVALID", "modules.icons.page_id", "must match page_id")
    if module.get("slide_coordinate_unit") != "EMU":
        _error(errors, "SPEC_ICONS_UNIT_INVALID", "modules.icons.slide_coordinate_unit", "icon slide coordinates must use EMU")

    expected_reference_path = clean_visual_reference.get("path") if isinstance(clean_visual_reference, dict) else None
    expected_reference_hash = clean_visual_reference.get("sha256") if isinstance(clean_visual_reference, dict) else None
    if module.get("clean_visual_reference") != expected_reference_path:
        _error(errors, "SPEC_ICONS_REFERENCE_INVALID", "modules.icons.clean_visual_reference", "must match clean_visual_reference.path")
    if module.get("clean_visual_sha256") != expected_reference_hash:
        _error(errors, "SPEC_ICONS_REFERENCE_INVALID", "modules.icons.clean_visual_sha256", "must match clean_visual_reference.sha256")

    icons = module.get("icons")
    if not isinstance(icons, list) or not icons:
        _error(errors, "SPEC_ICONS_ITEMS_INVALID", "modules.icons.icons", "icons must be a non-empty array")
        return

    icon_element_ids = {element_id for element_id, element in element_map.items() if element.get("kind") == "icon"}
    seen_icon_ids: set[str] = set()
    seen_element_ids: set[str] = set()
    required_item = {
        "icon_id",
        "element_id",
        "category",
        "instance_count",
        "repeat_group",
        "semantic_scope",
        "source_bbox",
        "slide_bbox",
        "layer",
        "source_path",
        "source_sha256",
        "crop_mode",
        "padding",
        "background_handling",
        "asset_path",
        "asset_sha256",
        "alpha_mask_sha256",
        "final_width",
        "final_height",
        "sharpness",
        "validation",
        "native_redraw",
        "selectable_picture_verified",
        "object_type",
    }
    visual_size = canvas.get("visual_size") if isinstance(canvas, dict) else None
    for index, item in enumerate(icons):
        path = f"modules.icons.icons[{index}]"
        if not isinstance(item, dict):
            _error(errors, "SPEC_ICON_ITEM_INVALID", path, "icon must be an object")
            continue
        missing_item = sorted(required_item - set(item))
        if missing_item:
            _error(errors, "SPEC_ICON_FIELD_MISSING", path, f"missing fields: {', '.join(missing_item)}")
            continue

        icon_id = item.get("icon_id")
        if not isinstance(icon_id, str) or not icon_id or icon_id in seen_icon_ids:
            _error(errors, "SPEC_ICON_ID_INVALID", f"{path}.icon_id", "icon_id must be unique and non-empty")
        else:
            seen_icon_ids.add(icon_id)
        element_id = item.get("element_id")
        if not isinstance(element_id, str) or element_id not in icon_element_ids:
            _error(errors, "SPEC_ICON_ELEMENT_REFERENCE_INVALID", f"{path}.element_id", "must reference an icon element")
        elif element_id in seen_element_ids:
            _error(errors, "SPEC_ICON_ELEMENT_DUPLICATE", f"{path}.element_id", "icon element may appear once")
        else:
            seen_element_ids.add(element_id)

        if not isinstance(item.get("category"), str) or not item["category"]:
            _error(errors, "SPEC_ICON_CATEGORY_INVALID", f"{path}.category", "category must be non-empty")
        if not isinstance(item.get("instance_count"), int) or item["instance_count"] <= 0:
            _error(errors, "SPEC_ICON_INSTANCE_COUNT_INVALID", f"{path}.instance_count", "must be a positive integer")
        if item.get("repeat_group") is not None and (not isinstance(item.get("repeat_group"), str) or not item["repeat_group"]):
            _error(errors, "SPEC_ICON_REPEAT_GROUP_INVALID", f"{path}.repeat_group", "must be a non-empty string or null")
        if item.get("semantic_scope") not in {"icon_only", "intentional_composite"}:
            _error(errors, "SPEC_ICON_SEMANTIC_SCOPE_INVALID", f"{path}.semantic_scope", "must be icon_only or intentional_composite")

        source_bbox = item.get("source_bbox")
        slide_bbox = item.get("slide_bbox")
        if not _valid_bbox(source_bbox) or not _valid_bbox(slide_bbox):
            _error(errors, "SPEC_ICON_BBOX_INVALID", path, "source_bbox and slide_bbox must be valid")
        else:
            if not _bbox_in_bounds(source_bbox, visual_size):
                _error(errors, "SPEC_ICON_BBOX_OUT_OF_BOUNDS", f"{path}.source_bbox", "source bbox exceeds visual canvas")
            if isinstance(element_id, str) and element_id in element_map:
                element = element_map[element_id]
                if source_bbox != element.get("source_bbox") or slide_bbox != element.get("slide_bbox"):
                    _error(errors, "SPEC_ICON_ELEMENT_MAPPING_INVALID", path, "icon bboxes must match the referenced element")
                if item.get("layer") != element.get("layer"):
                    _error(errors, "SPEC_ICON_ELEMENT_MAPPING_INVALID", f"{path}.layer", "layer must match the referenced element")
        if not isinstance(item.get("layer"), int) or item["layer"] <= 0:
            _error(errors, "SPEC_ICON_LAYER_INVALID", f"{path}.layer", "must be a positive integer")

        padding = item.get("padding")
        if not isinstance(padding, int) or padding < 0:
            _error(errors, "SPEC_ICON_PADDING_INVALID", f"{path}.padding", "must be a non-negative integer")
        elif _valid_bbox(source_bbox) and _valid_size(visual_size):
            crop_bounds = [source_bbox[0] - padding, source_bbox[1] - padding, source_bbox[2] + padding * 2, source_bbox[3] + padding * 2]
            if not _bbox_in_bounds(crop_bounds, visual_size):
                _error(errors, "SPEC_ICON_CROP_OUT_OF_BOUNDS", f"{path}.padding", "crop bbox plus padding exceeds visual canvas")

        if item.get("source_path") != expected_reference_path or item.get("source_sha256") != expected_reference_hash:
            _error(errors, "SPEC_ICON_SOURCE_BINDING_INVALID", path, "source must exactly bind to clean_visual_reference")
        source_path = Path(item["source_path"]).expanduser() if isinstance(item.get("source_path"), str) else None
        if source_path is None or not source_path.is_absolute() or source_path.is_symlink() or not source_path.is_file():
            _error(errors, "SPEC_ICON_SOURCE_INVALID", f"{path}.source_path", "source must be a readable non-symlink file")
        elif isinstance(item.get("source_sha256"), str) and SHA256_PATTERN.fullmatch(item["source_sha256"]):
            if _file_sha256(source_path.resolve()).lower() != item["source_sha256"].lower():
                _error(errors, "SPEC_ICON_SOURCE_HASH_MISMATCH", f"{path}.source_sha256", "source hash does not match current file")

        crop_mode = item.get("crop_mode")
        if crop_mode != "alpha_isolation":
            _error(
                errors,
                "SPEC_ICON_CROP_MODE_INVALID",
                f"{path}.crop_mode",
                "must be alpha_isolation",
            )
        alpha_hash = item.get("alpha_mask_sha256")
        if not isinstance(item.get("background_handling"), str) or not item["background_handling"]:
            _error(errors, "SPEC_ICON_BACKGROUND_HANDLING_INVALID", f"{path}.background_handling", "must be non-empty")
        if not isinstance(alpha_hash, str) or not SHA256_PATTERN.fullmatch(alpha_hash):
            _error(errors, "SPEC_ICON_ALPHA_MASK_INVALID", f"{path}.alpha_mask_sha256", "alpha isolation requires alpha_mask_sha256")

        asset_path = Path(item["asset_path"]).expanduser() if isinstance(item.get("asset_path"), str) else None
        if asset_path is None or not asset_path.is_absolute() or asset_path.is_symlink() or not asset_path.is_file():
            _error(errors, "SPEC_ICON_ASSET_INVALID", f"{path}.asset_path", "asset must be a readable non-symlink file")
        else:
            resolved_asset = asset_path.resolve()
            if resolved_asset.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                _error(errors, "SPEC_ICON_ASSET_INVALID", f"{path}.asset_path", "asset must be PNG, JPEG, or WEBP")
            if (
                resolved_asset.parent.name != "icons"
                or resolved_asset.parent.parent.name != "assets"
            ):
                _error(
                    errors,
                    "SPEC_ICON_ASSET_LOCATION_INVALID",
                    f"{path}.asset_path",
                    "asset parent must be assets/icons",
                )
            asset_hash = item.get("asset_sha256")
            if not isinstance(asset_hash, str) or not SHA256_PATTERN.fullmatch(asset_hash):
                _error(errors, "SPEC_ICON_ASSET_HASH_INVALID", f"{path}.asset_sha256", "asset sha256 must contain 64 hex characters")
            elif _file_sha256(resolved_asset).lower() != asset_hash.lower():
                _error(errors, "SPEC_ICON_ASSET_HASH_MISMATCH", f"{path}.asset_sha256", "asset hash does not match current file")
            if resolved_asset.suffix.lower() != ".png":
                _error(errors, "SPEC_ICON_ALPHA_CONTENT_INVALID", f"{path}.asset_path", "icon asset must be a PNG")
            elif crop_mode == "alpha_isolation":
                try:
                    with Image.open(resolved_asset) as image:
                        image.load()
                        declared_width = item.get("final_width")
                        declared_height = item.get("final_height")
                        if (
                            isinstance(declared_width, int)
                            and not isinstance(declared_width, bool)
                            and isinstance(declared_height, int)
                            and not isinstance(declared_height, bool)
                            and image.size != (declared_width, declared_height)
                        ):
                            _error(
                                errors,
                                "SPEC_ICON_ASSET_DIMENSIONS_INVALID",
                                path,
                                "decoded asset dimensions must match final_width/final_height",
                            )
                        if image.mode != "RGBA":
                            _error(errors, "SPEC_ICON_ALPHA_CONTENT_INVALID", f"{path}.asset_path", "icon PNG must use RGBA mode")
                        else:
                            alpha = image.getchannel("A")
                            minimum, maximum = alpha.getextrema()
                            if minimum != 0 or maximum == 0:
                                _error(errors, "SPEC_ICON_ALPHA_CONTENT_INVALID", f"{path}.asset_path", "icon alpha must contain transparent background and visible foreground")
                            actual_alpha_hash = hashlib.sha256(alpha.tobytes()).hexdigest()
                            if isinstance(alpha_hash, str) and SHA256_PATTERN.fullmatch(alpha_hash) and actual_alpha_hash.lower() != alpha_hash.lower():
                                _error(errors, "SPEC_ICON_ALPHA_MASK_MISMATCH", f"{path}.alpha_mask_sha256", "alpha mask hash does not match the current icon asset")
                            foreground = alpha.getbbox()
                            if foreground is not None and (
                                foreground[0] == 0
                                or foreground[1] == 0
                                or foreground[2] == image.width
                                or foreground[3] == image.height
                            ):
                                _error(errors, "SPEC_ICON_FOREGROUND_TOUCHES_EDGE", f"{path}.asset_path", "visible icon pixels must not touch the crop boundary")
                        if (
                            source_path is not None
                            and source_path.is_absolute()
                            and source_path.is_file()
                            and _valid_bbox(source_bbox)
                            and isinstance(padding, int)
                            and padding >= 0
                        ):
                            with Image.open(source_path.resolve()) as source_image:
                                source_image.load()
                                left = source_bbox[0] - padding
                                top = source_bbox[1] - padding
                                right = source_bbox[0] + source_bbox[2] + padding
                                bottom = source_bbox[1] + source_bbox[3] + padding
                                source_crop = source_image.convert("RGB").crop(
                                    (left, top, right, bottom)
                                )
                            asset_rgb = image.convert("RGB")
                            if (
                                source_crop.size == asset_rgb.size
                                and source_crop.tobytes() != asset_rgb.tobytes()
                            ):
                                _error(
                                    errors,
                                    "SPEC_ICON_RGB_MISMATCH",
                                    f"{path}.asset_path",
                                    "asset RGB pixels must exactly match the bound source crop",
                                )
                except (OSError, UnidentifiedImageError):
                    _error(errors, "SPEC_ICON_ALPHA_CONTENT_INVALID", f"{path}.asset_path", "icon asset is not a readable PNG")

        final_width = item.get("final_width")
        final_height = item.get("final_height")
        if not isinstance(final_width, int) or final_width <= 0 or not isinstance(final_height, int) or final_height <= 0:
            _error(errors, "SPEC_ICON_ASSET_DIMENSIONS_INVALID", path, "final_width and final_height must be positive integers")
        elif _valid_bbox(source_bbox) and isinstance(padding, int) and padding >= 0:
            expected_width = source_bbox[2] + padding * 2
            expected_height = source_bbox[3] + padding * 2
            if final_width != expected_width or final_height != expected_height:
                _error(errors, "SPEC_ICON_ASSET_DIMENSIONS_INVALID", path, "asset dimensions must equal crop bbox plus padding")
        if not isinstance(item.get("sharpness"), str) or not item["sharpness"]:
            _error(errors, "SPEC_ICON_SHARPNESS_INVALID", f"{path}.sharpness", "must be non-empty")

        if item.get("validation") != "passed":
            _error(errors, "SPEC_ICON_VALIDATION_INVALID", f"{path}.validation", "must be passed")
        if item.get("native_redraw") is not False:
            _error(errors, "SPEC_ICON_NATIVE_REDRAW_INVALID", f"{path}.native_redraw", "must be false")
        if not isinstance(item.get("selectable_picture_verified"), bool):
            _error(errors, "SPEC_ICON_SELECTABILITY_INVALID", f"{path}.selectable_picture_verified", "must be boolean")
        elif stage == "final" and not item["selectable_picture_verified"]:
            _error(errors, "SPEC_ICON_SELECTABILITY_NOT_VERIFIED", f"{path}.selectable_picture_verified", "final spec requires independently selectable picture")
        if item.get("object_type") != "picture":
            _error(errors, "SPEC_ICON_OBJECT_TYPE_INVALID", f"{path}.object_type", "must be picture")

    missing_elements = sorted(icon_element_ids - seen_element_ids)
    if missing_elements:
        _error(errors, "SPEC_ICON_ELEMENT_MISSING", "modules.icons.icons", f"missing icon records: {', '.join(missing_elements)}")


def validate_spec(spec: Any, stage: str = "prebuild") -> dict[str, Any]:
    """Return a stable validation report for a reconstruction specification."""
    if stage not in {"prebuild", "final"}:
        raise ValueError("stage must be prebuild or final")
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(spec, dict):
        return {
            "valid": False,
            "stage": stage,
            "verification_profile": "strict",
            "errors": [{"code": "SPEC_ROOT_INVALID", "path": "$", "detail": "root must be an object"}],
            "warnings": [],
        }

    verification_profile = _verification_profile(spec)
    _validate_verification_identity(spec, verification_profile, errors)

    required_top = {
        "schema_version",
        "page_id",
        "session_reuse",
        "content_reference",
        "clean_visual_reference",
        "canvas",
        "activated_modules",
        "modules",
        "regions",
        "elements",
        "reading_order",
        "visual_gate",
        "editability_gate",
    }
    missing_top = sorted(required_top - set(spec))
    if missing_top:
        _error(errors, "SPEC_TOP_LEVEL_FIELD_MISSING", "$", f"missing fields: {', '.join(missing_top)}")

    if spec.get("schema_version") != 2:
        _error(errors, "SPEC_SCHEMA_VERSION_UNSUPPORTED", "schema_version", "expected schema_version 2")
    if not isinstance(spec.get("page_id"), str) or not re.fullmatch(r"page-\d{3}", spec.get("page_id", "")):
        _error(errors, "SPEC_PAGE_ID_INVALID", "page_id", "expected page-NNN")

    session = spec.get("session_reuse")
    if not isinstance(session, dict) or session.get("mode") not in {"fresh_reconstruction", "same_session_reuse"}:
        _error(errors, "SPEC_SESSION_REUSE_INVALID", "session_reuse", "invalid session reuse mode")
    elif session["mode"] == "fresh_reconstruction" and session.get("artifacts") != []:
        _error(errors, "SPEC_SESSION_REUSE_INVALID", "session_reuse.artifacts", "fresh reconstruction requires no artifacts")
    elif session["mode"] == "same_session_reuse":
        artifacts = session.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            _error(errors, "SPEC_SESSION_ARTIFACTS_INVALID", "session_reuse.artifacts", "same-session reuse requires verified artifacts")
        else:
            for index, artifact in enumerate(artifacts):
                path = f"session_reuse.artifacts[{index}]"
                if not isinstance(artifact, dict) or artifact.get("identity_verified") is not True:
                    _error(errors, "SPEC_SESSION_ARTIFACTS_INVALID", path, "artifact requires identity_verified: true")
                    continue
                _validate_reference(artifact, path, errors)

    canvas = spec.get("canvas")
    if not isinstance(canvas, dict):
        _error(errors, "SPEC_CANVAS_INVALID", "canvas", "canvas must be an object")
    else:
        for key in ("source_size", "visual_size", "slide_size_emu"):
            if not _valid_size(canvas.get(key)):
                _error(errors, "SPEC_CANVAS_FIELD_INVALID", f"canvas.{key}", "expected two positive numbers")
        if not _valid_bbox(canvas.get("page_frame_bbox")):
            _error(errors, "SPEC_CANVAS_FIELD_INVALID", "canvas.page_frame_bbox", "invalid bbox")
        if not isinstance(canvas.get("mapping_mode"), str) or not canvas.get("mapping_mode"):
            _error(errors, "SPEC_CANVAS_FIELD_INVALID", "canvas.mapping_mode", "mapping_mode is required")
        if not isinstance(canvas.get("background"), str) or not canvas.get("background"):
            _error(errors, "SPEC_CANVAS_FIELD_INVALID", "canvas.background", "background is required")
    _validate_reference(
        spec.get("content_reference"),
        "content_reference",
        errors,
        canvas.get("source_size") if isinstance(canvas, dict) else None,
    )
    _validate_reference(
        spec.get("clean_visual_reference"),
        "clean_visual_reference",
        errors,
        canvas.get("visual_size") if isinstance(canvas, dict) else None,
    )

    elements = spec.get("elements")
    element_ids: set[str] = set()
    element_map: dict[str, dict[str, Any]] = {}
    if not isinstance(elements, list) or not elements:
        _error(errors, "SPEC_ELEMENTS_INVALID", "elements", "elements must be non-empty")
    else:
        required_element = {
            "element_id",
            "kind",
            "source_bbox",
            "slide_bbox",
            "layer",
            "editable",
            "confidence",
            "style",
            "content",
        }
        for index, element in enumerate(elements):
            path = f"elements[{index}]"
            if not isinstance(element, dict):
                _error(errors, "SPEC_ELEMENT_INVALID", path, "element must be an object")
                continue
            missing = sorted(required_element - set(element))
            if missing:
                _error(errors, "SPEC_ELEMENT_FIELD_MISSING", path, f"missing fields: {', '.join(missing)}")
                continue
            element_id = element.get("element_id")
            if not isinstance(element_id, str) or not element_id or element_id in element_ids:
                _error(errors, "SPEC_ELEMENT_ID_INVALID", f"{path}.element_id", "element_id must be unique and non-empty")
            else:
                element_ids.add(element_id)
                element_map[element_id] = element
            if element.get("kind") not in ALLOWED_KINDS:
                _error(errors, "SPEC_ELEMENT_KIND_INVALID", f"{path}.kind", "unsupported kind")
            if not _valid_bbox(element.get("source_bbox")) or not _valid_bbox(element.get("slide_bbox")):
                _error(errors, "SPEC_ELEMENT_BBOX_INVALID", path, "source_bbox and slide_bbox must be valid")
            elif _slide_bbox_unit_suspect(
                element.get("source_bbox"),
                element.get("slide_bbox"),
                canvas,
                element.get("kind"),
            ):
                _error(
                    errors,
                    "SPEC_SLIDE_BBOX_UNIT_SUSPECT",
                    f"{path}.slide_bbox",
                    "slide_bbox scale is inconsistent with source_bbox and may use pixel coordinates",
                )
            elif _bbox_mapping_invalid(element.get("source_bbox"), element.get("slide_bbox"), canvas):
                _error(errors, "SPEC_SLIDE_BBOX_MAPPING_INVALID", f"{path}.slide_bbox", "slide bbox does not match canvas mapping")
            if isinstance(canvas, dict):
                if not _bbox_in_bounds(element.get("source_bbox"), canvas.get("visual_size")):
                    _error(errors, "SPEC_ELEMENT_BBOX_OUT_OF_BOUNDS", f"{path}.source_bbox", "source bbox exceeds visual canvas")
                if not _bbox_in_bounds(element.get("slide_bbox"), canvas.get("slide_size_emu")):
                    _error(errors, "SPEC_ELEMENT_BBOX_OUT_OF_BOUNDS", f"{path}.slide_bbox", "slide bbox exceeds slide canvas")
            if not isinstance(element.get("layer"), int):
                _error(errors, "SPEC_ELEMENT_LAYER_INVALID", f"{path}.layer", "layer must be integer")
            if not isinstance(element.get("editable"), bool):
                _error(errors, "SPEC_ELEMENT_EDITABLE_INVALID", f"{path}.editable", "editable must be boolean")
            if element.get("confidence") not in ALLOWED_CONFIDENCE:
                _error(errors, "SPEC_ELEMENT_CONFIDENCE_INVALID", f"{path}.confidence", "invalid confidence")
            if not isinstance(element.get("style"), dict) or not isinstance(element.get("content"), dict):
                _error(errors, "SPEC_ELEMENT_PAYLOAD_INVALID", path, "style and content must be objects")

    regions = spec.get("regions")
    region_element_ids: set[str] = set()
    if not isinstance(regions, list) or not regions:
        _error(errors, "SPEC_REGIONS_INVALID", "regions", "regions must be non-empty")
    else:
        region_ids: set[str] = set()
        for index, region in enumerate(regions):
            path = f"regions[{index}]"
            if not isinstance(region, dict):
                _error(errors, "SPEC_REGION_INVALID", path, "region must be an object")
                continue
            required = {"region_id", "source_bbox", "slide_bbox", "layer", "padding", "element_ids"}
            missing = sorted(required - set(region))
            if missing:
                _error(errors, "SPEC_REGION_FIELD_MISSING", path, f"missing fields: {', '.join(missing)}")
                continue
            region_id = region.get("region_id")
            if not isinstance(region_id, str) or not region_id or region_id in region_ids:
                _error(errors, "SPEC_REGION_ID_INVALID", f"{path}.region_id", "region_id must be unique")
            else:
                region_ids.add(region_id)
            if not _valid_bbox(region.get("source_bbox")) or not _valid_bbox(region.get("slide_bbox")):
                _error(errors, "SPEC_REGION_BBOX_INVALID", path, "invalid region bbox")
            else:
                if isinstance(canvas, dict) and not _bbox_in_bounds(region.get("source_bbox"), canvas.get("visual_size")):
                    _error(errors, "SPEC_REGION_BBOX_OUT_OF_BOUNDS", f"{path}.source_bbox", "region exceeds visual canvas")
                if isinstance(canvas, dict) and not _bbox_in_bounds(region.get("slide_bbox"), canvas.get("slide_size_emu")):
                    _error(errors, "SPEC_REGION_BBOX_OUT_OF_BOUNDS", f"{path}.slide_bbox", "region exceeds slide canvas")
                if _slide_bbox_unit_suspect(region.get("source_bbox"), region.get("slide_bbox"), canvas, "shape"):
                    _error(errors, "SPEC_SLIDE_BBOX_UNIT_SUSPECT", f"{path}.slide_bbox", "region slide_bbox may use pixels")
                elif _bbox_mapping_invalid(region.get("source_bbox"), region.get("slide_bbox"), canvas):
                    _error(errors, "SPEC_SLIDE_BBOX_MAPPING_INVALID", f"{path}.slide_bbox", "region bbox does not match canvas mapping")
            references = region.get("element_ids")
            if not isinstance(references, list) or any(item not in element_ids for item in references):
                _error(errors, "SPEC_ELEMENT_REFERENCE_INVALID", f"{path}.element_ids", "unknown element reference")
            else:
                region_element_ids.update(item for item in references if isinstance(item, str))
        if region_element_ids != element_ids:
            _error(
                errors,
                "SPEC_REGION_COVERAGE_INVALID",
                "regions",
                f"regions must cover every element; missing: {', '.join(sorted(element_ids - region_element_ids))}",
            )

    reading_order = spec.get("reading_order")
    if not isinstance(reading_order, list) or not reading_order or len(reading_order) != len(set(reading_order)):
        _error(errors, "SPEC_READING_ORDER_INVALID", "reading_order", "must be non-empty and unique")
    elif any(item not in element_ids for item in reading_order):
        _error(errors, "SPEC_ELEMENT_REFERENCE_INVALID", "reading_order", "unknown element reference")
    elif set(reading_order) != element_ids:
        _error(
            errors,
            "SPEC_READING_ORDER_COVERAGE_INVALID",
            "reading_order",
            f"reading_order must cover every element; missing: {', '.join(sorted(element_ids - set(reading_order)))}",
        )

    activated = spec.get("activated_modules")
    modules = spec.get("modules")
    if not isinstance(activated, list) or len(activated) != len(set(activated)):
        _error(errors, "SPEC_ACTIVATED_MODULES_INVALID", "activated_modules", "must be a unique array")
        activated = []
    if not isinstance(modules, dict):
        _error(errors, "SPEC_MODULES_INVALID", "modules", "modules must be an object")
        modules = {}
    for module_name in activated:
        if module_name not in ALLOWED_MODULES:
            _error(errors, "SPEC_ACTIVATED_MODULE_UNKNOWN", f"activated_modules.{module_name}", "unknown module name")
        if module_name not in modules:
            _error(errors, "SPEC_ACTIVATED_MODULE_MISSING", f"modules.{module_name}", "activated module is absent")
            continue
        module = modules.get(module_name)
        if not isinstance(module, dict) or not module:
            _error(errors, "SPEC_ACTIVATED_MODULE_EMPTY", f"modules.{module_name}", "activated module must be a non-empty object")
            continue
        unknown_references = sorted(_module_element_references(module) - element_ids)
        if unknown_references:
            _error(
                errors,
                "SPEC_MODULE_ELEMENT_REFERENCE_INVALID",
                f"modules.{module_name}",
                f"unknown element references: {', '.join(unknown_references)}",
            )
    _validate_coordinate_overlay_evidence(
        modules.get("page_layout"),
        spec.get("clean_visual_reference"),
        errors,
    )
    if "typography" in activated:
        _validate_typography(modules.get("typography"), element_map, canvas, stage, errors)
    if "icons" in activated:
        _validate_icons(
            modules.get("icons"),
            element_map,
            canvas,
            spec.get("clean_visual_reference"),
            spec.get("page_id"),
            stage,
            errors,
        )

    if stage == "final":
        visual_gate = spec.get("visual_gate")
        editability_gate = spec.get("editability_gate")
        expected_delivery_status = {
            "rapid": "rapid_validated",
            "reviewed": "reviewed_passed",
            "strict": "strict_gate_passed",
        }.get(verification_profile)
        if (
            spec.get("verification_profile") is not None
            and spec.get("delivery_status") != expected_delivery_status
        ):
            _error(
                errors,
                "SPEC_DELIVERY_STATUS_INVALID",
                "delivery_status",
                f"final {verification_profile} delivery requires {expected_delivery_status}",
            )
        if verification_profile == "rapid":
            if (
                not isinstance(visual_gate, dict)
                or visual_gate.get("status") != "not_independently_reviewed"
                or not visual_gate.get("evidence")
            ):
                _error(
                    errors,
                    "SPEC_RAPID_VISUAL_STATUS_INVALID",
                    "visual_gate",
                    "rapid final requires not_independently_reviewed status and evidence",
                )
        elif not isinstance(visual_gate, dict) or visual_gate.get("status") != "passed" or not visual_gate.get("evidence"):
            _error(errors, "SPEC_VISUAL_GATE_NOT_PASSED", "visual_gate", "reviewed and strict final require passed visual status and evidence")
        if isinstance(visual_gate, dict) and isinstance(visual_gate.get("tripwire"), dict) and visual_gate["tripwire"].get("triggered"):
            _error(errors, "SPEC_VISUAL_TRIPWIRE_TRIGGERED", "visual_gate.tripwire", "triggered tripwire blocks final delivery")
        if not isinstance(editability_gate, dict) or editability_gate.get("status") != "passed" or not editability_gate.get("evidence"):
            _error(errors, "SPEC_EDITABILITY_GATE_NOT_PASSED", "editability_gate", "final editability gate requires passed status and evidence")
        review_round = visual_gate.get("review_round") if isinstance(visual_gate, dict) else None
        if verification_profile == "rapid" and review_round is not None:
            _error(
                errors,
                "SPEC_RAPID_REVIEWER_FORBIDDEN",
                "visual_gate.review_round",
                "rapid verification must not claim an independent review round",
            )
        elif verification_profile != "rapid" and (type(review_round) is not int or not 1 <= review_round <= 2):
            _error(
                errors,
                "SPEC_VISUAL_REVIEW_ROUND_INVALID",
                "visual_gate.review_round",
                "final visual review round must be an integer from 1 through 2",
            )
        for gate_name, gate in (("visual_gate", visual_gate), ("editability_gate", editability_gate)):
            evidence = gate.get("evidence") if isinstance(gate, dict) else None
            if not isinstance(evidence, list) or not evidence or not all(isinstance(item, str) and item for item in evidence):
                _error(errors, "SPEC_GATE_EVIDENCE_INVALID", f"{gate_name}.evidence", "evidence must be a non-empty string array")
        visual_review = visual_gate.get("review") if isinstance(visual_gate, dict) else None
        required_visual = {"whole_page", "title", "body", "footer", "high_risk_regions"}
        if verification_profile == "strict" and (not isinstance(visual_review, dict) or not required_visual.issubset(visual_review) or any(visual_review.get(key) != "passed" for key in ("whole_page", "title", "body", "footer")) or not isinstance(visual_review.get("high_risk_regions"), list)):
            _error(errors, "SPEC_VISUAL_REVIEW_INVALID", "visual_gate.review", "all visual review fields are required and must pass")
        reviewer = visual_gate.get("reviewer") if isinstance(visual_gate, dict) else None
        if verification_profile == "rapid" and reviewer is not None:
            _error(
                errors,
                "SPEC_RAPID_REVIEWER_FORBIDDEN",
                "visual_gate.reviewer",
                "rapid verification must not claim an independent reviewer",
            )
        elif verification_profile != "rapid" and (not isinstance(reviewer, dict) or reviewer.get("mode") != "independent_read_only_subagent"):
            _error(
                errors,
                "SPEC_INDEPENDENT_VISUAL_REVIEW_REQUIRED",
                "visual_gate.reviewer",
                "independent read-only visual review is required",
            )
        elif isinstance(reviewer, dict):
            findings = reviewer.get("findings")
            disclosures = reviewer.get("p2_disclosures")
            coverage = reviewer.get("coverage")
            decision = reviewer.get("decision")
            blocking_findings = (
                [
                    finding
                    for finding in findings
                    if isinstance(finding, dict)
                    and isinstance(finding.get("severity"), str)
                    and finding["severity"] in {"P0", "P1"}
                ]
                if isinstance(findings, list)
                else []
            )
            if (
                not isinstance(decision, str)
                or decision not in {"passed", "changes_required", "not_reviewable"}
                or not isinstance(findings, list)
                or not all(
                    isinstance(finding, dict)
                    and isinstance(finding.get("severity"), str)
                    and finding["severity"] in {"P0", "P1", "P2"}
                    and isinstance(finding.get("category"), str)
                    and finding["category"] in VISUAL_REVIEW_COVERAGE_FIELDS
                    and all(
                        isinstance(finding.get(field), str)
                        and bool(finding[field].strip())
                        for field in (
                            "location",
                            "source_fact",
                            "observed_difference",
                            "evidence",
                        )
                    )
                    for finding in findings
                )
                or not isinstance(disclosures, list)
                or (bool(blocking_findings) and decision != "changes_required")
                or (decision == "changes_required" and not blocking_findings)
            ):
                _error(
                    errors,
                    "SPEC_INDEPENDENT_VISUAL_REVIEW_INVALID",
                    "visual_gate.reviewer",
                    "reviewer decision, findings and p2_disclosures are invalid",
                )
            element_kinds = {
                element.get("kind")
                for element in element_map.values()
                if isinstance(element, dict)
            }
            required_checked = {"canvas_and_regions", "objects_and_geometry"}
            if element_kinds & {"text", "special_text"} or set(activated) & {"typography", "special_text"}:
                required_checked.add("text_and_typography")
            if element_kinds & {"table", "matrix"}:
                required_checked.add("tables_and_matrices")
            if element_kinds & {"shape", "line", "status", "diagram", "chart"} or set(activated) & {"graphics", "diagram", "chart"}:
                required_checked.add("graphics_connectors_charts")
            if element_kinds & {"icon", "picture"} or set(activated) & {"icons", "picture_framing"}:
                required_checked.add("pictures_crop_layers")
            high_risk_module = modules.get("high_risk")
            if (
                "high_risk" in activated
                and isinstance(high_risk_module, dict)
                and high_risk_module.get("items")
            ):
                required_checked.add("high_risk_regions")
            coverage_valid = (
                isinstance(coverage, dict)
                and set(coverage) == VISUAL_REVIEW_COVERAGE_FIELDS
                and all(
                    isinstance(value, str)
                    and value in VISUAL_REVIEW_COVERAGE_RESULTS
                    for value in coverage.values()
                )
                and all(coverage.get(category) == "checked" for category in required_checked)
                and not (
                    reviewer.get("decision") == "passed"
                    and "not_reviewable" in coverage.values()
                )
            )
            if not coverage_valid:
                _error(
                    errors,
                    "SPEC_VISUAL_REVIEW_COVERAGE_INVALID",
                    "visual_gate.reviewer.coverage",
                    "coverage must contain exactly the required categories, mark applicable categories checked, and a passing review cannot contain not_reviewable",
                )
            if reviewer.get("decision") != "passed" or (
                isinstance(findings, list)
                and any(
                    isinstance(finding, dict)
                    and isinstance(finding.get("severity"), str)
                    and finding["severity"] in {"P0", "P1"}
                    for finding in findings
                )
            ):
                _error(
                    errors,
                    "SPEC_OPEN_BLOCKING_DIFFERENCE",
                    "visual_gate.reviewer",
                    "open P0/P1 or a non-passing reviewer decision blocks final delivery",
                )
            preview_hash = (
                visual_gate.get("preview", {}).get("sha256")
                if isinstance(visual_gate.get("preview"), dict)
                else None
            )
            if reviewer.get("preview_sha256") != preview_hash:
                _error(
                    errors,
                    "SPEC_VISUAL_REVIEW_PREVIEW_MISMATCH",
                    "visual_gate.reviewer.preview_sha256",
                    "review must bind the current preview",
                )
            source_hash = (
                spec.get("clean_visual_reference", {}).get("sha256")
                if isinstance(spec.get("clean_visual_reference"), dict)
                else None
            )
            if reviewer.get("source_sha256") != source_hash:
                _error(
                    errors,
                    "SPEC_VISUAL_REVIEW_SOURCE_MISMATCH",
                    "visual_gate.reviewer.source_sha256",
                    "review must bind the current visual source",
                )
            if reviewer.get("page_id") != spec.get("page_id"):
                _error(
                    errors,
                    "SPEC_VISUAL_REVIEW_PAGE_MISMATCH",
                    "visual_gate.reviewer.page_id",
                    "review must bind the current page_id",
                )
        edit_review = editability_gate.get("review") if isinstance(editability_gate, dict) else None
        required_edit = {
            "text_and_data",
            "native_text_structure",
            "basic_structure",
            "full_slide_picture_risk",
        }
        if not isinstance(edit_review, dict) or not required_edit.issubset(edit_review) or any(edit_review.get(key) != "passed" for key in required_edit):
            _error(errors, "SPEC_EDITABILITY_REVIEW_INVALID", "editability_gate.review", "all editability review fields are required and must pass")
        tripwire = visual_gate.get("tripwire") if isinstance(visual_gate, dict) else None
        tripwire_valid = False
        if isinstance(tripwire, dict):
            if tripwire.get("available") is False:
                tripwire_valid = (
                    tripwire.get("triggered") is None
                    and tripwire.get("reason") == "no_approved_baseline"
                )
            elif tripwire.get("available") is True:
                tripwire_valid = tripwire.get("triggered") is False
        if not tripwire_valid:
            _error(
                errors,
                "SPEC_VISUAL_TRIPWIRE_INVALID",
                "visual_gate.tripwire",
                "tripwire must be unavailable with triggered=null or available and explicitly untriggered",
            )
        visual_pptx = _validate_gate_artifact(
            visual_gate.get("pptx") if isinstance(visual_gate, dict) else None,
            "visual_gate.pptx",
            errors,
        )
        preview_artifact = _validate_gate_artifact(
            visual_gate.get("preview") if isinstance(visual_gate, dict) else None,
            "visual_gate.preview",
            errors,
        )
        report_artifact = _validate_gate_artifact(
            visual_gate.get("report") if isinstance(visual_gate, dict) else None,
            "visual_gate.report",
            errors,
        )
        if not isinstance(visual_gate, dict) or not isinstance(
            visual_gate.get("render_report"), dict
        ):
            _error(
                errors,
                "SPEC_RENDER_REPORT_MISSING",
                "visual_gate.render_report",
                "final validation requires the current render-report.json identity",
            )
        render_report_artifact = _validate_gate_artifact(
            visual_gate.get("render_report") if isinstance(visual_gate, dict) else None,
            "visual_gate.render_report",
            errors,
        )
        if not isinstance(spec.get("runtime_preflight"), dict):
            _error(
                errors,
                "SPEC_RUNTIME_PREFLIGHT_MISSING",
                "runtime_preflight",
                "final validation requires the fixed LibreOffice preflight identity",
            )
        preflight_artifact = _validate_gate_artifact(
            spec.get("runtime_preflight"),
            "runtime_preflight",
            errors,
        )
        editability_pptx = _validate_gate_artifact(
            editability_gate.get("pptx") if isinstance(editability_gate, dict) else None,
            "editability_gate.pptx",
            errors,
        )
        validator_artifact = _validate_gate_artifact(
            editability_gate.get("validator") if isinstance(editability_gate, dict) else None,
            "editability_gate.validator",
            errors,
        )
        expected_native_list_contracts = sum(
            1
            for item in (
                modules.get("typography", {}).get("items", [])
                if isinstance(modules.get("typography"), dict)
                else []
            )
            if isinstance(item, dict)
            and isinstance(item.get("paragraphs"), list)
            and any(
                isinstance(paragraph, dict)
                and isinstance(paragraph.get("list"), dict)
                and paragraph["list"].get("is_list") is True
                for paragraph in item["paragraphs"]
            )
        )
        _validate_validator_report(
            validator_artifact,
            expected_native_list_contracts,
            editability_pptx[1] if editability_pptx else None,
            errors,
        )
        _validate_image_artifact(
            preview_artifact,
            "SPEC_PREVIEW_IMAGE_INVALID",
            "visual_gate.preview",
            errors,
        )
        render_payload = _validate_render_report(
            render_report_artifact,
            visual_pptx,
            preview_artifact,
            preflight_artifact,
            errors,
        )
        _validate_font_traces_against_render(
            modules.get("typography") if isinstance(modules, dict) else None,
            render_payload,
            errors,
        )
        verified_visual_evidence = _validate_visual_diff_report(
            report_artifact,
            spec,
            preview_artifact,
            render_report_artifact,
            visual_pptx,
            errors,
            require_all_regions=verification_profile == "strict",
        )
        visual_evidence = visual_gate.get("evidence") if isinstance(visual_gate, dict) else None
        if isinstance(visual_evidence, list):
            for index, evidence_path in enumerate(visual_evidence):
                if evidence_path not in verified_visual_evidence and Path(str(evidence_path)).name not in verified_visual_evidence:
                    _error(
                        errors,
                        "SPEC_GATE_EVIDENCE_INVALID",
                        f"visual_gate.evidence[{index}]",
                        "visual gate evidence must come from the verified visual-diff report",
                    )
        editability_evidence = editability_gate.get("evidence") if isinstance(editability_gate, dict) else None
        validator_names = (
            {validator_artifact[0], Path(validator_artifact[0]).name}
            if validator_artifact
            else set()
        )
        if isinstance(editability_evidence, list):
            for index, evidence_path in enumerate(editability_evidence):
                if evidence_path not in validator_names and Path(str(evidence_path)).name not in validator_names:
                    _error(
                        errors,
                        "SPEC_GATE_EVIDENCE_INVALID",
                        f"editability_gate.evidence[{index}]",
                        "editability evidence must identify the verified validator report",
                    )
        if isinstance(reviewer, dict) and isinstance(reviewer.get("findings"), list):
            for index, finding in enumerate(reviewer["findings"]):
                evidence_path = finding.get("evidence") if isinstance(finding, dict) else None
                if (
                    isinstance(evidence_path, str)
                    and evidence_path
                    and evidence_path not in verified_visual_evidence
                    and Path(evidence_path).name not in verified_visual_evidence
                ):
                    _error(
                        errors,
                        "SPEC_FINDING_EVIDENCE_INVALID",
                        f"visual_gate.reviewer.findings[{index}].evidence",
                        "finding evidence must identify current verified visual-diff evidence",
                    )
        if visual_pptx and editability_pptx and visual_pptx != editability_pptx:
            _error(
                errors,
                "SPEC_GATE_PPTX_IDENTITY_MISMATCH",
                "visual_gate.pptx",
                "visual and editability gates must reference the same current PPTX",
            )
        _rerun_pptx_validator(editability_pptx or visual_pptx, spec, errors)
        high_risk = modules.get("high_risk") if isinstance(modules, dict) else None
        items = high_risk.get("items", []) if isinstance(high_risk, dict) else []
        if "high_risk" in activated and (not isinstance(high_risk, dict) or not isinstance(high_risk.get("items"), list)):
            _error(errors, "SPEC_HIGH_RISK_ITEMS_INVALID", "modules.high_risk.items", "activated high_risk requires an items array")
            items = []
        required_risk = {"risk_id", "source", "scope", "category", "expected", "strategy", "result", "evidence", "confidence", "severity", "verification"}
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not required_risk.issubset(item) or item.get("result") not in {"passed", "changes_required", "visual_approximation", "not_verifiable"} or item.get("severity") not in {"P0", "P1", "P2"} or item.get("confidence") not in ALLOWED_CONFIDENCE:
                _error(errors, "SPEC_HIGH_RISK_ITEM_INVALID", f"modules.high_risk.items[{index}]", "high-risk item fields or enums are invalid")
            if (
                isinstance(item, dict)
                and item.get("severity") in {"P0", "P1"}
                and item.get("result") != "passed"
            ):
                _error(
                    errors,
                    "SPEC_OPEN_BLOCKING_DIFFERENCE",
                    f"modules.high_risk.items[{index}]",
                    "open P0/P1 blocks final delivery",
                )

    return {
        "valid": not errors,
        "stage": stage,
        "verification_profile": verification_profile,
        "errors": errors,
        "warnings": warnings,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="Path to page-reconstruction.json")
    parser.add_argument("--stage", choices=("prebuild", "final"), default="prebuild")
    parser.add_argument(
        "--output",
        type=Path,
        help="atomically save the same JSON emitted to stdout",
    )
    return parser.parse_args(argv)


def _emit_json(payload: dict[str, Any], output: Path | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, output)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    print(text, end="")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        result = validate_spec(spec, stage=args.stage)
    except (OSError, json.JSONDecodeError) as exc:
        result = {
            "valid": False,
            "stage": args.stage,
            "errors": [{"code": "SPEC_FILE_INVALID", "path": str(args.spec), "detail": str(exc)}],
            "warnings": [],
        }
    _emit_json(result, args.output)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
