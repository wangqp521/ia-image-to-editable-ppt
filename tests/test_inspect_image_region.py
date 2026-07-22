from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "inspect_image_region.py"


def load_module():
    if not SCRIPT_PATH.is_file():
        raise AssertionError(f"missing script: {SCRIPT_PATH}")
    spec = importlib.util.spec_from_file_location("inspect_image_region", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InspectImageRegionTests(unittest.TestCase):
    def test_cli_help_names_bbox_as_edges_not_width_and_height(self):
        module = load_module()
        output = io.StringIO()

        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_context:
            module._parse_args(["--help"])

        self.assertEqual(0, exit_context.exception.code)
        self.assertIn("LEFT,TOP,RIGHT,BOTTOM", output.getvalue())
        self.assertNotIn("X,Y,W,H", output.getvalue())

    def test_explicit_bbox_outputs_crop_samples_and_200_percent_image(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            image = Image.new("RGBA", (100, 50), (255, 255, 255, 0))
            ImageDraw.Draw(image).rectangle((20, 10, 59, 29), fill=(255, 0, 0, 255))
            image.save(source)

            report = module.inspect_image_region(
                source,
                root / "out",
                bboxes=[(20, 10, 60, 30)],
                scale=2,
            )

            region = report["regions"][0]
            self.assertEqual("#FF0000FF", region["samples"]["center"])
            self.assertEqual([40, 20], region["crop_size"])
            self.assertEqual("xywh", region["bbox_format"])
            self.assertEqual([20, 10, 40, 20], region["source_bbox"])
            self.assertEqual([20, 10, 60, 30], region["measured_bbox_ltrb"])
            with Image.open(region["magnified_path"]) as magnified:
                self.assertEqual((80, 40), magnified.size)
            self.assertEqual([0.2, 0.2, 0.6, 0.6], region["normalized_bbox"])

    def test_out_of_bounds_bbox_is_rejected(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            Image.new("RGB", (100, 50), "white").save(source)

            with self.assertRaisesRegex(ValueError, "bbox must stay inside source image"):
                module.inspect_image_region(
                    source,
                    root / "out",
                    bboxes=[(90, 40, 110, 60)],
                )

    def test_point_sampling_is_exact(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            image = Image.new("RGB", (10, 10), "white")
            image.putpixel((3, 4), (1, 2, 3))
            image.save(source)

            report = module.inspect_image_region(source, root / "out", points=[(3, 4)])

            self.assertEqual("#010203FF", report["points"][0]["rgba"])


if __name__ == "__main__":
    unittest.main()
