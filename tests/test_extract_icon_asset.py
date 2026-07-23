from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "extract_icon_asset.py"


def load_module():
    if not SCRIPT_PATH.is_file():
        raise AssertionError(f"missing script: {SCRIPT_PATH}")
    spec = importlib.util.spec_from_file_location("extract_icon_asset", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExtractIconAssetTests(unittest.TestCase):
    def _paths(self, root: Path) -> tuple[Path, Path]:
        source = root / "source.png"
        output = root / "assets" / "icons" / "icon.png"
        output.parent.mkdir(parents=True)
        return source, output

    def test_closed_ring_keeps_enclosed_background_opaque(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            image = Image.new("RGB", (15, 15), "white")
            ImageDraw.Draw(image).rectangle((4, 4, 10, 10), outline="black", width=1)
            image.save(source)

            result = module.extract_icon_asset(
                source,
                output,
                (0, 0, 15, 15),
                icon_id="closed-ring",
            )

            with Image.open(output) as asset:
                self.assertEqual(asset.mode, "RGBA")
                self.assertEqual(asset.getpixel((0, 0))[3], 0)
                self.assertEqual(asset.getpixel((7, 7)), (255, 255, 255, 255))
                self.assertEqual(asset.getpixel((4, 4)), (0, 0, 0, 255))
            self.assertEqual(result["bbox_format"], "xywh")
            self.assertEqual(result["source_bbox"], [0, 0, 15, 15])
            self.assertEqual(result["crop_mode"], "alpha_isolation")
            self.assertTrue(result["rgb_preserved"])

    def test_open_outline_makes_connected_internal_background_transparent(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            image = Image.new("RGB", (15, 15), "white")
            draw = ImageDraw.Draw(image)
            draw.line((4, 4, 4, 10), fill="black", width=1)
            draw.line((4, 10, 10, 10), fill="black", width=1)
            draw.line((10, 10, 10, 4), fill="black", width=1)
            image.save(source)

            module.extract_icon_asset(
                source,
                output,
                (0, 0, 15, 15),
                icon_id="open-u",
            )

            with Image.open(output) as asset:
                self.assertEqual(asset.getpixel((7, 7)), (255, 255, 255, 0))
                self.assertEqual(asset.getpixel((4, 7)), (0, 0, 0, 255))

    def test_alpha_isolation_preserves_every_source_rgb_pixel_exactly(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            image = Image.new("RGB", (12, 10), (245, 244, 242))
            draw = ImageDraw.Draw(image)
            draw.rectangle((3, 2, 8, 7), fill=(17, 81, 193))
            draw.point((9, 6), fill=(221, 44, 71))
            image.save(source)

            module.extract_icon_asset(
                source,
                output,
                (1, 1, 10, 8),
                icon_id="rgb-exact",
            )

            with Image.open(source) as source_image, Image.open(output) as asset:
                expected = source_image.convert("RGB").crop((1, 1, 11, 9))
                self.assertEqual(asset.convert("RGB").tobytes(), expected.tobytes())

    def test_detached_tiny_foreground_component_is_retained(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            image = Image.new("RGB", (14, 14), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((4, 4, 8, 8), fill="black")
            draw.point((10, 9), fill=(255, 0, 0))
            image.save(source)

            module.extract_icon_asset(
                source,
                output,
                (0, 0, 14, 14),
                icon_id="detached-dot",
            )

            with Image.open(output) as asset:
                self.assertEqual(asset.getpixel((10, 9)), (255, 0, 0, 255))

    def test_rejects_invalid_bbox_and_wrong_output_location(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            Image.new("RGB", (10, 10), "white").save(source)

            with self.assertRaisesRegex(ValueError, "bbox"):
                module.extract_icon_asset(
                    source, output, (8, 8, 3, 3), icon_id="bad"
                )
            with self.assertRaisesRegex(ValueError, "assets/icons"):
                module.extract_icon_asset(
                    source,
                    root / "icon.png",
                    (0, 0, 10, 10),
                    icon_id="bad",
                )

    def test_rejects_alpha_asset_when_foreground_touches_crop_edge(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            image = Image.new("RGB", (10, 10), "white")
            ImageDraw.Draw(image).rectangle((0, 3, 5, 6), fill="black")
            image.save(source)

            with self.assertRaisesRegex(ValueError, "left"):
                module.extract_icon_asset(
                    source,
                    output,
                    (0, 0, 10, 10),
                    icon_id="touching",
                )

    def test_rejects_alpha_asset_without_visible_foreground(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            Image.new("RGB", (10, 10), "white").save(source)

            with self.assertRaisesRegex(ValueError, "visible foreground"):
                module.extract_icon_asset(
                    source,
                    output,
                    (0, 0, 10, 10),
                    icon_id="empty",
                )

    def test_cli_accepts_xywh_and_returns_json_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            image = Image.new("RGB", (12, 12), "white")
            ImageDraw.Draw(image).rectangle((3, 3, 8, 8), fill="black")
            image.save(source)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    str(source),
                    "--icon-id",
                    "cli-icon",
                    "--bbox-xywh",
                    "0,0,12,12",
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["icon_id"], "cli-icon")
            self.assertEqual(result["size"], [12, 12])
            self.assertEqual(len(result["asset_sha256"]), 64)
            self.assertEqual(len(result["alpha_mask_sha256"]), 64)

    def test_cli_rejects_removed_crop_mode_option(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            Image.new("RGB", (12, 12), "white").save(source)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    str(source),
                    "--icon-id",
                    "legacy-icon",
                    "--bbox-xywh",
                    "0,0,12,12",
                    "--crop-mode",
                    "background_preserved",
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unrecognized arguments: --crop-mode", completed.stderr)


if __name__ == "__main__":
    unittest.main()
