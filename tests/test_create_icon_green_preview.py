from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "create_icon_green_preview.py"
)


def load_module():
    if not SCRIPT_PATH.is_file():
        raise AssertionError(f"missing script: {SCRIPT_PATH}")
    spec = importlib.util.spec_from_file_location("create_icon_green_preview", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CreateIconGreenPreviewTests(unittest.TestCase):
    def _write_spec(self, root: Path, asset: Path, *, declared_hash: str | None = None) -> Path:
        spec_path = root / "work" / "page-reconstruction.json"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text(
            json.dumps(
                {
                    "modules": {
                        "icons": {
                            "icons": [
                                {
                                    "icon_id": "calendar",
                                    "crop_mode": "alpha_isolation",
                                    "asset_path": str(asset),
                                    "asset_sha256": declared_hash or sha256(asset),
                                }
                            ]
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return spec_path

    def _write_rgba_icon(self, root: Path) -> Path:
        asset = root / "assets" / "icons" / "calendar.png"
        asset.parent.mkdir(parents=True)
        image = Image.new("RGBA", (12, 10), (255, 255, 255, 0))
        ImageDraw.Draw(image).rectangle((3, 2, 8, 7), fill=(255, 85, 20, 255))
        image.save(asset)
        return asset

    def test_preview_contains_only_final_rgba_icons_on_green(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = self._write_rgba_icon(root)
            spec_path = self._write_spec(root, asset)
            output = root / "comparisons" / "icon-alpha-preview.png"

            result = load_module().create_icon_green_preview(spec_path, output)

            self.assertTrue(result["ok"])
            self.assertEqual(result["icon_count"], 1)
            self.assertEqual(result["background"], "#00FF00")
            self.assertEqual(result["scale"], 4)
            self.assertEqual(result["icon_ids"], ["calendar"])
            self.assertEqual(result["output"], str(output.resolve()))
            self.assertEqual(result["output_sha256"], sha256(output))
            self.assertNotIn("icon_manifest_sha256", result)
            self.assertNotIn("inspection", result)
            self.assertNotIn("reused", result)
            with Image.open(output) as preview:
                self.assertEqual(preview.mode, "RGB")
                self.assertEqual(preview.getpixel((0, 0)), (0, 255, 0))
                self.assertEqual(preview.getpixel((28, 48)), (255, 85, 20))

    def test_rejects_asset_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = self._write_rgba_icon(root)
            spec_path = self._write_spec(root, asset, declared_hash="0" * 64)

            with self.assertRaisesRegex(ValueError, "asset_sha256 mismatch"):
                load_module().create_icon_green_preview(
                    spec_path,
                    root / "comparisons" / "icon-alpha-preview.png",
                )

    def test_rejects_non_rgba_or_fully_opaque_icon(self) -> None:
        module = load_module()
        for mode, color in (("RGB", (255, 255, 255)), ("RGBA", (255, 85, 20, 255))):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                asset = root / "assets" / "icons" / "calendar.png"
                asset.parent.mkdir(parents=True)
                Image.new(mode, (8, 8), color).save(asset)
                spec_path = self._write_spec(root, asset)

                with self.assertRaisesRegex(ValueError, "RGBA|transparent background"):
                    module.create_icon_green_preview(
                        spec_path,
                        root / "comparisons" / "icon-alpha-preview.png",
                    )

    def test_rejects_spec_without_icons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "page-reconstruction.json"
            spec_path.write_text('{"modules":{"icons":{"icons":[]}}}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "at least one icon"):
                load_module().create_icon_green_preview(
                    spec_path,
                    root / "icon-alpha-preview.png",
                )


if __name__ == "__main__":
    unittest.main()
