from __future__ import annotations

import sys
import tempfile
import unittest
import copy
from pathlib import Path

from PIL import Image

from tests.fixture_specs import add_picture_asset, make_text_spec


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import create_asset_crop_review as MODULE
import extract_assets
from lib.hashing import file_sha256


class CreateAssetCropReviewTests(unittest.TestCase):
    def test_review_contains_green_asset_panel_and_manifest_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = add_picture_asset(make_text_spec(root))
            updated, _ = extract_assets.extract_assets(spec, root / "assets")
            output = root / "asset-review.png"

            report = MODULE.render_asset_review(updated, output)

            self.assertTrue(output.is_file())
            self.assertRegex(report["manifest_sha256"], r"^[0-9a-f]{64}$")
            with Image.open(output) as image:
                colors = image.convert("RGB").getcolors(maxcolors=image.width * image.height)
            self.assertIsNotNone(colors)
            self.assertIn((0, 255, 0), {color for _, color in colors or []})

    def _icon_spec(self, root: Path) -> dict:
        spec = make_text_spec(root)
        source = Path(spec["clean_visual_reference"]["path"])
        Image.new("RGB", (1600, 900), "white").save(source)
        asset = root / "icon.png"
        icon = Image.new("RGBA", (12, 10), (10, 20, 30, 255))
        for x in range(3):
            icon.putpixel((x, 0), (10, 20, 30, 0))
        icon.save(asset)
        spec["elements"].append(
            {
                "element_id": "icon-1",
                "kind": "icon",
                "source_bbox": [40, 50, 12, 10],
                "slide_bbox": [304800, 381000, 91440, 76200],
                "layer": 3,
                "editable": False,
                "confidence": "high",
                "style": {},
                "content": {
                    "asset": {
                        "asset_path": str(asset),
                        "asset_sha256": file_sha256(asset),
                        "alpha_mask_sha256": "a" * 64,
                        "crop_mode": "alpha_isolation",
                        "background_handling": "edge_connected_background_removed",
                        "fallback_reason": None,
                        "padding": 0,
                    }
                },
            }
        )
        return spec

    def test_icon_uses_400_percent_and_manifest_binds_crop_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self._icon_spec(root)
            output = root / "review.png"
            report = MODULE.render_asset_review(spec, output)

            self.assertEqual(report["items"][0]["review_scale"], 4)
            manifest = report["manifest"][0]
            self.assertEqual(manifest["kind"], "icon")
            self.assertEqual(manifest["crop_mode"], "alpha_isolation")
            self.assertEqual(manifest["alpha_mask_sha256"], "a" * 64)
            self.assertEqual(manifest["background_handling"], "edge_connected_background_removed")

    def test_picture_uses_250_percent_review_scale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = add_picture_asset(make_text_spec(root), source_bbox=[100, 100, 20, 16])
            updated, _ = extract_assets.extract_assets(spec, root / "assets")
            report = MODULE.render_asset_review(updated, root / "review.png")
            photo = next(item for item in report["items"] if item["element_id"] == "photo")
            self.assertEqual(photo["review_scale"], 2.5)

    def test_review_reuses_only_when_manifest_dependencies_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self._icon_spec(root)
            output = root / "review.png"
            first = MODULE.render_asset_review(spec, output)
            second = MODULE.render_asset_review(copy.deepcopy(spec), output)
            changed = copy.deepcopy(spec)
            changed["elements"][-1]["content"]["asset"]["background_handling"] = "changed"
            third = MODULE.render_asset_review(changed, output)

            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertFalse(third["reused"])
            self.assertNotEqual(second["manifest_sha256"], third["manifest_sha256"])


if __name__ == "__main__":
    unittest.main()
