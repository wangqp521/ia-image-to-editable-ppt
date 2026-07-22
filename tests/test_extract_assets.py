from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image, ImageDraw

from tests.fixture_specs import add_picture_asset, make_text_spec, refresh_reference_identity


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import extract_assets as MODULE
from lib.error_codes import ToolError
from lib.hashing import file_sha256


class ExtractAssetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.assets = self.root / "assets"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _picture_spec(self, processor: str = "source_patch") -> dict:
        spec = make_text_spec(self.root)
        source = Path(spec["clean_visual_reference"]["path"])
        image = Image.open(source).convert("RGB")
        ImageDraw.Draw(image).rectangle((100, 100, 219, 179), fill=(20, 80, 180))
        image.save(source)
        refresh_reference_identity(spec)
        return add_picture_asset(spec, processor=processor)

    def test_source_patch_is_exact_and_backfills_identity(self) -> None:
        spec = self._picture_spec()
        updated, report = MODULE.extract_assets(spec, self.assets)
        asset = updated["elements"][1]["content"]["asset"]
        asset_path = Path(asset["asset_path"])

        with Image.open(spec["clean_visual_reference"]["path"]) as source_image:
            expected = source_image.convert("RGB").crop((100, 100, 220, 180))
        with Image.open(asset_path) as actual:
            self.assertEqual(actual.convert("RGB").tobytes(), expected.tobytes())
        self.assertEqual(asset["asset_sha256"], file_sha256(asset_path))
        self.assertEqual([asset["final_width"], asset["final_height"]], [120, 80])
        self.assertEqual(report["items"][0]["status"], "succeeded")

    def test_explicit_mask_creates_rgba_and_records_alpha_hash(self) -> None:
        mask = self.root / "mask.png"
        Image.new("L", (120, 80), 0).save(mask)
        with Image.open(mask) as opened:
            pixels = opened.copy()
        ImageDraw.Draw(pixels).rectangle((10, 10, 109, 69), fill=255)
        pixels.save(mask)
        spec = self._picture_spec("explicit_mask")
        spec["elements"][1]["content"]["asset"]["mask_path"] = str(mask)

        updated, _ = MODULE.extract_assets(spec, self.assets)
        asset = updated["elements"][1]["content"]["asset"]
        with Image.open(asset["asset_path"]) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getchannel("A").getextrema(), (0, 255))
        self.assertRegex(asset["alpha_mask_sha256"], r"^[0-9a-f]{64}$")

    def test_non_icon_alpha_isolation_fails_closed(self) -> None:
        spec = self._picture_spec("alpha_isolation")
        with self.assertRaisesRegex(ToolError, "ALPHA_EXTRACTION_UNSAFE"):
            MODULE.extract_assets(spec, self.assets)

    def test_batch_failure_does_not_mutate_spec_or_publish_assets(self) -> None:
        spec = self._picture_spec()
        add_picture_asset(spec, element_id="bad", source_bbox=[1590, 890, 30, 30])
        original = copy.deepcopy(spec)
        with self.assertRaisesRegex(ToolError, "BBOX_OUT_OF_RANGE"):
            MODULE.extract_assets(spec, self.assets)
        self.assertEqual(spec, original)
        self.assertFalse(self.assets.exists())

    def test_cli_writes_spec_and_report(self) -> None:
        spec = self._picture_spec()
        spec_path = self.root / "spec.json"
        output_path = self.root / "updated.json"
        report_path = self.root / "assets-report.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "extract_assets.py"),
                "--spec",
                str(spec_path),
                "--assets-dir",
                str(self.assets),
                "--output",
                str(output_path),
                "--report",
                str(report_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(output_path.is_file())
        self.assertTrue(report_path.is_file())

    def test_current_asset_identity_is_reused_without_reextracting(self) -> None:
        spec = self._picture_spec()
        updated, _ = MODULE.extract_assets(spec, self.assets)
        with mock.patch.dict(
            MODULE.GENERIC_PROCESSORS,
            {"source_patch": mock.Mock(side_effect=AssertionError("must reuse"))},
        ):
            reused, report = MODULE.extract_assets(updated, self.assets)
        self.assertTrue(report["cache_hit"])
        self.assertTrue(report["items"][0]["reused"])
        self.assertEqual(
            reused["elements"][1]["content"]["asset"]["asset_sha256"],
            updated["elements"][1]["content"]["asset"]["asset_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
