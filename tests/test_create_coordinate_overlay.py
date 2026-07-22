from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "create_coordinate_overlay.py"


def load_module():
    if not SCRIPT_PATH.is_file():
        raise AssertionError(f"missing script: {SCRIPT_PATH}")
    spec = importlib.util.spec_from_file_location("create_coordinate_overlay", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoordinateOverlayTests(unittest.TestCase):
    def test_reports_image_identity_alpha_and_direct_mapping(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            output = root / "overlay.png"
            Image.new("RGBA", (320, 180), (255, 255, 255, 128)).save(source)

            report = module.create_coordinate_overlay(source, output, cols=32, rows=18)

            self.assertEqual([320, 180], report["source"]["pixel_size"])
            self.assertTrue(report["source"]["has_alpha"])
            self.assertEqual("direct_16_9", report["mapping"]["mode"])
            self.assertRegex(report["source"]["sha256"], r"^[0-9a-f]{64}$")
            with Image.open(output) as overlay:
                self.assertEqual((320, 180), overlay.size)

    def test_non_16_9_image_uses_contain_mapping(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            Image.new("RGB", (100, 100), "white").save(source)

            report = module.create_coordinate_overlay(source, root / "overlay.png")

            self.assertEqual("contain", report["mapping"]["mode"])
            self.assertGreater(report["mapping"]["offset_in"][0], 0)
            self.assertEqual(0.0, report["mapping"]["offset_in"][1])

    def test_invalid_grid_is_rejected(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            Image.new("RGB", (160, 90), "white").save(source)

            with self.assertRaisesRegex(ValueError, "cols and rows must be positive"):
                module.create_coordinate_overlay(source, root / "overlay.png", cols=0)


if __name__ == "__main__":
    unittest.main()
