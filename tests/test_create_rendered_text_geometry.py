"""Behavior tests for rendered PDF text geometry evidence."""

from __future__ import annotations

import copy
import hashlib
import html
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fixture_specs import make_minimal_spec


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
SCRIPT = SCRIPTS_ROOT / "create_rendered_text_geometry.py"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from lib.hashing import canonical_json_sha256, file_sha256
from lib.spec_identity import content_spec_sha256, input_spec_sha256


def load_module():
    module_spec = importlib.util.spec_from_file_location(
        "test_rendered_text_geometry_script", SCRIPT
    )
    if module_spec is None or module_spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


def bbox_xml(words: list[tuple[str, float, float, float, float]]) -> str:
    body = "".join(
        f'<word xMin="{x0}" yMin="{y0}" xMax="{x1}" yMax="{y1}">{html.escape(text)}</word>'
        for text, x0, y0, x1, y1 in words
    )
    return (
        '<doc><page width="960" height="540"><flow><block><line>'
        f"{body}</line></block></flow></page></doc>"
    )


POPPLER_XHTML_DOCTYPE = (
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
    '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">'
)


def pt_to_emu(value: float) -> int:
    return round(value * 12_700)


def paragraph_contract(
    start: int, end: int, *, bullet: str | None = None
) -> dict[str, object]:
    paragraph: dict[str, object] = {
        "start": start,
        "end": end,
        "alignment": "left",
        "line_spacing": 1.0,
        "space_before": 0,
        "space_after": 0,
        "indent": 0,
        "list": {"is_list": False, "level": 0, "bullet": None},
    }
    if bullet is not None:
        paragraph.update({"margin_left": 170_000, "indent": -115_000})
        paragraph["list"] = {
            "is_list": True,
            "level": 0,
            "bullet_type": "char",
            "bullet": bullet,
            "bullet_font": "follow_text",
            "bullet_size_mode": "follow_text",
            "bullet_size_value": None,
            "bullet_color": "#E52B11",
        }
    return paragraph


class RenderedTextGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.module = load_module()
        self.run_count = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_json(self, path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _write_pdftotext(
        self,
        xml: str,
        *,
        returncode: int = 0,
        during_run_source: str = "",
        raw_stdout: bytes | None = None,
        raw_stderr: bytes | None = None,
    ) -> Path:
        xml_path = self.root / "bbox.xml"
        xml_path.write_text(xml, encoding="utf-8")
        executable = self.root / "pdftotext"
        source = (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"expected_pdf = {str((self.root / 'page.pdf').resolve())!r}\n"
            "expected = ['-bbox-layout', '-enc', 'UTF-8', expected_pdf, '-']\n"
            "if sys.argv[1:] != expected:\n"
            "    print('wrong arguments: ' + repr(sys.argv[1:]), file=sys.stderr)\n"
            "    raise SystemExit(91)\n"
            f"{during_run_source}"
        )
        source += (
            f"sys.stdout.buffer.write({raw_stdout!r})\n"
            if raw_stdout is not None
            else f"payload = Path({str(xml_path)!r}).read_text(encoding='utf-8')\n"
            "sys.stdout.write(payload)\n"
        )
        if raw_stderr is not None:
            source += f"sys.stderr.buffer.write({raw_stderr!r})\n"
        source += f"raise SystemExit({returncode})\n"
        executable.write_text(
            source,
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def _write_runtime_tool(self, name: str) -> Path:
        executable = self.root / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        return executable

    def run_geometry(
        self,
        xml: str,
        *,
        text: str = "测试标题",
        text_box_pt: list[float] | None = None,
        paragraphs: list[dict[str, object]] | None = None,
        mutate: callable | None = None,
        returncode: int = 0,
        during_run_source: str = "",
        raw_stdout: bytes | None = None,
        raw_stderr: bytes | None = None,
    ) -> dict[str, object]:
        self.run_count += 1
        if text_box_pt is None:
            text_box_pt = [18, 18, 100, 30]
        spec = make_minimal_spec(self.root / "source")
        element = next(
            item for item in spec["elements"] if item["element_id"] == "element-001"
        )
        typography = spec["modules"]["typography"]["items"][0]
        emu_bbox = [pt_to_emu(value) for value in text_box_pt]
        element["slide_bbox"] = list(emu_bbox)
        element["content"]["text"] = text
        typography["text"] = text
        typography["runs"][0]["end"] = len(text)
        if paragraphs is None:
            typography["paragraphs"][0]["end"] = len(text)
        else:
            typography["paragraphs"] = copy.deepcopy(paragraphs)
        for key, value in zip(("x", "y", "w", "h"), emu_bbox):
            typography["text_box"][key] = value

        spec_path = self.root / "page-reconstruction.json"
        self._write_json(spec_path, spec)
        pptx = self.root / "page.pptx"
        pptx.write_bytes(b"current-pptx")
        pdf = self.root / "page.pdf"
        pdf.write_bytes(b"current-pdf")
        pdftotext = self._write_pdftotext(
            xml,
            returncode=returncode,
            during_run_source=during_run_source,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
        )
        soffice = self._write_runtime_tool("soffice")
        pdftoppm = self._write_runtime_tool("pdftoppm")
        pdffonts = self._write_runtime_tool("pdffonts")
        fontconfig = self.root / "fontconfig.xml"
        fontconfig.write_text("<fontconfig/>\n", encoding="utf-8")

        build = {
            "valid": True,
            "schema_version": 1,
            "schema_sha256": canonical_json_sha256(spec),
            "content_spec_sha256": content_spec_sha256(spec),
            "input_spec_sha256": input_spec_sha256(spec),
            "pptx_sha256": file_sha256(pptx),
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
                            "bbox": list(emu_bbox),
                        }
                    ],
                }
            },
            "warnings": [],
            "unsupported": [],
        }
        build_path = self.root / "build-report.json"
        self._write_json(build_path, build)
        runtime = {
            "valid": True,
            "errors": [],
            "renderer_backend": "libreoffice",
            "executables": {
                "soffice": {
                    "path": str(soffice.resolve()),
                    "version": "LibreOffice 26.2.3.2",
                    "sha256": file_sha256(soffice),
                },
                "pdftoppm": {
                    "path": str(pdftoppm.resolve()),
                    "version": "pdftoppm version 26.07.0",
                    "sha256": file_sha256(pdftoppm),
                },
                "pdffonts": {
                    "path": str(pdffonts.resolve()),
                    "version": "pdffonts version 26.07.0",
                    "sha256": file_sha256(pdffonts),
                },
                "pdftotext": {
                    "path": str(pdftotext.resolve()),
                    "version": "pdftotext version 26.07.0",
                    "sha256": file_sha256(pdftotext),
                }
            },
            "fontconfig": {
                "path": str(fontconfig.resolve()),
                "sha256": file_sha256(fontconfig),
            },
        }
        runtime_path = self.root / "preflight-runtime.json"
        self._write_json(runtime_path, runtime)
        render = {
            "schema_version": 1,
            "pptx": {"path": str(pptx.resolve()), "sha256": file_sha256(pptx)},
            "pdf": {
                "path": str(pdf.resolve()),
                "sha256": file_sha256(pdf),
                "pages": 1,
                "page_size_pt": [960.0, 540.0],
            },
            "renderer": {
                "backend": "libreoffice",
                "path": str(soffice.resolve()),
                "version": runtime["executables"]["soffice"]["version"],
                "executable_sha256": file_sha256(soffice),
                "fontconfig_path": str(fontconfig.resolve()),
                "fontconfig_sha256": file_sha256(fontconfig),
            },
            "rasterizer": {
                "path": str(pdftoppm.resolve()),
                "version": runtime["executables"]["pdftoppm"]["version"],
                "executable_sha256": file_sha256(pdftoppm),
            },
            "text_extractor": {
                "path": str(pdftotext.resolve()),
                "version": runtime["executables"]["pdftotext"]["version"],
                "executable_sha256": file_sha256(pdftotext),
            },
        }
        render_path = self.root / "render-report.json"
        self._write_json(render_path, render)

        paths = {
            "spec": spec_path,
            "pptx": pptx,
            "build": build_path,
            "render": render_path,
            "runtime": runtime_path,
            "pdf": pdf,
            "pdftotext": pdftotext,
            "output": self.root / f"rendered-text-geometry-{self.run_count}.json",
        }
        self.last_output = paths["output"]
        payloads = {"spec": spec, "build": build, "render": render, "runtime": runtime}
        if mutate is not None:
            mutate(paths, payloads)
        return self.module.create_rendered_text_geometry(
            paths["spec"],
            paths["pptx"],
            paths["build"],
            paths["render"],
            paths["runtime"],
            paths["output"],
        )

    def two_equal_distance_duplicates(self) -> str:
        return bbox_xml(
            [
                ("测试标题", 44, 20, 68, 30),
                ("测试标题", 68, 20, 92, 30),
            ]
        )

    def test_normalization_nfc_removes_all_unicode_whitespace_only(self) -> None:
        value = " e\u0301\u00a0测\u2003试\n42%/项-总数 "
        self.assertEqual("é测试42%/项-总数", self.module.normalize_match_text(value))

    def test_bbox_parser_preserves_document_order_and_line_identity(self) -> None:
        xml = (
            '<doc><page width="960" height="540"><flow><block>'
            '<line><word xMin="1" yMin="2" xMax="3" yMax="4">A</word></line>'
            '<line><word xMin="5" yMin="6" xMax="7" yMax="8">B</word></line>'
            "</block></flow></page></doc>"
        )
        tokens, size = self.module.parse_bbox_layout(xml)
        self.assertEqual((960.0, 540.0), size)
        self.assertEqual(["A", "B"], [token.text for token in tokens])
        self.assertEqual([0, 1], [token.line_index for token in tokens])

    def test_bbox_parser_accepts_exact_poppler_xhtml_doctype(self) -> None:
        xml = (
            POPPLER_XHTML_DOCTYPE
            + '<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>'
            '<page width="960" height="540"><flow><block><line>'
            '<word xMin="1" yMin="2" xMax="3" yMax="4">A</word>'
            "</line></block></flow></page></doc></body></html>"
        )

        tokens, size = self.module.parse_bbox_layout(xml)

        self.assertEqual((960.0, 540.0), size)
        self.assertEqual(["A"], [token.text for token in tokens])
        self.assertEqual([0], [token.line_index for token in tokens])

    def test_bbox_parser_accepts_bom_and_whitespace_after_poppler_doctype(
        self,
    ) -> None:
        xml = (
            "\ufeff"
            + POPPLER_XHTML_DOCTYPE
            + " \t\r\n"
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>'
            '<page width="960" height="540"><flow><block><line>'
            '<word xMin="5" yMin="6" xMax="7" yMax="8">B</word>'
            "</line></block></flow></page></doc></body></html>"
        )

        tokens, size = self.module.parse_bbox_layout(xml)

        self.assertEqual((960.0, 540.0), size)
        self.assertEqual(["B"], [token.text for token in tokens])
        self.assertEqual([0], [token.line_index for token in tokens])

    def _assert_bbox_xml_invalid(self, xml: str) -> None:
        with self.assertRaises(self.module.GeometryError) as raised:
            self.module.parse_bbox_layout(xml)
        self.assertEqual("TEXT_GEOMETRY_XML_INVALID", raised.exception.code)
        self.assertEqual("pdftotext.stdout", raised.exception.path)

    def test_bbox_parser_rejects_all_other_dtd_and_entity_declarations(
        self,
    ) -> None:
        valid_root = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>'
            '<page width="960" height="540"/></doc></body></html>'
        )
        cases = (
            (
                "mixed-case-entity",
                '<!EnTiTy x "boom">' + valid_root,
            ),
            (
                "internal-entity",
                '<!DOCTYPE html [<!ENTITY x "boom">]>' + valid_root,
            ),
            (
                "external-entity",
                '<!DOCTYPE html [<!ENTITY x SYSTEM "file:///not-read">]>'
                + valid_root,
            ),
            (
                "modified-public-id",
                '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" '
                '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">'
                + valid_root,
            ),
            (
                "modified-system-id",
                '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
                '"https://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">'
                + valid_root,
            ),
            (
                "repeated-doctype",
                POPPLER_XHTML_DOCTYPE + POPPLER_XHTML_DOCTYPE + valid_root,
            ),
            (
                "doctype-after-leading-whitespace",
                " \n" + POPPLER_XHTML_DOCTYPE + valid_root,
            ),
            (
                "doctype-in-document",
                "<doc>" + POPPLER_XHTML_DOCTYPE + "</doc>",
            ),
        )
        for name, xml in cases:
            with self.subTest(name=name):
                self._assert_bbox_xml_invalid(xml)

    def test_bbox_parser_rejects_malformed_multiple_page_and_nonfinite_xml(self) -> None:
        cases = (
            "<doc><page>",
            '<doc><page width="960" height="540"/><page width="960" height="540"/></doc>',
            '<doc><page width="NaN" height="540"/></doc>',
            '<doc><page width="960" height="540"><word xMin="x" yMin="0" xMax="1" yMax="1">A</word></page></doc>',
        )
        for xml in cases:
            with self.subTest(xml=xml[:30]):
                with self.assertRaises(self.module.GeometryError) as raised:
                    self.module.parse_bbox_layout(xml)
                self.assertEqual(
                    "TEXT_GEOMETRY_XML_INVALID", raised.exception.code
                )

    def test_bbox_parser_rejects_zero_width_or_height_words(self) -> None:
        for word in (
            ("测试标题", 20, 20, 20, 30),
            ("测试标题", 20, 20, 60, 20),
        ):
            with self.subTest(word=word):
                with self.assertRaises(self.module.GeometryError) as raised:
                    self.module.parse_bbox_layout(bbox_xml([word]))
                self.assertEqual("TEXT_GEOMETRY_XML_INVALID", raised.exception.code)

    def test_extreme_finite_word_publishes_serializable_xml_failure(self) -> None:
        try:
            report = self.run_geometry(
                bbox_xml([("测试标题", -1e308, 20, 1e308, 30)]),
                text_box_pt=[0, 18, 100, 30],
            )
        except self.module.GeometryError as exc:
            self.fail(f"failure report was not published: {exc}")
        self.assertFalse(report["valid"])
        self.assertEqual(report, json.loads(self.last_output.read_text()))
        self.assertIn(
            "TEXT_GEOMETRY_XML_INVALID",
            {error["code"] for error in report["errors"]},
        )

    def test_complete_chinese_text_inside_box_passes(self) -> None:
        report = self.run_geometry(
            bbox_xml([("测试", 18, 18, 40, 35), ("标题", 41, 18, 65, 35)])
        )
        self.assertTrue(report["valid"])
        self.assertEqual("passed", report["decision"])
        self.assertEqual("passed", report["elements"][0]["status"])
        self.assertEqual(2, len(report["elements"][0]["matched_tokens"]))

    def test_multiline_numbers_units_and_punctuation_match_completely(self) -> None:
        xml = (
            '<doc><page width="960" height="540"><flow><block>'
            '<line><word xMin="18" yMin="18" xMax="50" yMax="28">目标 ≥</word></line>'
            '<line><word xMin="18" yMin="30" xMax="56" yMax="40">42%/项-总数</word></line>'
            "</block></flow></page></doc>"
        )
        report = self.run_geometry(xml, text="目标 ≥\n42%/项-总数")
        item = report["elements"][0]
        self.assertEqual("passed", item["status"])
        self.assertEqual(2, item["line_count"])
        self.assertEqual("目标≥42%/项-总数", item["normalized_text"])

    def test_non_list_multiline_text_uses_exact_box_spatial_fallback_after_interleaved_flow(
        self,
    ) -> None:
        text = "每月：场景价值复盘 + 低效Skill优化/下线"
        xml = (
            '<doc><page width="960" height="540">'
            '<flow><block><line>'
            '<word xMin="18" yMin="18" xMax="38" yMax="28">每月：</word>'
            '<word xMin="41" yMin="18" xMax="71" yMax="28">场景价值复盘</word>'
            '<word xMin="74" yMin="18" xMax="78" yMax="28">+</word>'
            '</line></block></flow>'
            '<flow><block><line>'
            '<word xMin="150" yMin="20" xMax="170" yMax="30">他框</word>'
            '</line></block></flow>'
            '<flow><block><line>'
            '<word xMin="18" yMin="32" xMax="30" yMax="42">低效</word>'
            '<word xMin="33" yMin="32" xMax="45" yMax="42">Skill</word>'
            '<word xMin="48" yMin="32" xMax="60" yMax="42">优化</word>'
            '<word xMin="63" yMin="32" xMax="67" yMax="42">/</word>'
            '<word xMin="70" yMin="32" xMax="82" yMax="42">下线</word>'
            '</line></block></flow>'
            '</page></doc>'
        )

        report = self.run_geometry(
            xml,
            text=text,
            text_box_pt=[18, 18, 100, 30],
        )

        item = report["elements"][0]
        self.assertEqual("passed", item["status"])
        self.assertEqual(
            ["每月：", "场景价值复盘", "+", "低效", "Skill", "优化", "/", "下线"],
            [token["text"] for token in item["matched_tokens"]],
        )
        self.assertEqual([18.0, 18.0, 64.0, 24.0], item["actual_bbox_pt"])
        self.assertEqual(2, item["line_count"])

    def test_non_list_spatial_fallback_excludes_margin_neighbor_and_occupies_exact_sequence(
        self,
    ) -> None:
        expected = {
            "element_id": "spatial-fallback",
            "text": "ABC",
            "match_text": "ABC",
            "has_char_bullet": False,
            "expected_bbox_pt": [18, 18, 70, 30],
        }
        tokens = [
            self.module.PdfToken("A", "A", 18, 18, 28, 28, 0, 0),
            self.module.PdfToken("邻", "邻", 90, 18, 98, 28, 1, 1),
            self.module.PdfToken("B", "B", 32, 32, 42, 42, 2, 2),
            self.module.PdfToken("C", "C", 46, 32, 56, 42, 2, 3),
        ]
        occupied: set[int] = set()

        result = self.module._match_element(expected, tokens, occupied)

        self.assertEqual("passed", result["status"])
        self.assertEqual(
            [0, 2, 3],
            [token["token_index"] for token in result["matched_tokens"]],
        )
        self.assertEqual([18, 18, 38, 24], result["actual_bbox_pt"])
        self.assertEqual(2, result["line_count"])
        self.assertEqual({0, 2, 3}, occupied)

    def test_non_list_spatial_fallback_rejects_any_inexact_box_sequence_without_occupying(
        self,
    ) -> None:
        expected = {
            "element_id": "spatial-fallback",
            "text": "ABC",
            "match_text": "ABC",
            "has_char_bullet": False,
            "expected_bbox_pt": [18, 18, 70, 30],
        }
        outside = self.module.PdfToken("邻", "邻", 90, 18, 98, 28, 1, 1)
        cases = {
            "extra-before": [
                self.module.PdfToken("A", "A", 32, 18, 42, 28, 0, 0),
                outside,
                self.module.PdfToken("Q", "Q", 18, 18, 28, 28, 0, 2),
                self.module.PdfToken("B", "B", 32, 32, 42, 42, 2, 3),
                self.module.PdfToken("C", "C", 46, 32, 56, 42, 2, 4),
            ],
            "extra-middle": [
                self.module.PdfToken("A", "A", 18, 18, 28, 28, 0, 0),
                outside,
                self.module.PdfToken("Q", "Q", 32, 18, 42, 28, 0, 2),
                self.module.PdfToken("B", "B", 32, 32, 42, 42, 2, 3),
                self.module.PdfToken("C", "C", 46, 32, 56, 42, 2, 4),
            ],
            "extra-after": [
                self.module.PdfToken("A", "A", 18, 18, 28, 28, 0, 0),
                outside,
                self.module.PdfToken("B", "B", 32, 32, 42, 42, 2, 2),
                self.module.PdfToken("C", "C", 46, 32, 56, 42, 2, 3),
                self.module.PdfToken("Q", "Q", 60, 32, 70, 42, 2, 4),
            ],
            "wrong": [
                self.module.PdfToken("A", "A", 18, 18, 28, 28, 0, 0),
                outside,
                self.module.PdfToken("D", "D", 32, 32, 42, 42, 2, 2),
                self.module.PdfToken("C", "C", 46, 32, 56, 42, 2, 3),
            ],
            "missing": [
                self.module.PdfToken("A", "A", 18, 18, 28, 28, 0, 0),
                outside,
                self.module.PdfToken("B", "B", 32, 32, 42, 42, 2, 2),
            ],
        }

        for name, tokens in cases.items():
            with self.subTest(name=name):
                occupied: set[int] = set()
                result = self.module._match_element(expected, tokens, occupied)

                self.assertNotEqual("passed", result["status"])
                self.assertIsNone(result["actual_bbox_pt"])
                self.assertEqual([], result["matched_tokens"])
                self.assertEqual(set(), occupied)

    def test_non_list_primary_full_match_is_not_reinterpreted_by_spatial_fallback(
        self,
    ) -> None:
        expected = {
            "element_id": "primary-match",
            "text": "ABC",
            "match_text": "ABC",
            "has_char_bullet": False,
            "expected_bbox_pt": [18, 18, 70, 30],
        }
        tokens = [
            self.module.PdfToken("ABC", "ABC", 18, 18, 48, 28, 0, 0),
        ]
        occupied: set[int] = set()

        result = self.module._match_element(expected, tokens, occupied)

        self.assertEqual("passed", result["status"])
        self.assertEqual([0], [token["token_index"] for token in result["matched_tokens"]])
        self.assertEqual({0}, occupied)

    def test_single_char_bullet_paragraph_matches_bullet_and_body_tokens(
        self,
    ) -> None:
        text = "单段正文"
        xml = (
            '<doc><page width="960" height="540"><flow><block><line>'
            '<word xMin="18" yMin="18" xMax="22" yMax="30">•</word>'
            '<word xMin="25" yMin="18" xMax="65" yMax="30">单段正文</word>'
            "</line></block></flow></page></doc>"
        )

        report = self.run_geometry(
            xml,
            text=text,
            paragraphs=[paragraph_contract(0, len(text), bullet="•")],
        )

        item = report["elements"][0]
        self.assertEqual("passed", item["status"])
        self.assertEqual(
            ["•", "单段正文"],
            [token["text"] for token in item["matched_tokens"]],
        )
        self.assertEqual(text, item["original_text"])
        self.assertEqual(text, item["normalized_text"])

    def test_mixed_paragraphs_project_each_exact_char_bullet_contiguously(
        self,
    ) -> None:
        text = "普通一列表二列表三"
        xml = (
            '<doc><page width="960" height="540"><flow><block>'
            '<line><word xMin="18" yMin="18" xMax="48" yMax="28">普通一</word></line>'
            '<line><word xMin="18" yMin="30" xMax="22" yMax="40">•</word>'
            '<word xMin="25" yMin="30" xMax="55" yMax="40">列表二</word></line>'
            '<line><word xMin="18" yMin="42" xMax="22" yMax="52">▪</word>'
            '<word xMin="25" yMin="42" xMax="55" yMax="52">列表三</word></line>'
            "</block></flow></page></doc>"
        )

        report = self.run_geometry(
            xml,
            text=text,
            text_box_pt=[18, 18, 100, 36],
            paragraphs=[
                paragraph_contract(0, 3),
                paragraph_contract(3, 6, bullet="•"),
                paragraph_contract(6, 9, bullet="▪"),
            ],
        )

        item = report["elements"][0]
        self.assertEqual("passed", item["status"])
        self.assertEqual(
            ["普通一", "•", "列表二", "▪", "列表三"],
            [token["text"] for token in item["matched_tokens"]],
        )
        self.assertEqual(3, item["line_count"])
        self.assertEqual(text, item["original_text"])
        self.assertEqual(text, item["normalized_text"])

    def test_non_list_literal_bullet_remains_part_of_content_matching(self) -> None:
        text = "正文•字符"
        exact = bbox_xml(
            [
                ("正文", 18, 18, 38, 30),
                ("•", 39, 18, 43, 30),
                ("字符", 44, 18, 64, 30),
            ]
        )
        missing = bbox_xml(
            [("正文", 18, 18, 38, 30), ("字符", 44, 18, 64, 30)]
        )

        exact_report = self.run_geometry(
            exact,
            text=text,
            paragraphs=[paragraph_contract(0, len(text))],
        )
        missing_report = self.run_geometry(
            missing,
            text=text,
            paragraphs=[paragraph_contract(0, len(text))],
        )

        self.assertEqual("passed", exact_report["elements"][0]["status"])
        self.assertEqual(
            ["正文", "•", "字符"],
            [
                token["text"]
                for token in exact_report["elements"][0]["matched_tokens"]
            ],
        )
        self.assertEqual("incomplete", missing_report["elements"][0]["status"])

    def test_char_bullet_projection_requires_the_exact_contract_character(
        self,
    ) -> None:
        text = "正文"
        report = self.run_geometry(
            bbox_xml(
                [("•", 18, 18, 22, 30), ("正文", 25, 18, 45, 30)]
            ),
            text=text,
            paragraphs=[paragraph_contract(0, len(text), bullet="▪")],
        )

        self.assertNotEqual("passed", report["elements"][0]["status"])
        self.assertEqual([], report["elements"][0]["matched_tokens"])

    def test_whitespace_only_char_bullet_fails_identity_before_matching(
        self,
    ) -> None:
        for name, bullet in (
            ("space", " "),
            ("tab", "\t"),
            ("non-breaking-space", "\u00a0"),
            ("em-space", "\u2003"),
        ):
            with self.subTest(name=name):
                report = self.run_geometry(
                    bbox_xml([("正文", 25, 18, 45, 30)]),
                    text="正文",
                    paragraphs=[paragraph_contract(0, 2, bullet=bullet)],
                )

                self.assertFalse(report["valid"])
                self.assertEqual([], report["elements"])
                self.assertEqual(
                    [
                        {
                            "code": "TEXT_GEOMETRY_IDENTITY_MISMATCH",
                            "path": (
                                "modules.typography.items.element-001."
                                "paragraphs[0].list.bullet"
                            ),
                            "detail": "char bullet must contain visible text",
                        }
                    ],
                    report["errors"],
                )

    def test_char_list_orders_spatial_lines_without_consuming_other_columns(
        self,
    ) -> None:
        text = "甲段乙段"
        xml = (
            '<doc><page width="960" height="540">'
            '<flow><block><line>'
            '<word xMin="18" yMin="18" xMax="22" yMax="28">•</word>'
            '<word xMin="25" yMin="18" xMax="35" yMax="28">甲</word>'
            "</line></block></flow>"
            '<flow><block><line>'
            '<word xMin="150" yMin="18" xMax="170" yMax="28">他列</word>'
            "</line></block></flow>"
            '<flow><block><line>'
            '<word xMin="18" yMin="42" xMax="22" yMax="52">•</word>'
            '<word xMin="25" yMin="42" xMax="35" yMax="52">乙</word>'
            "</line></block></flow>"
            '<flow><block><line>'
            '<word xMin="25" yMin="30" xMax="35" yMax="40">段</word>'
            "</line></block></flow>"
            '<flow><block><line>'
            '<word xMin="25" yMin="54" xMax="35" yMax="64">段</word>'
            "</line></block></flow>"
            "</page></doc>"
        )

        report = self.run_geometry(
            xml,
            text=text,
            text_box_pt=[18, 18, 70, 48],
            paragraphs=[
                paragraph_contract(0, 2, bullet="•"),
                paragraph_contract(2, 4, bullet="•"),
            ],
        )

        item = report["elements"][0]
        self.assertEqual("passed", item["status"])
        self.assertEqual(
            ["•", "甲", "段", "•", "乙", "段"],
            [token["text"] for token in item["matched_tokens"]],
        )
        self.assertEqual(4, item["line_count"])

    def test_char_lists_own_only_tokens_centered_inside_each_exact_text_box(
        self,
    ) -> None:
        tokens = [
            self.module.PdfToken("•", "•", 18, 18, 22, 28, 0, 0),
            self.module.PdfToken("正", "正", 25, 18, 35, 28, 0, 1),
            self.module.PdfToken("•", "•", 90, 18, 94, 28, 1, 2),
            self.module.PdfToken("正", "正", 97, 18, 107, 28, 1, 3),
            self.module.PdfToken("文", "文", 25, 30, 35, 40, 2, 4),
            self.module.PdfToken("文", "文", 97, 30, 107, 40, 3, 5),
        ]
        left = {
            "element_id": "left-list",
            "text": "正文",
            "match_text": "•正文",
            "has_char_bullet": True,
            "expected_bbox_pt": [18, 18, 70, 30],
        }
        right = {
            "element_id": "right-list",
            "text": "正文",
            "match_text": "•正文",
            "has_char_bullet": True,
            "expected_bbox_pt": [89, 18, 70, 30],
        }
        occupied: set[int] = set()

        left_result = self.module._match_element(left, tokens, occupied)
        right_result = self.module._match_element(right, tokens, occupied)

        self.assertEqual("passed", left_result["status"])
        self.assertEqual("passed", right_result["status"])
        self.assertEqual(
            [0, 1, 4],
            [token["token_index"] for token in left_result["matched_tokens"]],
        )
        self.assertEqual(
            [2, 3, 5],
            [token["token_index"] for token in right_result["matched_tokens"]],
        )
        self.assertEqual([18, 18, 17, 22], left_result["actual_bbox_pt"])
        self.assertEqual([90, 18, 17, 22], right_result["actual_bbox_pt"])
        self.assertEqual(2, left_result["line_count"])
        self.assertEqual(2, right_result["line_count"])
        self.assertEqual(set(range(6)), occupied)

    def test_char_list_requires_one_complete_exact_box_token_sequence(
        self,
    ) -> None:
        expected = {
            "element_id": "exact-list",
            "text": "正文",
            "match_text": "•正文",
            "has_char_bullet": True,
            "expected_bbox_pt": [18, 18, 70, 42],
        }
        cases = {
            "extra-before": [
                self.module.PdfToken("旁", "旁", 18, 18, 28, 28, 0, 0),
                self.module.PdfToken("•", "•", 18, 30, 22, 40, 1, 1),
                self.module.PdfToken("正文", "正文", 25, 30, 45, 40, 1, 2),
            ],
            "extra-middle": [
                self.module.PdfToken("•", "•", 18, 18, 22, 28, 0, 0),
                self.module.PdfToken("旁", "旁", 25, 18, 35, 28, 0, 1),
                self.module.PdfToken("正文", "正文", 38, 18, 58, 28, 0, 2),
            ],
            "extra-after": [
                self.module.PdfToken("•", "•", 18, 18, 22, 28, 0, 0),
                self.module.PdfToken("正文", "正文", 25, 18, 45, 28, 0, 1),
                self.module.PdfToken("旁", "旁", 18, 30, 28, 40, 1, 2),
            ],
        }

        for name, tokens in cases.items():
            with self.subTest(name=name):
                occupied: set[int] = set()
                result = self.module._match_element(expected, tokens, occupied)

                self.assertEqual("incomplete", result["status"])
                self.assertIsNone(result["actual_bbox_pt"])
                self.assertEqual([], result["matched_tokens"])
                self.assertEqual(set(), occupied)

        tokens = [
            self.module.PdfToken("•", "•", 18, 18, 22, 28, 0, 0),
            self.module.PdfToken("正文", "正文", 25, 18, 45, 28, 0, 1),
        ]
        occupied = set()
        result = self.module._match_element(expected, tokens, occupied)
        self.assertEqual("passed", result["status"])
        self.assertEqual([18, 18, 27, 10], result["actual_bbox_pt"])
        self.assertEqual(1, result["line_count"])
        self.assertEqual({0, 1}, occupied)

    def test_auto_number_list_is_not_added_to_char_bullet_projection(self) -> None:
        text = "正文"
        paragraph = paragraph_contract(0, len(text), bullet="1.")
        paragraph["list"]["bullet_type"] = "auto_number"

        report = self.run_geometry(
            bbox_xml(
                [("1.", 18, 18, 22, 30), ("正文", 25, 18, 45, 30)]
            ),
            text=text,
            paragraphs=[paragraph],
        )

        item = report["elements"][0]
        self.assertEqual("passed", item["status"])
        self.assertEqual(["正文"], [token["text"] for token in item["matched_tokens"]])

    def test_attempt7_style_right_overflow_fails(self) -> None:
        report = self.run_geometry(
            bbox_xml([("已AI化/子场景-步骤总数", 201.1, 251.0, 345.23, 267.0)]),
            text="已AI化/子场景-步骤总数",
            text_box_pt=[201.0, 245.0, 116.0, 30.0],
        )
        item = report["elements"][0]
        self.assertFalse(report["valid"])
        self.assertEqual("overflow", item["status"])
        self.assertGreater(item["overflow_pt"]["right"], 28.0)

    def test_missing_partial_and_ambiguous_text_fail_closed(self) -> None:
        for xml, expected in (
            (bbox_xml([("测试", 18, 18, 40, 35)]), "incomplete"),
            (bbox_xml([]), "missing"),
            (self.two_equal_distance_duplicates(), "ambiguous"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(
                    expected, self.run_geometry(xml)["elements"][0]["status"]
                )

    def test_all_four_overflow_edges_obey_fixed_boundary(self) -> None:
        expected = [18.0, 18.0, 100.0, 30.0]
        edge_words = {
            "left": lambda delta: ("测试标题", 18.0 - delta, 20, 60, 30),
            "right": lambda delta: ("测试标题", 20, 20, 118.0 + delta, 30),
            "top": lambda delta: ("测试标题", 20, 18.0 - delta, 60, 30),
            "bottom": lambda delta: ("测试标题", 20, 20, 60, 48.0 + delta),
        }
        for edge, make_word in edge_words.items():
            for delta, status in ((1.49, "passed"), (1.51, "overflow")):
                with self.subTest(edge=edge, delta=delta):
                    report = self.run_geometry(
                        bbox_xml([make_word(delta)]), text_box_pt=expected
                    )
                    self.assertEqual(status, report["elements"][0]["status"])
                    self.assertAlmostEqual(
                        delta,
                        report["elements"][0]["overflow_pt"][edge],
                        places=6,
                    )

    def test_duplicate_text_uses_unique_nearest_spatial_candidate(self) -> None:
        report = self.run_geometry(
            bbox_xml(
                [
                    ("测试标题", 5, 20, 25, 30),
                    ("测试标题", 30, 20, 40, 30),
                ]
            ),
            text_box_pt=[18, 18, 24, 30],
        )
        item = report["elements"][0]
        self.assertEqual("passed", item["status"])
        self.assertEqual(30.0, item["actual_bbox_pt"][0])

    def test_first_token_outside_twelve_point_margin_is_not_a_candidate(self) -> None:
        report = self.run_geometry(
            bbox_xml([("测试标题", 5.0, 20, 5.98, 30)]),
            text_box_pt=[18, 18, 100, 30],
        )
        self.assertEqual("missing", report["elements"][0]["status"])

    def test_tokens_are_not_reused_by_later_reading_order_elements(self) -> None:
        def add_second(paths, payloads) -> None:
            spec = payloads["spec"]
            first = next(
                item for item in spec["elements"] if item["element_id"] == "element-001"
            )
            second = copy.deepcopy(first)
            second["element_id"] = "element-002"
            second["layer"] = 11
            spec["elements"].append(second)
            spec["regions"][0]["element_ids"].append("element-002")
            spec["reading_order"].append("element-002")
            typo = copy.deepcopy(spec["modules"]["typography"]["items"][0])
            typo["element_id"] = "element-002"
            spec["modules"]["typography"]["items"].append(typo)
            self._write_json(paths["spec"], spec)
            build = payloads["build"]
            build["schema_sha256"] = canonical_json_sha256(spec)
            build["content_spec_sha256"] = content_spec_sha256(spec)
            build["input_spec_sha256"] = input_spec_sha256(spec)
            build["elements"]["element-002"] = copy.deepcopy(
                build["elements"]["element-001"]
            )
            build["elements"]["element-002"]["objects"][0]["ooxml_name"] = (
                "ia:element-002"
            )
            self._write_json(paths["build"], build)

        report = self.run_geometry(
            bbox_xml([("测试标题", 20, 20, 60, 30)]), mutate=add_second
        )
        self.assertEqual(["passed", "missing"], [x["status"] for x in report["elements"]])

    def test_reading_order_includes_native_special_text_but_excludes_picture_art(self) -> None:
        def add_elements(paths, payloads) -> None:
            spec = payloads["spec"]
            first = next(
                item for item in spec["elements"] if item["element_id"] == "element-001"
            )
            special = copy.deepcopy(first)
            special.update({"element_id": "special-001", "kind": "special_text", "layer": 11})
            special["content"]["text"] = "原生艺术字"
            picture = {
                "element_id": "art-picture",
                "kind": "picture",
                "source_bbox": [0, 0, 10, 10],
                "slide_bbox": [0, 0, 127000, 127000],
                "layer": 12,
                "editable": False,
                "confidence": "high",
                "style": {"rotation": 0, "opacity": 1},
                "content": {},
            }
            spec["elements"].extend([special, picture])
            spec["regions"][0]["element_ids"].extend(["special-001", "art-picture"])
            spec["reading_order"].extend(["special-001", "art-picture"])
            typo = copy.deepcopy(spec["modules"]["typography"]["items"][0])
            typo.update({"element_id": "special-001", "text": "原生艺术字"})
            typo["runs"][0]["end"] = len("原生艺术字")
            typo["paragraphs"][0]["end"] = len("原生艺术字")
            spec["modules"]["typography"]["items"].append(typo)
            self._write_json(paths["spec"], spec)
            build = payloads["build"]
            build["schema_sha256"] = canonical_json_sha256(spec)
            build["content_spec_sha256"] = content_spec_sha256(spec)
            build["input_spec_sha256"] = input_spec_sha256(spec)
            build["elements"]["special-001"] = {
                "semantic_kind": "special_text",
                "selected_mode": "native",
                "object_type": "sp",
                "objects": [{"object_type": "sp", "ooxml_name": "ia:special-001", "text_summary": "原生艺术字"}],
            }
            build["elements"]["art-picture"] = {
                "semantic_kind": "picture",
                "selected_mode": "asset",
                "object_type": "pic",
                "objects": [{"object_type": "pic", "ooxml_name": "ia:art-picture"}],
            }
            self._write_json(paths["build"], build)

        report = self.run_geometry(
            bbox_xml(
                [
                    ("测试标题", 20, 20, 60, 30),
                    ("原生艺术字", 20, 32, 70, 42),
                ]
            ),
            mutate=add_elements,
        )
        self.assertEqual(
            ["element-001", "special-001"],
            [item["element_id"] for item in report["elements"]],
        )

    def test_report_contains_complete_chain_identities_and_constants(self) -> None:
        report = self.run_geometry(bbox_xml([("测试标题", 20, 20, 60, 30)]))
        self.assertEqual("page-001", report["page_id"])
        self.assertRegex(report["spec_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(report["input_spec_sha256"], r"^[0-9a-f]{64}$")
        for key in (
            "spec_file_sha256",
            "pptx_sha256",
            "build_report_sha256",
            "render_report_sha256",
            "runtime_sha256",
            "pdf_sha256",
        ):
            self.assertRegex(report["inputs"][key], r"^[0-9a-f]{64}$")
            self.assertEqual(report["inputs"][key], report[key])
        self.assertEqual(
            {
                "overflow_tolerance_pt": 1.5,
                "first_token_search_margin_pt": 12.0,
                "ambiguity_distance_pt": 0.5,
            },
            report["constants"],
        )
        self.assertEqual([960.0, 540.0], report["page_size_pt"])
        self.assertEqual("pdftotext version 26.07.0", report["pdftotext"]["version"])

    def test_stale_chain_inputs_fail_identity_closed_and_still_publish_report(self) -> None:
        mutations = {
            "content-spec": lambda paths, payloads: self._mutate_json_field(
                paths["build"], payloads["build"], "content_spec_sha256", "f" * 64
            ),
            "pptx": lambda paths, payloads: paths["pptx"].write_bytes(b"changed"),
            "pdf": lambda paths, payloads: paths["pdf"].write_bytes(b"changed"),
            "pdftotext": lambda paths, payloads: paths["pdftotext"].write_text(
                paths["pdftotext"].read_text(encoding="utf-8") + "\n# changed\n",
                encoding="utf-8",
            ),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    old_root, self.root = self.root, Path(directory)
                    try:
                        report = self.run_geometry(
                            bbox_xml([("测试标题", 20, 20, 60, 30)]), mutate=mutation
                        )
                        self.assertFalse(report["valid"])
                        self.assertEqual("failed", report["decision"])
                        self.assertIn(
                            "TEXT_GEOMETRY_IDENTITY_MISMATCH",
                            {error["code"] for error in report["errors"]},
                        )
                        published = json.loads(
                            self.last_output.read_text(
                                encoding="utf-8"
                            )
                        )
                        self.assertEqual(report, published)
                    finally:
                        self.root = old_root

    def test_files_drifting_during_extraction_fail_identity_closed(self) -> None:
        targets = {
            "spec": self.root / "page-reconstruction.json",
            "source": self.root / "source" / "source.png",
            "pptx": self.root / "page.pptx",
            "pdf": self.root / "page.pdf",
            "fontconfig": self.root / "fontconfig.xml",
            "runtime-tool": self.root / "soffice",
            "pdftotext": self.root / "pdftotext",
        }
        for name, target in targets.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    old_root, self.root = self.root, Path(directory)
                    try:
                        target = targets[name]
                        target = self.root / target.relative_to(old_root)
                        path_literal = str(target)
                        during_run = (
                            f"target = Path({path_literal!r})\n"
                            "target.write_bytes(target.read_bytes() + b'drift')\n"
                        )
                        report = self.run_geometry(
                            bbox_xml([("测试标题", 20, 20, 60, 30)]),
                            during_run_source=during_run,
                        )
                        self.assertFalse(report["valid"])
                        self.assertEqual(report, json.loads(self.last_output.read_text()))
                        self.assertIn(
                            "TEXT_GEOMETRY_IDENTITY_MISMATCH",
                            {error["code"] for error in report["errors"]},
                        )
                    finally:
                        self.root = old_root

    def test_pdf_and_extractor_are_revalidated_after_failed_command(self) -> None:
        for name in ("pdf", "pdftotext"):
            with self.subTest(name=name):
                target = self.root / ("page.pdf" if name == "pdf" else "pdftotext")
                during_run = (
                    f"target = Path({str(target)!r})\n"
                    "target.write_bytes(target.read_bytes() + b'drift')\n"
                )
                report = self.run_geometry(
                    bbox_xml([("测试标题", 20, 20, 60, 30)]),
                    during_run_source=during_run,
                    returncode=7,
                )
                self.assertFalse(report["valid"])
                self.assertEqual(
                    ["TEXT_GEOMETRY_IDENTITY_MISMATCH"],
                    [error["code"] for error in report["errors"]],
                )

    def test_post_use_drift_overrides_exit_zero_downstream_failures(self) -> None:
        cases = (
            ("malformed-xml", "<doc>"),
            (
                "geometry-overflow",
                bbox_xml([("测试标题", 14, 20, 119, 30)]),
            ),
        )
        for target_name in ("pdf", "pdftotext"):
            for failure_name, xml in cases:
                with self.subTest(target=target_name, failure=failure_name):
                    target = self.root / (
                        "page.pdf" if target_name == "pdf" else "pdftotext"
                    )
                    during_run = (
                        f"target = Path({str(target)!r})\n"
                        "target.write_bytes(target.read_bytes() + b'drift')\n"
                    )
                    report = self.run_geometry(
                        xml,
                        during_run_source=during_run,
                    )
                    self.assertFalse(report["valid"])
                    self.assertEqual([], report["elements"])
                    self.assertEqual(
                        ["TEXT_GEOMETRY_IDENTITY_MISMATCH"],
                        [error["code"] for error in report["errors"]],
                    )
                    self.assertEqual(
                        report,
                        json.loads(self.last_output.read_text(encoding="utf-8")),
                    )

    def test_runtime_renderer_identity_drift_fails_closed(self) -> None:
        def stale_renderer(paths, payloads) -> None:
            payloads["runtime"]["executables"]["soffice"]["sha256"] = "f" * 64
            self._write_json(paths["runtime"], payloads["runtime"])

        report = self.run_geometry(
            bbox_xml([("测试标题", 20, 20, 60, 30)]), mutate=stale_renderer
        )
        self.assertFalse(report["valid"])
        self.assertIn(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH",
            {error["code"] for error in report["errors"]},
        )

    def test_runtime_version_placeholder_fails_closed(self) -> None:
        def unavailable_version(paths, payloads) -> None:
            payloads["runtime"]["executables"]["pdftotext"]["version"] = (
                "unavailable: timed out"
            )
            payloads["render"]["text_extractor"]["version"] = (
                "unavailable: timed out"
            )
            self._write_json(paths["runtime"], payloads["runtime"])
            self._write_json(paths["render"], payloads["render"])

        report = self.run_geometry(
            bbox_xml([("测试标题", 20, 20, 60, 30)]), mutate=unavailable_version
        )
        self.assertFalse(report["valid"])
        self.assertIn(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH",
            {error["code"] for error in report["errors"]},
        )

    def _mutate_json_field(
        self, path: Path, payload: dict[str, object], field: str, value: object
    ) -> None:
        payload[field] = value
        self._write_json(path, payload)

    def test_wrong_page_size_and_xml_page_size_mismatch_fail_closed(self) -> None:
        def wrong_report_size(paths, payloads) -> None:
            payloads["render"]["pdf"]["page_size_pt"] = [959.0, 540.0]
            self._write_json(paths["render"], payloads["render"])

        for xml, mutation in (
            (bbox_xml([("测试标题", 20, 20, 60, 30)]), wrong_report_size),
            (
                bbox_xml([("测试标题", 20, 20, 60, 30)]).replace(
                    'width="960"', 'width="959"'
                ),
                None,
            ),
        ):
            with self.subTest(xml=xml[:35]):
                with tempfile.TemporaryDirectory() as directory:
                    old_root, self.root = self.root, Path(directory)
                    try:
                        report = self.run_geometry(xml, mutate=mutation)
                        self.assertFalse(report["valid"])
                        self.assertIn(
                            "TEXT_GEOMETRY_IDENTITY_MISMATCH",
                            {error["code"] for error in report["errors"]},
                        )
                    finally:
                        self.root = old_root

    def test_malformed_spec_containers_publish_structured_failure(self) -> None:
        def malformed_canvas(paths, payloads) -> None:
            self._replace_spec_and_rebind_build(paths, payloads, "canvas", [])

        def malformed_reading_order(paths, payloads) -> None:
            self._replace_spec_and_rebind_build(
                paths, payloads, "reading_order", [[]]
            )

        for name, mutation in (
            ("canvas", malformed_canvas),
            ("reading-order", malformed_reading_order),
        ):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    old_root, self.root = self.root, Path(directory)
                    try:
                        report = self.run_geometry(
                            bbox_xml([("测试标题", 20, 20, 60, 30)]),
                            mutate=mutation,
                        )
                        self.assertFalse(report["valid"])
                        self.assertEqual(report, json.loads(self.last_output.read_text()))
                        self.assertIn(
                            "TEXT_GEOMETRY_IDENTITY_MISMATCH",
                            {error["code"] for error in report["errors"]},
                        )
                    finally:
                        self.root = old_root

    def _replace_spec_and_rebind_build(
        self, paths, payloads, field: str, value: object
    ) -> None:
        payloads["spec"][field] = value
        self._rebind_current_spec(paths, payloads)

    def _rebind_current_spec(self, paths, payloads) -> None:
        self._write_json(paths["spec"], payloads["spec"])
        payloads["build"]["schema_sha256"] = canonical_json_sha256(payloads["spec"])
        payloads["build"]["content_spec_sha256"] = content_spec_sha256(
            payloads["spec"]
        )
        payloads["build"]["input_spec_sha256"] = input_spec_sha256(
            payloads["spec"]
        )
        self._write_json(paths["build"], payloads["build"])

    def _assert_structural_identity_failure(self, report) -> None:
        self.assertFalse(report["valid"])
        self.assertEqual("failed", report["decision"])
        self.assertEqual([], report["elements"])
        codes = {error["code"] for error in report["errors"]}
        self.assertIn("TEXT_GEOMETRY_IDENTITY_MISMATCH", codes)
        self.assertNotIn("TEXT_GEOMETRY_COMMAND_FAILED", codes)
        self.assertNotIn("TEXT_GEOMETRY_MISSING", codes)

    def test_reading_order_missing_element_coverage_fails_before_extraction(self) -> None:
        def missing_text(paths, payloads) -> None:
            payloads["spec"]["reading_order"] = ["background-base"]
            self._rebind_current_spec(paths, payloads)

        report = self.run_geometry(
            bbox_xml([("测试标题", 20, 20, 60, 30)]),
            mutate=missing_text,
            returncode=91,
        )
        self._assert_structural_identity_failure(report)

    def test_reading_order_unknown_element_fails_before_extraction(self) -> None:
        def unknown_element(paths, payloads) -> None:
            payloads["spec"]["reading_order"].append("unknown-element")
            self._rebind_current_spec(paths, payloads)

        report = self.run_geometry(
            bbox_xml([("测试标题", 20, 20, 60, 30)]),
            mutate=unknown_element,
            returncode=91,
        )
        self._assert_structural_identity_failure(report)

    def test_duplicate_reading_order_id_is_an_input_error_not_missing_text(self) -> None:
        def duplicate_order(paths, payloads) -> None:
            payloads["spec"]["reading_order"].append("element-001")
            self._rebind_current_spec(paths, payloads)

        report = self.run_geometry(
            bbox_xml([("测试标题", 20, 20, 60, 30)]), mutate=duplicate_order
        )
        self._assert_structural_identity_failure(report)

    def test_duplicate_element_id_fails_before_extraction(self) -> None:
        def duplicate_element(paths, payloads) -> None:
            duplicate = copy.deepcopy(payloads["spec"]["elements"][0])
            payloads["spec"]["elements"].append(duplicate)
            self._rebind_current_spec(paths, payloads)

        report = self.run_geometry(
            bbox_xml([("测试标题", 20, 20, 60, 30)]),
            mutate=duplicate_element,
            returncode=91,
        )
        self._assert_structural_identity_failure(report)

    def test_duplicate_required_typography_id_fails_before_extraction(self) -> None:
        def duplicate_typography(paths, payloads) -> None:
            duplicate = copy.deepcopy(
                payloads["spec"]["modules"]["typography"]["items"][0]
            )
            payloads["spec"]["modules"]["typography"]["items"].append(duplicate)
            self._rebind_current_spec(paths, payloads)

        report = self.run_geometry(
            bbox_xml([("测试标题", 20, 20, 60, 30)]),
            mutate=duplicate_typography,
            returncode=91,
        )
        self._assert_structural_identity_failure(report)

    def test_malformed_native_text_content_fails_before_extraction(self) -> None:
        def malformed_content(paths, payloads) -> None:
            payloads["spec"]["elements"][0]["content"] = []
            self._rebind_current_spec(paths, payloads)

        def non_string_text(paths, payloads) -> None:
            payloads["spec"]["elements"][0]["content"]["text"] = 123
            self._rebind_current_spec(paths, payloads)

        for name, mutation in (
            ("content", malformed_content),
            ("text", non_string_text),
        ):
            with self.subTest(name=name):
                report = self.run_geometry(
                    bbox_xml([("测试标题", 20, 20, 60, 30)]),
                    mutate=mutation,
                    returncode=91,
                )
                self._assert_structural_identity_failure(report)

    def test_normalized_empty_native_text_is_legally_excluded(self) -> None:
        for text in ("", "\u00a0\u2003\n\t"):
            with self.subTest(text=repr(text)):
                report = self.run_geometry(bbox_xml([]), text=text)
                self.assertTrue(report["valid"])
                self.assertEqual("passed", report["decision"])
                self.assertEqual([], report["elements"])
                self.assertEqual([], report["errors"])

    def test_pdftotext_command_failure_is_structured(self) -> None:
        report = self.run_geometry(
            bbox_xml([("测试标题", 20, 20, 60, 30)]), returncode=7
        )
        self.assertFalse(report["valid"])
        self.assertIn(
            "TEXT_GEOMETRY_COMMAND_FAILED",
            {error["code"] for error in report["errors"]},
        )

    def test_pdftotext_timeout_is_structured(self) -> None:
        with mock.patch.object(self.module, "PDFTOTEXT_TIMEOUT_SECONDS", 0.05):
            report = self.run_geometry(
                bbox_xml([("测试标题", 20, 20, 60, 30)]),
                during_run_source="import time\ntime.sleep(1)\n",
            )
        self.assertFalse(report["valid"])
        self.assertIn(
            "TEXT_GEOMETRY_COMMAND_FAILED",
            {error["code"] for error in report["errors"]},
        )

    def test_pdftotext_invalid_utf8_is_a_command_failure(self) -> None:
        report = self.run_geometry(
            bbox_xml([("测试标题", 20, 20, 60, 30)]), raw_stdout=b"\xff\xfe"
        )
        self.assertFalse(report["valid"])
        self.assertEqual(
            ["TEXT_GEOMETRY_COMMAND_FAILED"],
            [error["code"] for error in report["errors"]],
        )

    def test_pdftotext_output_limits_kill_process_and_bound_error_detail(self) -> None:
        cases = (
            {"raw_stdout": b"x" * 129},
            {"raw_stderr": b"y" * 1024, "returncode": 7},
        )
        for kwargs in cases:
            with self.subTest(stream=next(iter(kwargs))):
                with mock.patch.object(
                    self.module, "PDFTOTEXT_STDOUT_LIMIT_BYTES", 128, create=True
                ), mock.patch.object(
                    self.module, "PDFTOTEXT_STDERR_LIMIT_BYTES", 128, create=True
                ):
                    report = self.run_geometry(
                        bbox_xml([("测试标题", 20, 20, 60, 30)]), **kwargs
                    )
                self.assertFalse(report["valid"])
                self.assertEqual(
                    ["TEXT_GEOMETRY_COMMAND_FAILED"],
                    [error["code"] for error in report["errors"]],
                )
                self.assertLessEqual(len(report["errors"][0]["detail"]), 256)

    def test_selector_setup_failures_reap_process_and_close_pipes(self) -> None:
        pdf = (self.root / "page.pdf").resolve()
        pdf.write_bytes(b"pdf")
        executable = self._write_pdftotext(
            bbox_xml([("测试标题", 20, 20, 60, 30)]),
            during_run_source="import time\ntime.sleep(30)\n",
        )
        real_popen = subprocess.Popen

        class RegisterFailingSelector:
            def __init__(self) -> None:
                self.closed = False

            def register(self, *_args: object) -> None:
                raise RuntimeError("register failed")

            def close(self) -> None:
                self.closed = True

        selector = RegisterFailingSelector()
        factories = (
            lambda: (_ for _ in ()).throw(RuntimeError("selector failed")),
            lambda: selector,
        )
        for factory in factories:
            with self.subTest(factory=factory):
                captured: list[subprocess.Popen[bytes]] = []

                def recording_popen(*args: object, **kwargs: object):
                    process = real_popen(*args, **kwargs)
                    captured.append(process)
                    return process

                actual: Exception | None = None
                try:
                    with mock.patch.object(
                        self.module.subprocess, "Popen", side_effect=recording_popen
                    ), mock.patch.object(
                        self.module.selectors, "DefaultSelector", side_effect=factory
                    ):
                        self.module._run_pdftotext(executable, pdf)
                except Exception as exc:
                    actual = exc

                process = captured[0]
                was_reaped = process.poll() is not None
                stdout_was_closed = process.stdout is not None and process.stdout.closed
                stderr_was_closed = process.stderr is not None and process.stderr.closed
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

                self.assertIsInstance(actual, self.module.GeometryError)
                self.assertEqual("TEXT_GEOMETRY_COMMAND_FAILED", actual.code)
                self.assertTrue(was_reaped)
                self.assertTrue(stdout_was_closed)
                self.assertTrue(stderr_was_closed)

    def test_started_pdftotext_lifecycle_errors_are_bounded_and_cleaned_up(
        self,
    ) -> None:
        pdf = (self.root / "page.pdf").resolve()
        pdf.write_bytes(b"pdf")
        real_popen = subprocess.Popen
        real_selector_factory = self.module.selectors.DefaultSelector
        real_os_read = self.module.os.read

        class TrackingSelector:
            def __init__(self) -> None:
                self.delegate = real_selector_factory()
                self.closed = False

            def register(self, *args: object):
                return self.delegate.register(*args)

            def unregister(self, *args: object):
                return self.delegate.unregister(*args)

            def select(self, *args: object):
                return self.delegate.select(*args)

            def get_map(self):
                return self.delegate.get_map()

            def close(self) -> None:
                try:
                    self.delegate.close()
                finally:
                    self.closed = True

        for stage in ("read", "wait"):
            with self.subTest(stage=stage):
                ready_marker = self.root / f"pdftotext-{stage}-ready"
                during_run_source = (
                    f"Path({str(ready_marker)!r}).write_text('ready', encoding='utf-8')\n"
                    "import time\n"
                    "sys.stdout.write('ready')\nsys.stdout.flush()\n"
                    "time.sleep(30)\n"
                    if stage == "read"
                    else f"Path({str(ready_marker)!r}).write_text('ready', encoding='utf-8')\n"
                    "import os\nimport time\n"
                    "os.close(1)\nos.close(2)\ntime.sleep(30)\n"
                )
                executable = self._write_pdftotext(
                    bbox_xml([("测试标题", 20, 20, 60, 30)]),
                    during_run_source=during_run_source,
                )
                captured: list[subprocess.Popen[bytes]] = []
                selectors_created: list[TrackingSelector] = []
                cleanup_waits: list[callable] = []
                injection_observed_ready = False
                process_was_alive_before_injection = False

                def recording_popen(*args: object, **kwargs: object):
                    process = real_popen(*args, **kwargs)
                    captured.append(process)
                    real_wait = process.wait
                    cleanup_waits.append(real_wait)
                    if stage == "wait":
                        wait_attempts = 0

                        def failing_wait(*wait_args: object, **wait_kwargs: object):
                            nonlocal injection_observed_ready
                            nonlocal process_was_alive_before_injection
                            nonlocal wait_attempts
                            wait_attempts += 1
                            if wait_attempts == 1:
                                injection_observed_ready = (
                                    ready_marker.read_text(encoding="utf-8") == "ready"
                                )
                                process_was_alive_before_injection = process.poll() is None
                                raise OSError("W" * 10_000)
                            return real_wait(*wait_args, **wait_kwargs)

                        process.wait = failing_wait
                    return process

                def tracking_selector_factory() -> TrackingSelector:
                    selector = TrackingSelector()
                    selectors_created.append(selector)
                    return selector

                def read_proxy(fd: int, count: int) -> bytes:
                    nonlocal injection_observed_ready, process_was_alive_before_injection
                    process_pipe_fds = {
                        stream.fileno()
                        for process in captured
                        for stream in (process.stdout, process.stderr)
                        if stream is not None and not stream.closed
                    }
                    if stage == "read" and fd in process_pipe_fds:
                        injection_observed_ready = (
                            ready_marker.read_text(encoding="utf-8") == "ready"
                        )
                        process_was_alive_before_injection = captured[0].poll() is None
                        raise OSError("R" * 10_000)
                    return real_os_read(fd, count)

                actual: Exception | None = None
                try:
                    with mock.patch.object(
                        self.module.subprocess, "Popen", side_effect=recording_popen
                    ), mock.patch.object(
                        self.module.selectors,
                        "DefaultSelector",
                        side_effect=tracking_selector_factory,
                    ), mock.patch.object(
                        self.module.os, "read", side_effect=read_proxy
                    ):
                        self.module._run_pdftotext(executable, pdf)
                except Exception as exc:
                    actual = exc

                process = captured[0]
                selector = selectors_created[0]
                was_reaped = process.poll() is not None
                stdout_was_closed = process.stdout is not None and process.stdout.closed
                stderr_was_closed = process.stderr is not None and process.stderr.closed
                selector_was_closed = selector.closed
                if process.poll() is None:
                    process.kill()
                    cleanup_waits[0]()
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
                if not selector.closed:
                    selector.close()

                self.assertIsInstance(actual, self.module.GeometryError)
                self.assertEqual("TEXT_GEOMETRY_COMMAND_FAILED", actual.code)
                self.assertTrue(injection_observed_ready)
                self.assertTrue(process_was_alive_before_injection)
                self.assertTrue(was_reaped)
                self.assertTrue(stdout_was_closed)
                self.assertTrue(stderr_was_closed)
                self.assertTrue(selector_was_closed)
                self.assertLessEqual(
                    len(actual.detail), self.module.COMMAND_ERROR_DETAIL_LIMIT_CHARS
                )

    def test_pdftotext_exit_error_detail_is_bounded(self) -> None:
        pdf = (self.root / "page.pdf").resolve()
        pdf.write_bytes(b"pdf")
        executable = self._write_pdftotext(
            bbox_xml([("测试标题", 20, 20, 60, 30)]),
            returncode=7,
            raw_stderr=b"E" * 10_000,
        )
        with self.assertRaises(self.module.GeometryError) as exit_error:
            self.module._run_pdftotext(executable, pdf)
        self.assertEqual("TEXT_GEOMETRY_COMMAND_FAILED", exit_error.exception.code)
        self.assertTrue(
            exit_error.exception.detail.startswith("exit=7:"),
            exit_error.exception.detail,
        )
        self.assertLessEqual(
            len(exit_error.exception.detail),
            self.module.COMMAND_ERROR_DETAIL_LIMIT_CHARS,
        )

    def test_pdftotext_start_error_detail_is_bounded(self) -> None:
        pdf = self.root / "page.pdf"
        pdf.write_bytes(b"pdf")
        executable = self.root / "pdftotext"
        with mock.patch.object(
            self.module.subprocess, "Popen", side_effect=OSError("P" * 10_000)
        ):
            with self.assertRaises(self.module.GeometryError) as start_error:
                self.module._run_pdftotext(executable, pdf)
        self.assertEqual("TEXT_GEOMETRY_COMMAND_FAILED", start_error.exception.code)
        self.assertLessEqual(
            len(start_error.exception.detail),
            self.module.COMMAND_ERROR_DETAIL_LIMIT_CHARS,
        )

    def test_closed_pipes_still_use_the_single_command_deadline(self) -> None:
        pdf = self.root / "page.pdf"
        pdf.write_bytes(b"pdf")
        executable = self.root / "pdftotext-closed-pipes"

        def write_executable(delay: float) -> None:
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import time\n"
                "os.close(1)\n"
                "os.close(2)\n"
                f"time.sleep({delay!r})\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)

        write_executable(1.2)
        started = time.monotonic()
        output: str | None = None
        unexpected: Exception | None = None
        with mock.patch.object(self.module, "PDFTOTEXT_TIMEOUT_SECONDS", 2.0):
            try:
                output = self.module._run_pdftotext(executable, pdf)
            except Exception as exc:
                unexpected = exc
        elapsed = time.monotonic() - started
        self.assertIsNone(unexpected)
        self.assertEqual("", output)
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertLess(elapsed, 2.0)

        write_executable(0.3)
        started = time.monotonic()
        with mock.patch.object(self.module, "PDFTOTEXT_TIMEOUT_SECONDS", 0.1):
            with self.assertRaises(self.module.GeometryError) as caught:
                self.module._run_pdftotext(executable, pdf)
        elapsed = time.monotonic() - started
        self.assertEqual("TEXT_GEOMETRY_COMMAND_FAILED", caught.exception.code)
        self.assertIn("timed out", caught.exception.detail)
        self.assertLess(elapsed, 0.8)

    def test_symlink_loop_input_is_structured_for_api_and_cli(self) -> None:
        loop = self.root / "loop"
        loop.symlink_to(loop.name)

        api_output = self.root / "api-loop-report.json"
        report = self.module.create_rendered_text_geometry(
            loop, loop, loop, loop, loop, api_output
        )
        self.assertFalse(report["valid"])
        self.assertEqual(report, json.loads(api_output.read_text(encoding="utf-8")))
        self.assertIn(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH",
            {error["code"] for error in report["errors"]},
        )

        cli_output = self.root / "cli-loop-report.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(loop),
                "--pptx",
                str(loop),
                "--build-report",
                str(loop),
                "--render-report",
                str(loop),
                "--runtime",
                str(loop),
                "--output",
                str(cli_output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertNotIn("Traceback", completed.stderr)
        cli_report = json.loads(cli_output.read_text(encoding="utf-8"))
        self.assertFalse(cli_report["valid"])
        self.assertIn(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH",
            {error["code"] for error in cli_report["errors"]},
        )

    def test_existing_output_is_never_overwritten_by_cli(self) -> None:
        output = self.root / "rendered-text-geometry.json"
        output.write_text("keep-me", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(self.root / "missing-spec.json"),
                "--pptx",
                str(self.root / "missing.pptx"),
                "--build-report",
                str(self.root / "missing-build.json"),
                "--render-report",
                str(self.root / "missing-render.json"),
                "--runtime",
                str(self.root / "missing-runtime.json"),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertEqual("keep-me", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
