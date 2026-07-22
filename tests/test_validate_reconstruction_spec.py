from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.util import Pt


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_reconstruction_spec.py"
SPEC = importlib.util.spec_from_file_location("validate_reconstruction_spec", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load {SCRIPT_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

PPTX_VALIDATOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_pptx.py"
PPTX_SPEC = importlib.util.spec_from_file_location("validate_pptx_for_spec_tests", PPTX_VALIDATOR_PATH)
if PPTX_SPEC is None or PPTX_SPEC.loader is None:
    raise RuntimeError(f"Cannot load {PPTX_VALIDATOR_PATH}")
PPTX_VALIDATOR = importlib.util.module_from_spec(PPTX_SPEC)
PPTX_SPEC.loader.exec_module(PPTX_VALIDATOR)

VISUAL_DIFF_PATH = Path(__file__).resolve().parents[1] / "scripts" / "create_visual_diff.py"
VISUAL_DIFF_SPEC = importlib.util.spec_from_file_location("visual_diff_for_spec_tests", VISUAL_DIFF_PATH)
if VISUAL_DIFF_SPEC is None or VISUAL_DIFF_SPEC.loader is None:
    raise RuntimeError(f"Cannot load {VISUAL_DIFF_PATH}")
VISUAL_DIFF = importlib.util.module_from_spec(VISUAL_DIFF_SPEC)
VISUAL_DIFF_SPEC.loader.exec_module(VISUAL_DIFF)


REFERENCE_ROOT: tempfile.TemporaryDirectory[str] | None = None
REFERENCE_PATH: Path | None = None


def setUpModule() -> None:
    global REFERENCE_ROOT, REFERENCE_PATH
    REFERENCE_ROOT = tempfile.TemporaryDirectory()
    REFERENCE_PATH = Path(REFERENCE_ROOT.name) / "source.png"
    Image.new("RGB", (1600, 900), "white").save(REFERENCE_PATH)


def tearDownModule() -> None:
    global REFERENCE_ROOT, REFERENCE_PATH
    if REFERENCE_ROOT is not None:
        REFERENCE_ROOT.cleanup()
    REFERENCE_ROOT = None
    REFERENCE_PATH = None


def image_identity(path: Path) -> dict:
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def valid_spec() -> dict:
    text = "标题"
    if REFERENCE_PATH is None:
        raise RuntimeError("reference fixture is not initialized")
    reference = image_identity(REFERENCE_PATH)
    return {
        "schema_version": 2,
        "page_id": "page-001",
        "session_reuse": {
            "mode": "fresh_reconstruction",
            "reason": "new_session",
            "artifacts": [],
        },
        "content_reference": dict(reference),
        "clean_visual_reference": dict(reference),
        "canvas": {
            "source_size": [1600, 900],
            "visual_size": [1600, 900],
            "page_frame_bbox": [0, 0, 1600, 900],
            "slide_size_emu": [12192000, 6858000],
            "mapping_mode": "direct_16_9",
            "background": "#FFFFFF",
        },
        "activated_modules": ["page_layout", "typography"],
        "modules": {
            "page_layout": {
                "anchors": [],
                "relationships": [],
                "layout_invariants": [],
                "density_targets": {},
            },
            "typography": {
                "slide_coordinate_unit": "EMU",
                "items": [
                    {
                        "element_id": "title",
                        "text": text,
                        "source_font_guess": "Noto Sans CJK SC",
                        "candidates": ["Noto Sans CJK SC"],
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
                                "decoration": "none",
                                "letter_spacing": 0,
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
                            "x": 228600,
                            "y": 228600,
                            "w": 6096000,
                            "h": 457200,
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
                ],
            },
        },
        "regions": [
            {
                "region_id": "header",
                "source_bbox": [0, 0, 1600, 120],
                "slide_bbox": [0, 0, 12192000, 914400],
                "layer": 1,
                "padding": {"left": 0, "right": 0, "top": 0, "bottom": 0},
                "element_ids": ["title"],
            }
        ],
        "elements": [
            {
                "element_id": "title",
                "kind": "text",
                "source_bbox": [30, 30, 800, 60],
                "slide_bbox": [228600, 228600, 6096000, 457200],
                "layer": 2,
                "editable": True,
                "confidence": "high",
                "style": {"fill": "noFill"},
                "content": {"text": text},
            }
        ],
        "reading_order": ["title"],
        "visual_gate": {"status": "pending", "evidence": [], "tripwire": None},
        "editability_gate": {"status": "pending", "evidence": []},
    }


def valid_list_spec() -> dict:
    candidate = valid_spec()
    text = "第一项第二项"
    candidate["elements"][0]["content"]["text"] = text
    item = candidate["modules"]["typography"]["items"][0]
    item["text"] = text
    item["runs"][0]["end"] = len(text)
    item["paragraphs"] = [
        {
            "start": 0,
            "end": 3,
            "alignment": "left",
            "line_spacing": 1.0,
            "space_before": 0,
            "space_after": 0,
            "margin_left": 342900,
            "indent": -228600,
            "list": {
                "is_list": True,
                "level": 0,
                "bullet_type": "char",
                "bullet": "•",
                "bullet_font": "follow_text",
                "bullet_size_mode": "follow_text",
                "bullet_size_value": None,
                "bullet_color": "follow_text",
            },
        },
        {
            "start": 3,
            "end": len(text),
            "alignment": "left",
            "line_spacing": 1.0,
            "space_before": 0,
            "space_after": 0,
            "margin_left": 342900,
            "indent": -228600,
            "list": {
                "is_list": True,
                "level": 0,
                "bullet_type": "char",
                "bullet": "•",
                "bullet_font": "follow_text",
                "bullet_size_mode": "follow_text",
                "bullet_size_value": None,
                "bullet_color": "follow_text",
            },
        },
    ]
    item["text_box"]["paragraph_breaks"] = [3]
    return candidate


class ValidateReconstructionSpecTests(unittest.TestCase):
    def _artifact(self, path: Path, payload: bytes) -> dict:
        path.write_bytes(payload)
        return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}

    def _attach_final_gates(self, candidate: dict, root: Path, validator_payload: dict) -> None:
        candidate["modules"]["typography"]["items"][0]["font_declaration_verified"] = True
        presentation = Presentation()
        presentation.slide_width = 12_192_000
        presentation.slide_height = 6_858_000
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        item = candidate["modules"]["typography"]["items"][0]
        box = item["text_box"]
        text_box = slide.shapes.add_textbox(box["x"], box["y"], box["w"], box["h"])
        text_box.name = "ia:title"
        paragraph = text_box.text_frame.paragraphs[0]
        run = paragraph.add_run()
        run.text = item["text"]
        run.font.bold = True
        run.font.size = Pt(24)
        pptx_path = root / "page.pptx"
        presentation.save(pptx_path)
        pptx = image_identity(pptx_path)

        preview_path = root / "preview.png"
        Image.new("RGB", (1600, 900), "white").save(preview_path)
        preview = image_identity(preview_path)
        visual_report = VISUAL_DIFF.build_visual_diff(
            Path(candidate["clean_visual_reference"]["path"]),
            preview_path,
            root / "visual-diff",
            regions=candidate["regions"],
        )
        report_path = Path(visual_report["report"])
        report = image_identity(report_path)
        actual_validator = PPTX_VALIDATOR.validate_pptx(
            pptx_path,
            expected_slides=1,
            reconstruction_spec=candidate,
        )
        validator_payload = dict(validator_payload)
        actual_validator.update(validator_payload)
        validator_payload = actual_validator
        validator_payload.setdefault("pptx_sha256", pptx["sha256"])
        validator = self._artifact(
            root / "validator.json",
            json.dumps(validator_payload, ensure_ascii=False).encode("utf-8"),
        )
        coverage = {
            "canvas_and_regions": "checked",
            "objects_and_geometry": "checked",
            "text_and_typography": "not_applicable",
            "tables_and_matrices": "not_applicable",
            "graphics_connectors_charts": "not_applicable",
            "pictures_crop_layers": "not_applicable",
            "high_risk_regions": "not_applicable",
        }
        kinds = {
            element.get("kind")
            for element in candidate.get("elements", [])
            if isinstance(element, dict)
        }
        activated = set(candidate.get("activated_modules", []))
        if kinds & {"text", "special_text"} or activated & {"typography", "special_text"}:
            coverage["text_and_typography"] = "checked"
        if kinds & {"table", "matrix"}:
            coverage["tables_and_matrices"] = "checked"
        if kinds & {"shape", "line", "status", "diagram", "chart"} or activated & {"graphics", "diagram", "chart"}:
            coverage["graphics_connectors_charts"] = "checked"
        if kinds & {"icon", "picture"} or activated & {"icons", "picture_framing"}:
            coverage["pictures_crop_layers"] = "checked"
        high_risk = candidate.get("modules", {}).get("high_risk")
        if "high_risk" in activated and isinstance(high_risk, dict) and high_risk.get("items"):
            coverage["high_risk_regions"] = "checked"
        candidate["visual_gate"] = {
            "status": "passed",
            "review_round": 1,
            "evidence": [visual_report["evidence"]["overlay"]["path"]],
            "tripwire": {
                "available": False,
                "triggered": None,
                "reason": "no_approved_baseline",
            },
            "pptx": pptx,
            "preview": preview,
            "report": report,
            "reviewer": {
                "mode": "independent_read_only_subagent",
                "page_id": candidate["page_id"],
                "decision": "passed",
                "source_sha256": candidate["clean_visual_reference"]["sha256"],
                "preview_sha256": preview["sha256"],
                "coverage": coverage,
                "findings": [],
                "p2_disclosures": [],
            },
            "review": {
                "whole_page": "passed",
                "title": "passed",
                "body": "passed",
                "footer": "passed",
                "high_risk_regions": [],
            },
        }
        candidate["editability_gate"] = {
            "status": "passed",
            "evidence": [validator["path"]],
            "pptx": pptx,
            "validator": validator,
            "review": {
                "text_and_data": "passed",
                "native_text_structure": "passed",
                "basic_structure": "passed",
                "full_slide_picture_risk": "passed",
            },
        }

    def _add_valid_icon_contract(self, candidate: dict, root: Path) -> None:
        source_path = root / "source.png"
        source_image = Image.new("RGB", (1600, 900), (0, 0, 0))
        source_image.paste((241, 90, 34), (104, 104, 128, 128))
        source_image.save(source_path)
        source = {
            "path": str(source_path.resolve()),
            "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        }
        icons_dir = root / "assets" / "icons"
        icons_dir.mkdir(parents=True)
        asset_path = icons_dir / "status.png"
        icon = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        icon.paste((241, 90, 34, 255), (4, 4, 28, 28))
        icon.save(asset_path)
        asset = {
            "path": str(asset_path.resolve()),
            "sha256": hashlib.sha256(asset_path.read_bytes()).hexdigest(),
        }
        alpha_mask_sha256 = hashlib.sha256(icon.getchannel("A").tobytes()).hexdigest()
        candidate["content_reference"] = source
        candidate["clean_visual_reference"] = source
        candidate["activated_modules"].append("icons")
        candidate["elements"].append(
            {
                "element_id": "status-icon",
                "kind": "icon",
                "source_bbox": [100, 100, 32, 32],
                "slide_bbox": [762000, 762000, 243840, 243840],
                "layer": 3,
                "editable": False,
                "confidence": "high",
                "style": {},
                "content": {},
            }
        )
        candidate["regions"][0]["element_ids"].append("status-icon")
        candidate["reading_order"].append("status-icon")
        candidate["modules"]["icons"] = {
            "schema_version": 2,
            "page_id": "page-001",
            "slide_coordinate_unit": "EMU",
            "clean_visual_reference": source["path"],
            "clean_visual_sha256": source["sha256"],
            "icons": [
                {
                    "icon_id": "status-icon",
                    "element_id": "status-icon",
                    "category": "simple_symbol",
                    "instance_count": 1,
                    "repeat_group": None,
                    "semantic_scope": "icon_only",
                    "source_bbox": [100, 100, 32, 32],
                    "slide_bbox": [762000, 762000, 243840, 243840],
                    "layer": 3,
                    "source_path": source["path"],
                    "source_sha256": source["sha256"],
                    "crop_mode": "alpha_isolation",
                    "fallback_reason": None,
                    "padding": 0,
                    "background_handling": "border_connected_background_to_alpha",
                    "asset_path": asset["path"],
                    "asset_sha256": asset["sha256"],
                    "alpha_mask_sha256": alpha_mask_sha256,
                    "final_width": 32,
                    "final_height": 32,
                    "sharpness": "source_preserved",
                    "inspection": {
                        "roi_context_400": "passed",
                        "source_400": "passed",
                        "asset_400": "passed",
                        "placement_400": "pending",
                    },
                    "validation": "passed",
                    "native_redraw": False,
                    "selectable_picture_verified": False,
                    "object_type": "picture",
                }
            ],
        }

    def _replace_icon_asset(self, candidate: dict, image: Image.Image) -> None:
        icon = candidate["modules"]["icons"]["icons"][0]
        asset_path = Path(icon["asset_path"])
        image.save(asset_path)
        icon["asset_sha256"] = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        icon["alpha_mask_sha256"] = (
            hashlib.sha256(image.getchannel("A").tobytes()).hexdigest()
            if "A" in image.getbands()
            else None
        )

    def _set_background_preserved_icon(
        self,
        candidate: dict,
        image: Image.Image,
    ) -> None:
        icon = candidate["modules"]["icons"]["icons"][0]
        icon.update(
            {
                "crop_mode": "background_preserved",
                "fallback_reason": "透明化导致浅色轮廓或阴影出现可见损失",
                "background_handling": "preserved_source_patch",
                "semantic_scope": "intentional_composite",
                "alpha_mask_sha256": None,
                "sharpness": "source_pixels_preserved",
            }
        )
        self._replace_icon_asset(candidate, image)
        icon["alpha_mask_sha256"] = None
        source_path = Path(icon["source_path"])
        with Image.open(source_path) as opened:
            source = opened.convert("RGB")
        source.paste(image.convert("RGB"), (100, 100))
        source.save(source_path)
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        candidate["content_reference"]["sha256"] = source_sha256
        candidate["clean_visual_reference"]["sha256"] = source_sha256
        candidate["modules"]["icons"]["clean_visual_sha256"] = source_sha256
        icon["source_sha256"] = source_sha256

    def test_valid_prebuild_spec_passes(self):
        result = MODULE.validate_spec(valid_spec(), stage="prebuild")
        self.assertTrue(result["valid"], result)
        self.assertEqual([], result["errors"])

    def test_prebuild_rejects_missing_or_changed_reference(self):
        missing = valid_spec()
        missing["content_reference"] = {
            "path": "/tmp/ia-reference-does-not-exist.png",
            "sha256": "0" * 64,
        }
        changed = valid_spec()
        changed["clean_visual_reference"]["sha256"] = "f" * 64

        missing_result = MODULE.validate_spec(missing, stage="prebuild")
        changed_result = MODULE.validate_spec(changed, stage="prebuild")

        self.assertIn(
            "SPEC_REFERENCE_NOT_FOUND",
            {item["code"] for item in missing_result["errors"]},
        )
        self.assertIn(
            "SPEC_REFERENCE_HASH_MISMATCH",
            {item["code"] for item in changed_result["errors"]},
        )

    def test_activated_module_must_be_nonempty(self):
        candidate = valid_spec()
        candidate["activated_modules"].append("graphics")
        candidate["modules"]["graphics"] = {}

        result = MODULE.validate_spec(candidate, stage="prebuild")

        self.assertIn(
            "SPEC_ACTIVATED_MODULE_EMPTY",
            {item["code"] for item in result["errors"]},
        )

    def test_module_element_references_must_exist(self):
        candidate = valid_spec()
        candidate["activated_modules"].append("graphics")
        candidate["modules"]["graphics"] = {
            "items": [{"element_id": "missing-element"}]
        }

        result = MODULE.validate_spec(candidate, stage="prebuild")

        self.assertIn(
            "SPEC_MODULE_ELEMENT_REFERENCE_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_reading_order_must_cover_every_element(self):
        candidate = valid_spec()
        extra = dict(candidate["elements"][0])
        extra["element_id"] = "subtitle"
        candidate["elements"].append(extra)
        candidate["regions"][0]["element_ids"].append("subtitle")

        result = MODULE.validate_spec(candidate, stage="prebuild")

        self.assertIn(
            "SPEC_READING_ORDER_COVERAGE_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_regions_must_cover_every_element(self):
        candidate = valid_spec()
        extra = dict(candidate["elements"][0])
        extra["element_id"] = "subtitle"
        candidate["elements"].append(extra)
        candidate["reading_order"].append("subtitle")

        result = MODULE.validate_spec(candidate, stage="prebuild")

        self.assertIn(
            "SPEC_REGION_COVERAGE_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_icon_asset_path_does_not_derive_from_clean_reference(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._add_valid_icon_contract(candidate, root)
            original = Path(candidate["clean_visual_reference"]["path"])
            reference_dir = root / "references"
            reference_dir.mkdir()
            moved = reference_dir / "source.png"
            original.replace(moved)
            identity = image_identity(moved)
            candidate["content_reference"] = dict(identity)
            candidate["clean_visual_reference"] = dict(identity)
            icons = candidate["modules"]["icons"]
            icons["clean_visual_reference"] = identity["path"]
            icons["clean_visual_sha256"] = identity["sha256"]
            icon = icons["icons"][0]
            icon["source_path"] = identity["path"]
            icon["source_sha256"] = identity["sha256"]

            result = MODULE.validate_spec(candidate, stage="prebuild")

        self.assertTrue(result["valid"], result)

    def test_icon_asset_real_dimensions_must_match_declaration(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            icon = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
            icon.paste((241, 90, 34, 255), (4, 4, 12, 12))
            self._replace_icon_asset(candidate, icon)

            result = MODULE.validate_spec(candidate, stage="prebuild")

        self.assertIn(
            "SPEC_ICON_ASSET_DIMENSIONS_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_low_risk_typography_can_omit_manual_trials_and_metrics(self):
        candidate = valid_spec()

        result = MODULE.validate_spec(candidate, stage="prebuild")

        self.assertTrue(result["valid"], result)

    def test_present_font_trials_require_traceable_report(self):
        candidate = valid_spec()
        item = candidate["modules"]["typography"]["items"][0]
        item["candidate_trials"] = [
            {
                "font": "Noto Sans CJK SC",
                "font_size": 24,
                "width": 6096000,
                "height": 457200,
                "lines": 1,
                "score": 0,
            }
        ]
        item["render_metrics"] = {
            "width": 6096000,
            "height": 457200,
            "baseline": 342900,
            "lines": 1,
            "wrap_points": [],
            "width_delta": 0,
            "height_delta": 0,
        }

        result = MODULE.validate_spec(candidate, stage="prebuild")

        self.assertIn(
            "SPEC_FONT_TRIAL_EVIDENCE_REQUIRED",
            {entry["code"] for entry in result["errors"]},
        )

    def test_traceable_font_trial_report_is_accepted(self):
        candidate = valid_spec()
        item = candidate["modules"]["typography"]["items"][0]
        item["candidate_trials"] = [
            {
                "font": "Noto Sans CJK SC",
                "font_size": 24,
                "width": 6096000,
                "height": 457200,
                "lines": 1,
                "score": 0,
            }
        ]
        item["render_metrics"] = {
            "width": 6096000,
            "height": 457200,
            "baseline": 342900,
            "lines": 1,
            "wrap_points": [],
            "width_delta": 0,
            "height_delta": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            report_payload = {
                "trials": [
                    {
                        "requested_font": "Noto Sans CJK SC",
                        "resolved_fonts": ["ABCDEE+NotoSansCJKsc-Regular"],
                        "size_pt": 24,
                        "box_in": [6096000 / 914400, 457200 / 914400],
                        "line_count": 1,
                        "clipped": False,
                    }
                ]
            }
            report = self._artifact(
                Path(directory) / "font-trials.json",
                json.dumps(report_payload).encode("utf-8"),
            )
            item["font_trial_report"] = report

            result = MODULE.validate_spec(candidate, stage="prebuild")

        self.assertTrue(result["valid"], result)

    def test_typography_requires_emu(self):
        candidate = valid_spec()
        candidate["modules"]["typography"]["slide_coordinate_unit"] = "px"
        result = MODULE.validate_spec(candidate, stage="prebuild")
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("SPEC_TYPOGRAPHY_UNIT_INVALID", codes)

    def test_slide_bbox_rejects_pixel_scale_values(self):
        candidate = valid_spec()
        candidate["elements"][0]["slide_bbox"] = [24, 20, 1056, 64]
        result = MODULE.validate_spec(candidate, stage="prebuild")
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("SPEC_SLIDE_BBOX_UNIT_SUSPECT", codes)

    def test_slide_bbox_rejects_mixed_pixel_positions(self):
        candidate = valid_spec()
        candidate["elements"][0]["slide_bbox"][:2] = [30, 30]
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_SLIDE_BBOX_MAPPING_INVALID", {item["code"] for item in result["errors"]})

    def test_region_rejects_pixel_scale_and_out_of_bounds(self):
        candidate = valid_spec()
        candidate["regions"][0]["slide_bbox"] = [0, 0, 1600, 120]
        candidate["regions"][0]["source_bbox"] = [2000, 0, 100, 100]
        result = MODULE.validate_spec(candidate, stage="prebuild")
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("SPEC_REGION_BBOX_OUT_OF_BOUNDS", codes)
        self.assertIn("SPEC_SLIDE_BBOX_UNIT_SUSPECT", codes)

    def test_typography_text_box_must_match_element_emu_bbox(self):
        candidate = valid_spec()
        candidate["modules"]["typography"]["items"][0]["text_box"].update(
            {"x": 30, "y": 30, "w": 800, "h": 60}
        )
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_TEXT_BOX_MAPPING_INVALID", {item["code"] for item in result["errors"]})

    def test_unknown_activated_module_is_rejected(self):
        candidate = valid_spec()
        candidate["activated_modules"] = ["page_layout", "typogrphy"]
        candidate["modules"]["typogrphy"] = {}
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_ACTIVATED_MODULE_UNKNOWN", {item["code"] for item in result["errors"]})

    def test_activated_icons_require_complete_contract(self):
        candidate = valid_spec()
        candidate["activated_modules"].append("icons")
        candidate["modules"]["icons"] = {}
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_ICONS_FIELD_MISSING", {item["code"] for item in result["errors"]})

    def test_complete_icon_contract_passes_prebuild(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertTrue(result["valid"], result)

    def test_icon_contract_requires_roi_context_inspection(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            del candidate["modules"]["icons"]["icons"][0]["inspection"]["roi_context_400"]
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn(
            "SPEC_ICON_INSPECTION_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_alpha_isolation_requires_null_fallback_reason(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            candidate["modules"]["icons"]["icons"][0]["fallback_reason"] = "统一处理"
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn(
            "SPEC_ICON_FALLBACK_REASON_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_background_preserved_requires_nonempty_fallback_reason(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            self._set_background_preserved_icon(
                candidate,
                Image.new("RGB", (32, 32), (242, 241, 235)),
            )
            candidate["modules"]["icons"]["icons"][0]["fallback_reason"] = "   "
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn(
            "SPEC_ICON_FALLBACK_REASON_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_final_icon_contract_requires_placement_and_selectability_verification(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            result = MODULE.validate_spec(candidate, stage="final")
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("SPEC_ICON_PLACEMENT_NOT_VERIFIED", codes)
        self.assertIn("SPEC_ICON_SELECTABILITY_NOT_VERIFIED", codes)

    def test_unknown_icon_crop_mode_is_rejected(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            candidate["modules"]["icons"]["icons"][0]["crop_mode"] = "tight_rect"
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_ICON_CROP_MODE_INVALID", {item["code"] for item in result["errors"]})

    def test_background_preserved_accepts_rgb_png_without_alpha_hash(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            self._set_background_preserved_icon(
                candidate,
                Image.new("RGB", (32, 32), (242, 241, 235)),
            )
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertTrue(result["valid"], result)

    def test_background_preserved_accepts_fully_opaque_rgba_png(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            self._set_background_preserved_icon(
                candidate,
                Image.new("RGBA", (32, 32), (242, 241, 235, 255)),
            )
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertTrue(result["valid"], result)

    def test_background_preserved_rejects_partial_transparency(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            image = Image.new("RGBA", (32, 32), (242, 241, 235, 255))
            image.putpixel((0, 0), (242, 241, 235, 128))
            self._set_background_preserved_icon(candidate, image)
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn(
            "SPEC_ICON_BACKGROUND_PRESERVED_ALPHA_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_background_preserved_requires_intentional_composite(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            self._set_background_preserved_icon(
                candidate,
                Image.new("RGB", (32, 32), (242, 241, 235)),
            )
            candidate["modules"]["icons"]["icons"][0]["semantic_scope"] = "icon_only"
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn(
            "SPEC_ICON_BACKGROUND_PRESERVED_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_background_preserved_requires_preserved_source_patch_handling(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            self._set_background_preserved_icon(
                candidate,
                Image.new("RGB", (32, 32), (242, 241, 235)),
            )
            candidate["modules"]["icons"]["icons"][0]["background_handling"] = "none"
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn(
            "SPEC_ICON_BACKGROUND_PRESERVED_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_background_preserved_rejects_alpha_mask_hash(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            self._set_background_preserved_icon(
                candidate,
                Image.new("RGB", (32, 32), (242, 241, 235)),
            )
            candidate["modules"]["icons"]["icons"][0]["alpha_mask_sha256"] = "f" * 64
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn(
            "SPEC_ICON_BACKGROUND_PRESERVED_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_alpha_isolation_rejects_mismatched_alpha_hash(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            candidate["modules"]["icons"]["icons"][0]["alpha_mask_sha256"] = "f" * 64
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_ICON_ALPHA_MASK_MISMATCH", {item["code"] for item in result["errors"]})

    def test_alpha_isolation_rejects_changed_rgb_even_under_transparent_pixel(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            icon = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            icon.paste((241, 90, 34, 255), (4, 4, 28, 28))
            icon.putpixel((0, 0), (1, 2, 3, 0))
            self._replace_icon_asset(candidate, icon)
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_ICON_RGB_MISMATCH", {item["code"] for item in result["errors"]})

    def test_background_preserved_rejects_changed_source_crop_rgb(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            preserved = Image.new("RGB", (32, 32), (242, 241, 235))
            self._set_background_preserved_icon(candidate, preserved)
            changed = preserved.copy()
            changed.putpixel((7, 9), (10, 20, 30))
            self._replace_icon_asset(candidate, changed)
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_ICON_RGB_MISMATCH", {item["code"] for item in result["errors"]})

    def test_alpha_isolation_rejects_opaque_asset(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            self._replace_icon_asset(
                candidate,
                Image.new("RGBA", (32, 32), (241, 90, 34, 255)),
            )
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_ICON_ALPHA_CONTENT_INVALID", {item["code"] for item in result["errors"]})

    def test_alpha_isolation_rejects_non_rgba_png(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            icon = Image.new("LA", (32, 32), (0, 0))
            icon.paste((128, 255), (4, 4, 28, 28))
            self._replace_icon_asset(candidate, icon)
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_ICON_ALPHA_CONTENT_INVALID", {item["code"] for item in result["errors"]})

    def test_alpha_isolation_rejects_foreground_touching_edge(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._add_valid_icon_contract(candidate, Path(directory))
            icon = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            icon.paste((241, 90, 34, 255), (0, 4, 28, 28))
            self._replace_icon_asset(candidate, icon)
            result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_ICON_FOREGROUND_TOUCHES_EDGE", {item["code"] for item in result["errors"]})

    def test_same_session_reuse_requires_verified_artifacts(self):
        candidate = valid_spec()
        candidate["session_reuse"] = {"mode": "same_session_reuse", "reason": "continue", "artifacts": []}
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_SESSION_ARTIFACTS_INVALID", {item["code"] for item in result["errors"]})

    def test_element_requires_style_and_content(self):
        candidate = valid_spec()
        del candidate["elements"][0]["style"]
        result = MODULE.validate_spec(candidate, stage="prebuild")
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("SPEC_ELEMENT_FIELD_MISSING", codes)

    def test_text_runs_must_cover_full_text(self):
        candidate = valid_spec()
        candidate["modules"]["typography"]["items"][0]["runs"][0]["end"] = 1
        result = MODULE.validate_spec(candidate, stage="prebuild")
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("SPEC_TEXT_RUN_COVERAGE_INVALID", codes)

    def test_text_run_requires_font_weight(self):
        candidate = valid_spec()
        del candidate["modules"]["typography"]["items"][0]["runs"][0]["font_weight"]
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_TEXT_RUN_STYLE_INVALID", {item["code"] for item in result["errors"]})

    def test_text_run_rejects_boolean_font_weight(self):
        candidate = valid_spec()
        candidate["modules"]["typography"]["items"][0]["runs"][0]["font_weight"] = True
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_TEXT_RUN_STYLE_INVALID", {item["code"] for item in result["errors"]})

    def test_text_run_rejects_out_of_range_font_weight(self):
        candidate = valid_spec()
        candidate["modules"]["typography"]["items"][0]["runs"][0]["font_weight"] = 1200
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_TEXT_RUN_STYLE_INVALID", {item["code"] for item in result["errors"]})

    def test_paragraph_requires_list_contract(self):
        candidate = valid_spec()
        del candidate["modules"]["typography"]["items"][0]["paragraphs"][0]["list"]
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_PARAGRAPH_LIST_INVALID", {item["code"] for item in result["errors"]})

    def test_native_list_requires_complete_bullet_contract(self):
        candidate = valid_list_spec()
        del candidate["modules"]["typography"]["items"][0]["paragraphs"][0]["list"]["bullet_type"]
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_NATIVE_LIST_CONTRACT_INVALID", {item["code"] for item in result["errors"]})

    def test_native_list_requires_margin_and_hanging_indent(self):
        candidate = valid_list_spec()
        del candidate["modules"]["typography"]["items"][0]["paragraphs"][0]["margin_left"]
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_NATIVE_LIST_INDENT_INVALID", {item["code"] for item in result["errors"]})

    def test_text_box_paragraph_breaks_match_paragraph_boundaries(self):
        candidate = valid_list_spec()
        candidate["modules"]["typography"]["items"][0]["text_box"]["paragraph_breaks"] = []
        result = MODULE.validate_spec(candidate, stage="prebuild")
        self.assertIn("SPEC_PARAGRAPH_BREAKS_INVALID", {item["code"] for item in result["errors"]})

    def test_final_stage_requires_both_gates_passed(self):
        result = MODULE.validate_spec(valid_spec(), stage="final")
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("SPEC_VISUAL_GATE_NOT_PASSED", codes)
        self.assertIn("SPEC_EDITABILITY_GATE_NOT_PASSED", codes)

    def test_final_stage_requires_current_artifact_identities(self):
        candidate = valid_spec()
        candidate["visual_gate"] = {"status": "passed", "evidence": ["overlay.png"]}
        candidate["editability_gate"] = {"status": "passed", "evidence": ["validator.json"]}
        candidate["modules"]["typography"]["items"][0]["font_declaration_verified"] = True
        result = MODULE.validate_spec(candidate, stage="final")
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("SPEC_GATE_ARTIFACT_MISSING", codes)

    def test_final_stage_requires_complete_gate_reviews(self):
        candidate = valid_spec()
        candidate["visual_gate"] = {"status": "passed", "evidence": "overlay.png"}
        candidate["editability_gate"] = {"status": "passed", "evidence": ["validator.json"]}
        candidate["modules"]["typography"]["items"][0]["font_declaration_verified"] = True
        result = MODULE.validate_spec(candidate, stage="final")
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("SPEC_GATE_EVIDENCE_INVALID", codes)
        self.assertIn("SPEC_VISUAL_REVIEW_INVALID", codes)
        self.assertIn("SPEC_EDITABILITY_REVIEW_INVALID", codes)

    def test_final_stage_accepts_matching_current_artifacts(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._attach_final_gates(
                candidate,
                root,
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            result = MODULE.validate_spec(candidate, stage="final")
        self.assertTrue(result["valid"], result)

    def test_final_rejects_non_pptx_payload(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            pptx_path = Path(candidate["visual_gate"]["pptx"]["path"])
            pptx_path.write_bytes(b"not-a-pptx")
            pptx_identity = image_identity(pptx_path)
            candidate["visual_gate"]["pptx"] = dict(pptx_identity)
            candidate["editability_gate"]["pptx"] = dict(pptx_identity)
            validator_path = Path(candidate["editability_gate"]["validator"]["path"])
            validator = json.loads(validator_path.read_text(encoding="utf-8"))
            validator["pptx_sha256"] = pptx_identity["sha256"]
            validator_path.write_text(json.dumps(validator), encoding="utf-8")
            candidate["editability_gate"]["validator"] = image_identity(validator_path)

            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_CURRENT_PPTX_VALIDATION_FAILED",
            {item["code"] for item in result["errors"]},
        )

    def test_final_rejects_non_image_preview(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            preview_path = Path(candidate["visual_gate"]["preview"]["path"])
            preview_path.write_bytes(b"not-an-image")
            preview = image_identity(preview_path)
            candidate["visual_gate"]["preview"] = preview
            candidate["visual_gate"]["reviewer"]["preview_sha256"] = preview["sha256"]

            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_PREVIEW_IMAGE_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_final_rejects_invalid_visual_diff_json(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            report_path = Path(candidate["visual_gate"]["report"]["path"])
            report_path.write_bytes(b"not-json")
            candidate["visual_gate"]["report"] = image_identity(report_path)

            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_VISUAL_DIFF_REPORT_INVALID",
            {item["code"] for item in result["errors"]},
        )

    def test_final_rejects_skipped_visual_evidence(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            report_path = Path(candidate["visual_gate"]["report"]["path"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["region_summary"]["skipped"] = 1
            report_path.write_text(json.dumps(report), encoding="utf-8")
            candidate["visual_gate"]["report"] = image_identity(report_path)

            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_VISUAL_DIFF_EVIDENCE_INCOMPLETE",
            {item["code"] for item in result["errors"]},
        )

    def test_final_rejects_visual_diff_source_hash_mismatch(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            report_path = Path(candidate["visual_gate"]["report"]["path"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["reference"]["sha256"] = "f" * 64
            report_path.write_text(json.dumps(report), encoding="utf-8")
            candidate["visual_gate"]["report"] = image_identity(report_path)

            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_VISUAL_DIFF_SOURCE_MISMATCH",
            {item["code"] for item in result["errors"]},
        )

    def test_final_rejects_reviewer_page_mismatch(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            candidate["visual_gate"]["reviewer"]["page_id"] = "page-wrong"

            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_VISUAL_REVIEW_PAGE_MISMATCH",
            {item["code"] for item in result["errors"]},
        )

    def test_final_rejects_missing_independent_visual_reviewer(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            del candidate["visual_gate"]["reviewer"]

            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_INDEPENDENT_VISUAL_REVIEW_REQUIRED",
            {entry["code"] for entry in result["errors"]},
        )

    def test_final_requires_visual_review_round(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            del candidate["visual_gate"]["review_round"]
            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_VISUAL_REVIEW_ROUND_INVALID",
            {entry["code"] for entry in result["errors"]},
        )

    def test_final_rejects_visual_review_round_outside_one_to_two(self):
        for review_round in (0, 3, 4, True, 1.5):
            with self.subTest(review_round=review_round):
                candidate = valid_spec()
                with tempfile.TemporaryDirectory() as directory:
                    self._attach_final_gates(
                        candidate,
                        Path(directory),
                        {"valid": True, "errors": [], "native_list_contracts_checked": 0},
                    )
                    candidate["visual_gate"]["review_round"] = review_round
                    result = MODULE.validate_spec(candidate, stage="final")

                self.assertIn(
                    "SPEC_VISUAL_REVIEW_ROUND_INVALID",
                    {entry["code"] for entry in result["errors"]},
                )

    def test_final_accepts_visual_review_rounds_one_to_two(self):
        for review_round in (1, 2):
            with self.subTest(review_round=review_round):
                candidate = valid_spec()
                with tempfile.TemporaryDirectory() as directory:
                    self._attach_final_gates(
                        candidate,
                        Path(directory),
                        {"valid": True, "errors": [], "native_list_contracts_checked": 0},
                    )
                    candidate["visual_gate"]["review_round"] = review_round
                    result = MODULE.validate_spec(candidate, stage="final")

                self.assertTrue(result["valid"], result)

    def test_final_requires_complete_visual_review_coverage(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            del candidate["visual_gate"]["reviewer"]["coverage"]
            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_VISUAL_REVIEW_COVERAGE_INVALID",
            {entry["code"] for entry in result["errors"]},
        )

    def test_final_rejects_unknown_or_invalid_visual_coverage(self):
        mutations = (
            lambda coverage: coverage.update({"unknown": "checked"}),
            lambda coverage: coverage.update({"canvas_and_regions": "skipped"}),
            lambda coverage: coverage.update({"canvas_and_regions": "not_reviewable"}),
        )
        for mutate in mutations:
            candidate = valid_spec()
            with tempfile.TemporaryDirectory() as directory:
                self._attach_final_gates(
                    candidate,
                    Path(directory),
                    {"valid": True, "errors": [], "native_list_contracts_checked": 0},
                )
                mutate(candidate["visual_gate"]["reviewer"]["coverage"])
                result = MODULE.validate_spec(candidate, stage="final")

            self.assertIn(
                "SPEC_VISUAL_REVIEW_COVERAGE_INVALID",
                {entry["code"] for entry in result["errors"]},
            )

    def test_final_rejects_unhashable_visual_coverage_values_without_crashing(self):
        for invalid in ([], {}, 1, None):
            with self.subTest(invalid=invalid):
                candidate = valid_spec()
                with tempfile.TemporaryDirectory() as directory:
                    self._attach_final_gates(
                        candidate,
                        Path(directory),
                        {"valid": True, "errors": [], "native_list_contracts_checked": 0},
                    )
                    candidate["visual_gate"]["reviewer"]["coverage"][
                        "canvas_and_regions"
                    ] = invalid
                    result = MODULE.validate_spec(candidate, stage="final")

                self.assertIn(
                    "SPEC_VISUAL_REVIEW_COVERAGE_INVALID",
                    {entry["code"] for entry in result["errors"]},
                )

    def test_final_requires_applicable_visual_coverage_to_be_checked(self):
        for category in (
            "canvas_and_regions",
            "objects_and_geometry",
            "text_and_typography",
        ):
            with self.subTest(category=category):
                candidate = valid_spec()
                with tempfile.TemporaryDirectory() as directory:
                    self._attach_final_gates(
                        candidate,
                        Path(directory),
                        {"valid": True, "errors": [], "native_list_contracts_checked": 0},
                    )
                    candidate["visual_gate"]["reviewer"]["coverage"][category] = (
                        "not_applicable"
                    )
                    result = MODULE.validate_spec(candidate, stage="final")

                self.assertIn(
                    "SPEC_VISUAL_REVIEW_COVERAGE_INVALID",
                    {entry["code"] for entry in result["errors"]},
                )

    def test_final_requires_complete_finding_fields(self):
        finding = {
            "severity": "P2",
            "category": "pictures_crop_layers",
            "location": "右上角图片",
            "source_fact": "原图边缘为圆角",
            "observed_difference": "预览圆角半径略小",
            "evidence": "region-picture.png",
        }
        for field in tuple(finding):
            with self.subTest(field=field):
                candidate = valid_spec()
                with tempfile.TemporaryDirectory() as directory:
                    self._attach_final_gates(
                        candidate,
                        Path(directory),
                        {"valid": True, "errors": [], "native_list_contracts_checked": 0},
                    )
                    incomplete = dict(finding)
                    del incomplete[field]
                    candidate["visual_gate"]["reviewer"]["findings"] = [incomplete]
                    result = MODULE.validate_spec(candidate, stage="final")

                self.assertIn(
                    "SPEC_INDEPENDENT_VISUAL_REVIEW_INVALID",
                    {entry["code"] for entry in result["errors"]},
                )

    def test_final_rejects_non_string_finding_enums_without_crashing(self):
        base = {
            "severity": "P2",
            "category": "pictures_crop_layers",
            "location": "右上角图片",
            "source_fact": "原图边缘为圆角",
            "observed_difference": "预览圆角半径略小",
            "evidence": "region-picture.png",
        }
        for field in ("severity", "category"):
            for invalid in ([], {}, 1, None):
                with self.subTest(field=field, invalid=invalid):
                    candidate = valid_spec()
                    with tempfile.TemporaryDirectory() as directory:
                        self._attach_final_gates(
                            candidate,
                            Path(directory),
                            {"valid": True, "errors": [], "native_list_contracts_checked": 0},
                        )
                        finding = dict(base)
                        finding[field] = invalid
                        candidate["visual_gate"]["reviewer"]["findings"] = [finding]
                        result = MODULE.validate_spec(candidate, stage="final")

                    self.assertIn(
                        "SPEC_INDEPENDENT_VISUAL_REVIEW_INVALID",
                        {entry["code"] for entry in result["errors"]},
                    )

    def test_final_rejects_decision_and_findings_inconsistency(self):
        blocking = {
            "severity": "P1",
            "category": "text_and_typography",
            "location": "标题",
            "source_fact": "原图标题完整",
            "observed_difference": "预览标题被截断",
            "evidence": "region-title.png",
        }
        scenarios = (
            ("passed", [blocking]),
            ("not_reviewable", [blocking]),
            ("changes_required", []),
        )
        for decision, findings in scenarios:
            with self.subTest(decision=decision):
                candidate = valid_spec()
                with tempfile.TemporaryDirectory() as directory:
                    self._attach_final_gates(
                        candidate,
                        Path(directory),
                        {"valid": True, "errors": [], "native_list_contracts_checked": 0},
                    )
                    reviewer = candidate["visual_gate"]["reviewer"]
                    reviewer["decision"] = decision
                    reviewer["findings"] = findings
                    result = MODULE.validate_spec(candidate, stage="final")

                self.assertIn(
                    "SPEC_INDEPENDENT_VISUAL_REVIEW_INVALID",
                    {entry["code"] for entry in result["errors"]},
                )

    def test_final_rejects_non_string_reviewer_decision_without_crashing(self):
        for invalid in ([], {}, 1, None):
            with self.subTest(invalid=invalid):
                candidate = valid_spec()
                with tempfile.TemporaryDirectory() as directory:
                    self._attach_final_gates(
                        candidate,
                        Path(directory),
                        {"valid": True, "errors": [], "native_list_contracts_checked": 0},
                    )
                    candidate["visual_gate"]["reviewer"]["decision"] = invalid
                    result = MODULE.validate_spec(candidate, stage="final")

                self.assertIn(
                    "SPEC_INDEPENDENT_VISUAL_REVIEW_INVALID",
                    {entry["code"] for entry in result["errors"]},
                )

    def test_final_rejects_validator_report_for_different_pptx(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {
                    "valid": True,
                    "errors": [],
                    "native_list_contracts_checked": 0,
                    "pptx_sha256": "f" * 64,
                },
            )
            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_VALIDATOR_PPTX_MISMATCH",
            {entry["code"] for entry in result["errors"]},
        )

    def test_final_rejects_open_visual_p1(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            candidate["visual_gate"]["reviewer"]["findings"] = [
                {
                    "severity": "P1",
                    "category": "text_and_typography",
                    "location": "右侧文本框",
                    "source_fact": "原图文本完整显示",
                    "observed_difference": "预览末行被截断",
                    "evidence": "region-right.png",
                }
            ]

            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_OPEN_BLOCKING_DIFFERENCE",
            {entry["code"] for entry in result["errors"]},
        )

    def test_final_rejects_reviewed_preview_hash_mismatch(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            candidate["visual_gate"]["reviewer"]["preview_sha256"] = "f" * 64

            result = MODULE.validate_spec(candidate, stage="final")

        self.assertIn(
            "SPEC_VISUAL_REVIEW_PREVIEW_MISMATCH",
            {entry["code"] for entry in result["errors"]},
        )

    def test_final_stage_rejects_failed_validator_report(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": False, "errors": ["NATIVE_LIST_TEXTBOX_MISSING"], "native_list_contracts_checked": 0},
            )
            result = MODULE.validate_spec(candidate, stage="final")
        self.assertIn("SPEC_VALIDATOR_REPORT_FAILED", {item["code"] for item in result["errors"]})

    def test_final_stage_requires_native_list_contract_evidence(self):
        candidate = valid_list_spec()
        with tempfile.TemporaryDirectory() as directory:
            self._attach_final_gates(
                candidate,
                Path(directory),
                {"valid": True, "errors": [], "native_list_contracts_checked": 0},
            )
            result = MODULE.validate_spec(candidate, stage="final")
        self.assertIn("SPEC_NATIVE_LIST_VALIDATION_MISSING", {item["code"] for item in result["errors"]})

    def test_final_stage_rejects_open_p1(self):
        candidate = valid_spec()
        candidate["visual_gate"] = {"status": "passed", "evidence": ["overlay.png"]}
        candidate["editability_gate"] = {"status": "passed", "evidence": ["validator.json"]}
        candidate["activated_modules"].append("high_risk")
        candidate["modules"]["high_risk"] = {
            "items": [
                {
                    "risk_id": "layout-diff",
                    "severity": "P1",
                    "result": "changes_required",
                }
            ]
        }
        result = MODULE.validate_spec(candidate, stage="final")
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("SPEC_OPEN_BLOCKING_DIFFERENCE", codes)
        self.assertIn("SPEC_HIGH_RISK_ITEM_INVALID", codes)

    def test_cli_writes_json_and_returns_failure(self):
        candidate = valid_spec()
        candidate["modules"]["typography"]["slide_coordinate_unit"] = "px"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page-reconstruction.json"
            path.write_text(json.dumps(candidate), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = MODULE.main([str(path), "--stage", "prebuild"])
        self.assertEqual(1, exit_code)

    def test_cli_output_matches_stdout_json(self):
        candidate = valid_spec()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "page-reconstruction.json"
            report_path = root / "reports" / "prebuild.json"
            spec_path.write_text(json.dumps(candidate), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = MODULE.main(
                    [
                        str(spec_path),
                        "--stage",
                        "prebuild",
                        "--output",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertTrue(report_path.is_file())
            self.assertEqual(
                json.loads(stdout.getvalue()),
                json.loads(report_path.read_text(encoding="utf-8")),
            )


if __name__ == "__main__":
    unittest.main()
