"""Shared current-schema fixtures for schema v2 compiler tests."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def _load_script(name: str, module_name: str):
    script_path = SCRIPTS_ROOT / name
    module_spec = importlib.util.spec_from_file_location(module_name, script_path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"cannot load {script_path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def _identity(path: Path) -> dict[str, Any]:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _coordinate_evidence(root: Path, reference: Path) -> dict[str, Any]:
    overlay = root / "coordinate-overlay.png"
    coordinate_overlay = _load_script(
        "create_coordinate_overlay.py", "fixture_coordinate_overlay"
    )
    report = coordinate_overlay.create_coordinate_overlay(reference, overlay)
    return {
        "path": str(overlay.resolve()),
        "sha256": hashlib.sha256(overlay.read_bytes()).hexdigest(),
        "source_sha256": report["source"]["sha256"],
        "manifest_sha256": report["coordinate_overlay_manifest_sha256"],
        "grid": report["grid"],
        "inspection": "passed",
    }


def make_png_asset(root: Path, name: str = "asset.png") -> dict[str, Any]:
    """Create a fixed 16x16 RGBA PNG and return its asset contract."""
    root.mkdir(parents=True, exist_ok=True)
    asset_path = root / name
    Image.new("RGBA", (16, 16), (32, 96, 192, 255)).save(asset_path)
    return {
        "path": str(asset_path.resolve()),
        "asset_sha256": hashlib.sha256(asset_path.read_bytes()).hexdigest(),
        "pixel_size": [16, 16],
    }


def make_minimal_spec(root: Path, *, kind: str = "text") -> dict[str, Any]:
    """Build a current, minimal 16:9 schema v2 prebuild fixture."""
    root.mkdir(parents=True, exist_ok=True)
    reference = root / "source.png"
    Image.new("RGB", (1600, 900), "white").save(reference)
    representation_evidence = root / "representation-evidence.json"
    representation_evidence.write_text("{}\n", encoding="utf-8")
    reference_identity = _identity(reference)
    source_bbox = [30, 30, 800, 60]
    slide_bbox = [228600, 228600, 6096000, 457200]
    element_id = "element-001"
    text = "测试标题"
    background_id = "background-base"
    background: dict[str, Any] = {
        "element_id": background_id,
        "kind": "shape",
        "source_bbox": [0, 0, 1600, 900],
        "slide_bbox": [0, 0, 12192000, 6858000],
        "layer": 0,
        "editable": True,
        "confidence": "high",
        "style": {
            "shape_type": "rectangle",
            "fill": {"type": "solid", "color": "#FFFFFF", "opacity": 1},
            "effects": "none",
            "rotation": 0,
        },
        "content": {},
    }
    element: dict[str, Any] = {
        "element_id": element_id,
        "kind": kind,
        "source_bbox": source_bbox,
        "slide_bbox": slide_bbox,
        "layer": 10,
        "editable": True,
        "confidence": "high",
        "style": {"fill": "noFill"},
        "content": {"text": text},
    }
    typography_item = {
        "element_id": element_id,
        "text": text,
        "source_font_guess": "Noto Sans CJK SC",
        "selected_font": "Noto Sans CJK SC",
        "fallback_reason": None,
        "fallback_trace": None,
        "runs": [
            {
                "start": 0,
                "end": len(text),
                "font_size": 24,
                "font_weight": 700,
                "color": "#000000",
                "letter_spacing": 0,
                "italic": False,
                "underline": False,
                "strike": False,
                "baseline": 0,
            }
        ],
        "paragraphs": [
            {
                "start": 0,
                "end": len(text),
                "alignment": "left",
                "line_spacing": 1.0,
                "space_before": 0,
                "space_after": 0,
                "indent": 0,
                "list": {"is_list": False, "level": 0, "bullet": None},
            }
        ],
        "text_box": {
            "x": slide_bbox[0],
            "y": slide_bbox[1],
            "w": slide_bbox[2],
            "h": slide_bbox[3],
            "margins": {"left": 0, "right": 0, "top": 0, "bottom": 0},
            "alignment": "left",
            "vertical_alignment": "top",
            "wrap": False,
            "overflow": False,
            "soft_breaks": [],
            "paragraph_breaks": [],
        },
        "internal_font_declaration": "Noto Sans CJK SC",
        "font_declaration_verified": False,
    }
    return {
        "schema_version": 2,
        "page_id": "page-001",
        "verification_profile": "rapid",
        "delivery_status": "pending",
        "session_reuse": {
            "mode": "fresh_reconstruction",
            "reason": "new_session",
            "artifacts": [],
        },
        "content_reference": copy.deepcopy(reference_identity),
        "clean_visual_reference": copy.deepcopy(reference_identity),
        "canvas": {
            "source_size": [1600, 900],
            "visual_size": [1600, 900],
            "page_frame_bbox": [0, 0, 1600, 900],
            "slide_size_emu": [12192000, 6858000],
            "mapping_mode": "direct_16_9",
            "background": "#FFFFFF",
        },
        "activated_modules": [
            "page_layout",
            "typography",
            "representation_plan",
            "background",
        ],
        "modules": {
            "page_layout": {
                "anchors": [],
                "relationships": [],
                "layout_invariants": [],
                "density_targets": {},
                "coordinate_overlay_evidence": _coordinate_evidence(root, reference),
            },
            "typography": {"slide_coordinate_unit": "EMU", "items": [typography_item]},
            "representation_plan": {
                "items": [
                    {
                        "source_fact_id": "fact-001",
                        "semantic_role": kind,
                        "source_bbox": list(source_bbox),
                        "required": True,
                        "selected_mode": "native",
                        "required_editability": "full",
                        "fallback_policy": "forbid",
                        "bound_element_ids": [element_id],
                        "reason": "minimal compiler fixture",
                        "coverage_status": "covered",
                        "evidence": [str(representation_evidence.resolve())],
                    }
                ]
            },
            "background": {
                "items": [
                    {
                        "background_id": "background-001",
                        "role": "base",
                        "source_bbox": [0, 0, 1600, 900],
                        "selected_mode": "native",
                        "bound_element_id": background_id,
                        "source_provenance": {
                            "kind": "native_measurement",
                            "source_path": reference_identity["path"],
                            "source_sha256": reference_identity["sha256"],
                        },
                        "reason": "measured solid page background",
                        "evidence": [reference_identity["path"]],
                        "contains_foreground_semantics": False,
                    }
                ]
            },
        },
        "regions": [
            {
                "region_id": "region-001",
                "source_bbox": [0, 0, 1600, 900],
                "slide_bbox": [0, 0, 12192000, 6858000],
                "layer": 0,
                "padding": {"left": 0, "right": 0, "top": 0, "bottom": 0},
                "element_ids": [background_id, element_id],
            }
        ],
        "elements": [element, background],
        "reading_order": [background_id, element_id],
        "visual_gate": {"status": "pending", "evidence": [], "tripwire": None},
        "editability_gate": {"status": "pending", "evidence": []},
    }


def make_asset_fallback_spec(
    root: Path, *, required_editability: str = "none"
) -> dict[str, Any]:
    """Build an asset fallback fact with its exact local picture and labels."""
    spec = make_minimal_spec(root)
    asset = make_png_asset(root, "artwork.png")
    picture_id = "artwork-picture"
    source_bbox = [120, 140, 240, 180]
    slide_bbox = [914400, 1066800, 1828800, 1371600]
    spec["elements"].append(
        {
            "element_id": picture_id,
            "kind": "picture",
            "source_bbox": list(source_bbox),
            "slide_bbox": list(slide_bbox),
            "layer": 20,
            "editable": False,
            "confidence": "high",
            "style": {"rotation": 0, "opacity": 1},
            "content": {
                "asset": asset,
                "mode": "none",
                "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            },
        }
    )
    spec["regions"][0]["element_ids"].append(picture_id)
    spec["reading_order"].append(picture_id)
    bindings = [picture_id]
    if required_editability == "labels_only":
        bindings.append("element-001")
    spec["modules"]["representation_plan"]["items"].append(
        {
            "source_fact_id": "fact-artwork",
            "semantic_role": "artwork",
            "source_bbox": list(source_bbox),
            "required": True,
            "selected_mode": "asset",
            "required_editability": required_editability,
            "fallback_policy": "allow_minimal_asset",
            "bound_element_ids": bindings,
            "reason": "complex local artwork requires a minimal raster asset",
            "coverage_status": "covered",
            "evidence": [
                str((root / "representation-evidence.json").resolve())
            ],
        }
    )
    return spec


def write_valid_fixture(root: Path, spec: dict[str, Any]) -> tuple[Path, Path]:
    """Write a spec, validate its real prebuild contract, and write the report."""
    root.mkdir(parents=True, exist_ok=True)
    spec_path = root / "page-reconstruction.json"
    report_path = root / "prebuild-report.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    validator = _load_script("validate_reconstruction_spec.py", "fixture_spec_validator")
    report = validator.validate_spec(spec, stage="prebuild")
    if not report["valid"]:
        raise AssertionError(f"fixture must pass prebuild: {report['errors']!r}")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return spec_path, report_path
