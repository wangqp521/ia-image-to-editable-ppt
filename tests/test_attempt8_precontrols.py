"""Attempt 7 fixed negatives that must block before an Attempt 8 review."""

from __future__ import annotations

import html
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from create_rendered_text_geometry import create_rendered_text_geometry
from lib.background_contracts import validate_background_prebuild
from lib.hashing import canonical_json_sha256, file_sha256
from lib import review_contracts
from lib.spec_identity import content_spec_sha256, input_spec_sha256
from tests.fixture_specs import make_minimal_spec


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_executable(path: Path, source: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _point_bbox_to_emu(values: list[float]) -> list[int]:
    return [round(value * 12_700) for value in values]


def geometry_fixture(
    root: Path,
    *,
    text: str,
    expected: list[float],
    actual: list[float],
) -> dict[str, Any]:
    """Run the production geometry controller against inline fake Poppler XHTML."""
    root.mkdir(parents=True, exist_ok=True)
    spec = make_minimal_spec(root / "source")
    element = next(
        item for item in spec["elements"] if item["element_id"] == "element-001"
    )
    typography = spec["modules"]["typography"]["items"][0]
    expected_emu = _point_bbox_to_emu(expected)
    element["slide_bbox"] = list(expected_emu)
    element["content"]["text"] = text
    typography["text"] = text
    typography["runs"][0]["end"] = len(text)
    typography["paragraphs"][0]["end"] = len(text)
    for field, value in zip(("x", "y", "w", "h"), expected_emu):
        typography["text_box"][field] = value

    spec_path = root / "page-reconstruction.json"
    _write_json(spec_path, spec)
    pptx_path = root / "page.pptx"
    pptx_path.write_bytes(b"attempt-7-fixed-negative-pptx")
    pdf_path = root / "page.pdf"
    pdf_path.write_bytes(b"attempt-7-fixed-negative-pdf")

    x, y, width, height = actual
    xhtml = (
        '<doc><page width="960" height="540"><flow><block><line>'
        f'<word xMin="{x}" yMin="{y}" xMax="{x + width}" '
        f'yMax="{y + height}">{html.escape(text)}</word>'
        "</line></block></flow></page></doc>"
    )
    pdftotext_path = _write_executable(
        root / "pdftotext",
        "import sys\n"
        f"expected = ['-bbox-layout', '-enc', 'UTF-8', {str(pdf_path.resolve())!r}, '-']\n"
        "if sys.argv[1:] != expected:\n"
        "    raise SystemExit(91)\n"
        f"sys.stdout.write({xhtml!r})\n",
    )
    soffice_path = _write_executable(root / "soffice", "raise SystemExit(0)\n")
    pdftoppm_path = _write_executable(root / "pdftoppm", "raise SystemExit(0)\n")
    pdffonts_path = _write_executable(root / "pdffonts", "raise SystemExit(0)\n")
    fontconfig_path = root / "fontconfig.xml"
    fontconfig_path.write_text("<fontconfig/>\n", encoding="utf-8")

    build_report = {
        "valid": True,
        "schema_version": 1,
        "schema_sha256": canonical_json_sha256(spec),
        "content_spec_sha256": content_spec_sha256(spec),
        "input_spec_sha256": input_spec_sha256(spec),
        "pptx_sha256": file_sha256(pptx_path),
        "elements": {
            "element-001": {
                "semantic_kind": "text",
                "selected_mode": "native",
                "object_type": "sp",
                "objects": [
                    {
                        "object_type": "sp",
                        "ooxml_name": "ia:element-001",
                        "text_summary": text,
                        "bbox": list(expected_emu),
                    }
                ],
            }
        },
        "warnings": [],
        "unsupported": [],
    }
    build_report_path = root / "build-report.json"
    _write_json(build_report_path, build_report)

    runtime = {
        "valid": True,
        "errors": [],
        "renderer_backend": "libreoffice",
        "executables": {
            "soffice": {
                "path": str(soffice_path.resolve()),
                "version": "LibreOffice 26.2.3.2",
                "sha256": file_sha256(soffice_path),
            },
            "pdftoppm": {
                "path": str(pdftoppm_path.resolve()),
                "version": "pdftoppm version 26.07.0",
                "sha256": file_sha256(pdftoppm_path),
            },
            "pdffonts": {
                "path": str(pdffonts_path.resolve()),
                "version": "pdffonts version 26.07.0",
                "sha256": file_sha256(pdffonts_path),
            },
            "pdftotext": {
                "path": str(pdftotext_path.resolve()),
                "version": "pdftotext version 26.07.0",
                "sha256": file_sha256(pdftotext_path),
            },
        },
        "fontconfig": {
            "path": str(fontconfig_path.resolve()),
            "sha256": file_sha256(fontconfig_path),
        },
    }
    runtime_path = root / "preflight-runtime.json"
    _write_json(runtime_path, runtime)

    render_report = {
        "schema_version": 1,
        "pptx": {
            "path": str(pptx_path.resolve()),
            "sha256": file_sha256(pptx_path),
        },
        "pdf": {
            "path": str(pdf_path.resolve()),
            "sha256": file_sha256(pdf_path),
            "pages": 1,
            "page_size_pt": [960.0, 540.0],
        },
        "renderer": {
            "backend": "libreoffice",
            "path": str(soffice_path.resolve()),
            "version": runtime["executables"]["soffice"]["version"],
            "executable_sha256": file_sha256(soffice_path),
            "fontconfig_path": str(fontconfig_path.resolve()),
            "fontconfig_sha256": file_sha256(fontconfig_path),
        },
        "rasterizer": {
            "path": str(pdftoppm_path.resolve()),
            "version": runtime["executables"]["pdftoppm"]["version"],
            "executable_sha256": file_sha256(pdftoppm_path),
        },
        "text_extractor": {
            "path": str(pdftotext_path.resolve()),
            "version": runtime["executables"]["pdftotext"]["version"],
            "executable_sha256": file_sha256(pdftotext_path),
        },
    }
    render_report_path = root / "render-report.json"
    _write_json(render_report_path, render_report)

    return create_rendered_text_geometry(
        spec_path,
        pptx_path,
        build_report_path,
        render_report_path,
        runtime_path,
        root / "rendered-text-geometry.json",
    )


def _identity(name: str, sha256: str) -> dict[str, str]:
    return {"path": f"evidence/{name}", "sha256": sha256}


def admission_fixture(*, page_id: str) -> dict[str, Any]:
    """Create one exact round-one admission JSON object for payload validation."""
    digests = {name: character * 64 for name, character in zip(
        (
            "spec",
            "pptx",
            "source",
            "preview",
            "pdf",
            "font_report",
            "build_report",
            "structure_report",
            "render_report",
            "runtime_preflight",
            "rendered_text_geometry",
            "background_contract",
            "visual_diff",
        ),
        "123456789abcd",
    )}
    artifacts = {
        name: _identity(name, digest) for name, digest in digests.items()
    }
    admission: dict[str, Any] = {
        "schema_version": 1,
        "page_id": page_id,
        "review_round": 1,
        "verification_profile": "strict",
        "spec_sha256": digests["spec"],
        "input_spec_sha256": "e" * 64,
        "review_state_sha256": "f" * 64,
        "pptx_sha256": digests["pptx"],
        "source_sha256": digests["source"],
        "preview_sha256": digests["preview"],
        "build_report_sha256": digests["build_report"],
        "structure_report_sha256": digests["structure_report"],
        "render_report_sha256": digests["render_report"],
        "rendered_text_geometry_sha256": digests["rendered_text_geometry"],
        "background_contract_sha256": digests["background_contract"],
        "visual_diff_sha256": digests["visual_diff"],
        "artifacts": artifacts,
        "render_identity": {
            "renderer": {"backend": "libreoffice"},
            "rasterizer": {"name": "pdftoppm"},
            "text_extractor": {"name": "pdftotext"},
            "pdffonts": {"name": "pdffonts"},
            "fontconfig": {"name": "fixture-fontconfig"},
            "runtime_preflight_sha256": digests["runtime_preflight"],
            "pdf_sha256": digests["pdf"],
            "font_report_sha256": digests["font_report"],
        },
        "visual_evidence": {
            "source": artifacts["source"],
            "preview": artifacts["preview"],
            "side_by_side": {
                "source": artifacts["source"],
                "preview": artifacts["preview"],
            },
            "overlay": _identity("overlay.png", "0" * 64),
            "diff": _identity("diff.png", "a" * 64),
            "regions": [],
        },
        "generator": {
            "name": "review_contracts.issue_admission",
            "schema_version": 1,
            **_identity("review_contracts.py", "b" * 64),
        },
    }
    admission["admission_id"] = review_contracts.recompute_admission_id(admission)
    if set(admission) != review_contracts._ADMISSION_FIELDS:
        raise AssertionError("admission fixture must match the exact production fields")
    return admission


def response_fixture(*, page_id: str) -> dict[str, Any]:
    """Create the exact nine-field response while changing only its page ID."""
    admission = admission_fixture(page_id="page-001")
    response = {
        "admission_id": admission["admission_id"],
        "page_id": page_id,
        "review_round": admission["review_round"],
        "source_sha256": admission["source_sha256"],
        "preview_sha256": admission["preview_sha256"],
        "decision": "passed",
        "coverage": {
            "canvas_and_regions": "checked",
            "objects_and_geometry": "checked",
            "text_and_typography": "checked",
            "tables_and_matrices": "not_applicable",
            "graphics_connectors_charts": "not_applicable",
            "pictures_crop_layers": "not_applicable",
            "high_risk_regions": "not_applicable",
        },
        "findings": [],
        "p2_disclosures": [],
    }
    if set(response) != set(review_contracts._PROMPT_FIELDS):
        raise AssertionError("response fixture must match the exact production fields")
    return response


def validate_response_fixture(
    admission: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    """Exercise the production response payload validator without file artifacts."""
    errors = review_contracts._validate_response_payload(response, admission)
    return {"valid": not errors, "errors": errors}


def background_picture_fixture(
    root: Path, *, asset_equals_clean_reference: bool
) -> dict[str, Any]:
    """Mutate the Task A8-3 shared schema into the fixed contaminated background."""
    if not asset_equals_clean_reference:
        raise ValueError("this fixture represents only the fixed Attempt 7 negative")
    spec = make_minimal_spec(root)
    reference = spec["clean_visual_reference"]
    background = next(
        item for item in spec["elements"] if item["element_id"] == "background-base"
    )
    background.update(
        {
            "kind": "picture",
            "editable": False,
            "style": {"rotation": 0, "opacity": 1},
            "content": {
                "asset": {
                    "path": reference["path"],
                    "asset_sha256": reference["sha256"],
                    "pixel_size": [1600, 900],
                },
                "mode": "none",
                "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            },
        }
    )
    spec["modules"]["background"]["items"][0].update(
        {
            "selected_mode": "background_picture",
            "source_provenance": {
                "kind": "clean_background_asset",
                "source_path": reference["path"],
                "source_sha256": reference["sha256"],
            },
            "reason": "fixed full-page clean-reference contamination negative",
            "evidence": [reference["path"]],
            "contains_foreground_semantics": False,
        }
    )
    return spec


class Attempt8PrecontrolRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_attempt7_metric_bbox_is_a_blocking_overflow(self) -> None:
        report = geometry_fixture(
            self.root / "geometry",
            text="已AI化/子场景-步骤总数",
            expected=[201.0, 245.0, 116.0, 30.0],
            actual=[201.1, 251.0, 144.13, 16.0],
        )

        self.assertEqual("overflow", report["elements"][0]["status"])
        self.assertFalse(report["valid"])
        self.assertEqual("failed", report["decision"])
        self.assertIn(
            "TEXT_GEOMETRY_OVERFLOW",
            {item["code"] for item in report["errors"]},
        )

    def test_attempt7_reviewer_page_alias_is_rejected(self) -> None:
        admission = admission_fixture(page_id="page-001")
        response = response_fixture(page_id="page-001-ai-operations")

        report = validate_response_fixture(admission, response)

        self.assertFalse(report["valid"])
        self.assertEqual(
            ["REVIEW_ADMISSION_PAGE_MISMATCH"],
            [item["code"] for item in report["errors"]],
        )

    def test_attempt7_full_page_source_cannot_be_background_picture(self) -> None:
        spec = background_picture_fixture(
            self.root / "background", asset_equals_clean_reference=True
        )

        issues = validate_background_prebuild(spec)

        self.assertIn(
            "BACKGROUND_FOREGROUND_CONTAMINATION_RISK",
            {item.code for item in issues},
        )


if __name__ == "__main__":
    unittest.main()
