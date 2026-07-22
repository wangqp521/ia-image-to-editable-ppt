from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from PIL import Image


def file_identity(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def make_text_spec(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source.png"
    if not source.exists():
        Image.new("RGB", (1600, 900), "white").save(source)
    reference = file_identity(source)
    text = "标题"
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
                                "list": {
                                    "is_list": False,
                                    "level": 0,
                                    "bullet": None,
                                },
                            }
                        ],
                        "text_box": {
                            "x": 228600,
                            "y": 228600,
                            "w": 6096000,
                            "h": 457200,
                            "margins": {
                                "left": 0,
                                "right": 0,
                                "top": 0,
                                "bottom": 0,
                            },
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


def refresh_reference_identity(spec: dict[str, Any]) -> None:
    source = Path(spec["clean_visual_reference"]["path"])
    identity = file_identity(source)
    spec["clean_visual_reference"] = dict(identity)
    spec["content_reference"] = dict(identity)


def add_picture_asset(
    spec: dict[str, Any],
    *,
    element_id: str = "photo",
    source_bbox: list[int] | None = None,
    processor: str = "source_patch",
    mask_path: str | None = None,
) -> dict[str, Any]:
    bbox = [100, 100, 120, 80] if source_bbox is None else list(source_bbox)
    slide_bbox = [
        round(bbox[0] * 12192000 / 1600),
        round(bbox[1] * 6858000 / 900),
        round(bbox[2] * 12192000 / 1600),
        round(bbox[3] * 6858000 / 900),
    ]
    spec["elements"].append(
        {
            "element_id": element_id,
            "kind": "picture",
            "source_bbox": bbox,
            "slide_bbox": slide_bbox,
            "layer": 3,
            "editable": False,
            "confidence": "high",
            "style": {},
            "content": {
                "asset": {
                    "processor": processor,
                    "padding": 0,
                    "tolerance": None,
                    "mask_path": mask_path,
                    "asset_path": None,
                    "asset_sha256": None,
                    "alpha_mask_sha256": None,
                    "final_width": None,
                    "final_height": None,
                },
                "placement": {"mode": "contain", "crop": None, "opacity": 1.0},
            },
        }
    )
    spec["reading_order"].append(element_id)
    spec["regions"][0]["element_ids"].append(element_id)
    return spec


def add_shape_and_line(spec: dict[str, Any]) -> dict[str, Any]:
    additions = [
        {
            "element_id": "card",
            "kind": "shape",
            "source_bbox": [100, 200, 400, 180],
            "slide_bbox": [762000, 1524000, 3048000, 1371600],
            "layer": 1,
            "editable": True,
            "confidence": "high",
            "style": {
                "shape_type": "roundRect",
                "fill": {"color": "#E8F0FF", "transparency": 0},
                "line": {"color": "#3355AA", "width_emu": 12700, "dash": "solid"},
                "adjustments": [0.12],
            },
            "content": {},
        },
        {
            "element_id": "divider",
            "kind": "line",
            "source_bbox": [100, 400, 400, 2],
            "slide_bbox": [762000, 3048000, 3048000, 15240],
            "layer": 2,
            "editable": True,
            "confidence": "high",
            "style": {
                "line": {"color": "#666666", "width_emu": 12700, "dash": "dash"}
            },
            "content": {},
        },
    ]
    spec["elements"].extend(additions)
    spec["reading_order"].extend(["card", "divider"])
    spec["regions"][0]["element_ids"].extend(["card", "divider"])
    return spec


def add_merged_table(spec: dict[str, Any]) -> dict[str, Any]:
    element = {
        "element_id": "table",
        "kind": "table",
        "source_bbox": [600, 200, 480, 180],
        "slide_bbox": [4572000, 1524000, 3657600, 1371600],
        "layer": 2,
        "editable": True,
        "confidence": "high",
        "style": {
            "fill": {"color": "#FFFFFF", "transparency": 0},
            "line": {"color": "#777777", "width_emu": 12700, "dash": "solid"},
        },
        "content": {
            "rows": 2,
            "columns": 2,
            "row_heights": [685800, 685800],
            "column_widths": [1219200, 2438400],
            "cells": [
                {"row": 0, "column": 0, "text": "A", "row_span": 1, "column_span": 2},
                {"row": 1, "column": 0, "text": "B", "row_span": 1, "column_span": 1},
                {"row": 1, "column": 1, "text": "C", "row_span": 1, "column_span": 1},
            ],
        },
    }
    spec["elements"].append(element)
    spec["reading_order"].append("table")
    spec["regions"][0]["element_ids"].append("table")
    return spec


def add_matrix_and_status(spec: dict[str, Any]) -> dict[str, Any]:
    matrix = {
        "element_id": "matrix",
        "kind": "matrix",
        "source_bbox": [200, 500, 400, 120],
        "slide_bbox": [1524000, 3810000, 3048000, 914400],
        "layer": 2,
        "editable": True,
        "confidence": "high",
        "style": {},
        "content": {
            "cells": [
                {
                    "part": "cell-0-0",
                    "slide_bbox": [1524000, 3810000, 1524000, 914400],
                    "text": "左",
                    "text_style": {"font_name": "Noto Sans CJK SC", "font_size": 14, "color": "#000000"},
                    "fill": "#DDEAFF",
                    "line": "#3355AA",
                },
                {
                    "part": "cell-0-1",
                    "slide_bbox": [3048000, 3810000, 1524000, 914400],
                    "text": "右",
                    "text_style": {"font_name": "Noto Sans CJK SC", "font_size": 14, "color": "#000000"},
                    "fill": "#EEF4FF",
                    "line": "#3355AA",
                },
            ]
        },
    }
    status = {
        "element_id": "status",
        "kind": "status",
        "source_bbox": [700, 520, 300, 30],
        "slide_bbox": [5334000, 3962400, 2286000, 228600],
        "layer": 3,
        "editable": True,
        "confidence": "high",
        "style": {},
        "content": {
            "segments": [
                {
                    "part": "segment-0",
                    "slide_bbox": [5334000, 3962400, 1371600, 228600],
                    "fill": "#22AA66",
                    "line": None,
                },
                {
                    "part": "segment-1",
                    "slide_bbox": [6705600, 3962400, 914400, 228600],
                    "fill": "#DDE3E8",
                    "line": None,
                },
            ]
        },
    }
    spec["elements"].extend([matrix, status])
    spec["reading_order"].extend(["matrix", "status"])
    spec["regions"][0]["element_ids"].extend(["matrix", "status"])
    return spec
