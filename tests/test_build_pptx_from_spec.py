from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from PIL import Image
from pptx import Presentation

from tests.fixture_specs import (
    add_merged_table,
    add_matrix_and_status,
    add_picture_asset,
    add_shape_and_line,
    make_text_spec,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_pptx_from_spec as MODULE
import extract_assets
from lib.error_codes import ToolError
from lib.hashing import canonical_json_sha256


def prebuild_report(spec: dict) -> dict:
    return {
        "valid": True,
        "stage": "prebuild",
        "verification_profile": spec.get("verification_profile", "strict"),
        "spec_sha256": canonical_json_sha256(spec),
        "errors": [],
        "warnings": [],
    }


class BuildPptxFromSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "page.pptx"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _slide_xml(self) -> str:
        with ZipFile(self.output) as archive:
            return archive.read("ppt/slides/slide1.xml").decode("utf-8")

    def test_builder_rejects_stale_prebuild_report(self) -> None:
        spec = make_text_spec(self.root)
        report = prebuild_report(spec)
        report["spec_sha256"] = "0" * 64
        with self.assertRaisesRegex(ToolError, "SPEC_HASH_MISMATCH"):
            MODULE.build_single_page(spec, report, self.output)

    def test_text_shape_and_line_are_native_and_named(self) -> None:
        spec = add_shape_and_line(make_text_spec(self.root))
        build = MODULE.build_single_page(spec, prebuild_report(spec), self.output)
        prs = Presentation(self.output)
        names = {shape.name for shape in prs.slides[0].shapes}
        self.assertIn("ia:title", names)
        self.assertIn("ia:card", names)
        self.assertIn("ia:divider", names)
        self.assertTrue(any(shape.has_text_frame for shape in prs.slides[0].shapes))
        self.assertEqual(build["elements"]["title"]["object_type"], "text")

    def test_builder_preserves_declared_font_without_autofit(self) -> None:
        spec = make_text_spec(self.root)
        MODULE.build_single_page(spec, prebuild_report(spec), self.output)
        xml = self._slide_xml()
        self.assertNotIn("normAutofit", xml)
        self.assertIn('typeface="Noto Sans CJK SC"', xml)

    def test_text_run_applies_high_fidelity_character_properties(self) -> None:
        spec = make_text_spec(self.root)
        run = spec["modules"]["typography"]["items"][0]["runs"][0]
        run.update(
            {
                "italic": True,
                "strike": True,
                "baseline": "superscript",
                "letter_spacing": 120,
            }
        )
        MODULE.build_single_page(spec, prebuild_report(spec), self.output)
        xml = self._slide_xml()
        self.assertIn('i="1"', xml)
        self.assertIn('strike="sngStrike"', xml)
        self.assertIn('baseline="30000"', xml)
        self.assertIn('spc="120"', xml)

    def test_table_preserves_nonuniform_sizes_and_merge(self) -> None:
        spec = add_merged_table(make_text_spec(self.root))
        MODULE.build_single_page(spec, prebuild_report(spec), self.output)
        prs = Presentation(self.output)
        table = next(shape.table for shape in prs.slides[0].shapes if shape.has_table)
        self.assertEqual([column.width for column in table.columns], [1219200, 2438400])
        self.assertTrue(table.cell(0, 0).is_merge_origin)

    def test_table_applies_cell_fill_margin_alignment_font_and_local_borders(self) -> None:
        spec = add_merged_table(make_text_spec(self.root))
        cell = spec["elements"][-1]["content"]["cells"][1]
        cell.update(
            {
                "fill": "#112233",
                "margins": {"left": 1000, "right": 2000, "top": 3000, "bottom": 4000},
                "horizontal_alignment": "center",
                "vertical_alignment": "middle",
                "text_style": {
                    "font_name": "Noto Sans CJK SC",
                    "font_size": 13,
                    "font_weight": 700,
                    "color": "#445566",
                },
                "borders": {
                    "left": {"color": "#FF0000", "width_emu": 25400, "dash": "solid"},
                    "top": None,
                    "right": None,
                    "bottom": None,
                },
            }
        )
        MODULE.build_single_page(spec, prebuild_report(spec), self.output)
        xml = self._slide_xml()
        self.assertIn("112233", xml)
        self.assertIn("445566", xml)
        self.assertIn("FF0000", xml)
        self.assertIn('marL="1000"', xml)
        self.assertIn('anchor="ctr"', xml)

    def test_picture_contain_is_an_independent_picture_object(self) -> None:
        spec = add_picture_asset(make_text_spec(self.root))
        source = Path(spec["clean_visual_reference"]["path"])
        Image.new("RGB", (1600, 900), "white").save(source)
        spec["clean_visual_reference"]["sha256"] = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
        spec["content_reference"]["sha256"] = spec["clean_visual_reference"]["sha256"]
        spec, _ = extract_assets.extract_assets(spec, self.root / "assets")
        build = MODULE.build_single_page(spec, prebuild_report(spec), self.output)
        prs = Presentation(self.output)
        pictures = [shape for shape in prs.slides[0].shapes if shape.shape_type == 13]
        self.assertEqual(len(pictures), 1)
        self.assertEqual(pictures[0].name, "ia:photo")
        self.assertEqual(build["elements"]["photo"]["object_type"], "picture")

    def test_picture_cover_uses_symmetric_crop_without_stretching(self) -> None:
        spec = add_picture_asset(make_text_spec(self.root))
        spec, _ = extract_assets.extract_assets(spec, self.root / "assets")
        spec["elements"][1]["slide_bbox"] = [762000, 762000, 914400, 914400]
        spec["elements"][1]["content"]["placement"]["mode"] = "cover"
        MODULE.build_single_page(spec, prebuild_report(spec), self.output)
        prs = Presentation(self.output)
        picture = next(shape for shape in prs.slides[0].shapes if shape.shape_type == 13)
        self.assertGreater(picture.crop_left, 0)
        self.assertAlmostEqual(picture.crop_left, picture.crop_right, places=6)
        self.assertEqual(picture.crop_top, 0)
        self.assertEqual(picture.crop_bottom, 0)

    def test_picture_cover_honors_focus_and_rotation(self) -> None:
        spec = add_picture_asset(make_text_spec(self.root))
        spec, _ = extract_assets.extract_assets(spec, self.root / "assets")
        picture_element = spec["elements"][1]
        picture_element["slide_bbox"] = [762000, 762000, 914400, 914400]
        picture_element["content"]["placement"].update(
            {"mode": "cover", "focus_x": 0.25, "focus_y": 0.5, "rotation": 12}
        )
        MODULE.build_single_page(spec, prebuild_report(spec), self.output)
        picture = next(shape for shape in Presentation(self.output).slides[0].shapes if shape.shape_type == 13)
        self.assertNotAlmostEqual(picture.crop_left, picture.crop_right, places=6)
        self.assertEqual(picture.rotation, 12)

    def test_picture_declared_unsupported_effect_fails_closed(self) -> None:
        spec = add_picture_asset(make_text_spec(self.root))
        spec, _ = extract_assets.extract_assets(spec, self.root / "assets")
        spec["elements"][1]["content"]["placement"]["shadow"] = {"blur": 4}
        with self.assertRaisesRegex(ToolError, "UNSUPPORTED_FEATURE"):
            MODULE.build_single_page(spec, prebuild_report(spec), self.output)

    def test_line_arrowheads_are_written_and_unknown_route_fails_closed(self) -> None:
        spec = add_shape_and_line(make_text_spec(self.root))
        spec["elements"][-1]["style"]["line"].update(
            {"begin_arrow": "none", "end_arrow": "triangle"}
        )
        MODULE.build_single_page(spec, prebuild_report(spec), self.output)
        self.assertIn("tailEnd", self._slide_xml())
        self.assertIn('type="triangle"', self._slide_xml())

        rejected = add_shape_and_line(make_text_spec(self.root / "rejected"))
        rejected["elements"][-1]["style"]["route"] = "bent"
        with self.assertRaisesRegex(ToolError, "UNSUPPORTED_FEATURE"):
            MODULE.build_single_page(rejected, prebuild_report(rejected), self.output)

    def test_matrix_and_status_use_stable_named_native_parts(self) -> None:
        spec = add_matrix_and_status(make_text_spec(self.root))
        build = MODULE.build_single_page(spec, prebuild_report(spec), self.output)
        prs = Presentation(self.output)
        names = {shape.name for shape in prs.slides[0].shapes}
        self.assertIn("ia:matrix:cell-0-0", names)
        self.assertIn("ia:matrix:cell-0-1", names)
        self.assertIn("ia:status:segment-0", names)
        self.assertIn("ia:status:segment-1", names)
        self.assertEqual(build["elements"]["matrix"]["object_count"], 2)
        self.assertEqual(build["elements"]["status"]["object_count"], 2)

    def test_unknown_element_kind_fails_closed(self) -> None:
        spec = make_text_spec(self.root)
        spec["elements"][0]["kind"] = "chart"
        with self.assertRaisesRegex(ToolError, "UNSUPPORTED_FEATURE"):
            MODULE.build_single_page(spec, prebuild_report(spec), self.output)

    def test_cli_writes_pptx_and_build_report(self) -> None:
        spec = make_text_spec(self.root)
        spec_path = self.root / "spec.json"
        prebuild_path = self.root / "prebuild.json"
        build_path = self.root / "build-report.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        prebuild_path.write_text(json.dumps(prebuild_report(spec)), encoding="utf-8")
        result = MODULE.main(
            [
                "--spec",
                str(spec_path),
                "--prebuild-report",
                str(prebuild_path),
                "--output",
                str(self.output),
                "--build-report",
                str(build_path),
            ]
        )
        self.assertEqual(result, 0)
        self.assertTrue(self.output.is_file())
        self.assertTrue(build_path.is_file())


if __name__ == "__main__":
    unittest.main()
