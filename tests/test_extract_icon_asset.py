from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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
                crop_mode="alpha_isolation",
            )

            with Image.open(output) as asset:
                self.assertEqual(asset.mode, "RGBA")
                self.assertEqual(asset.getpixel((0, 0))[3], 0)
                self.assertEqual(asset.getpixel((7, 7)), (255, 255, 255, 255))
                self.assertEqual(asset.getpixel((4, 4)), (0, 0, 0, 255))
            self.assertEqual(result["bbox_format"], "xywh")
            self.assertEqual(result["source_bbox"], [0, 0, 15, 15])
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
                crop_mode="alpha_isolation",
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
                crop_mode="alpha_isolation",
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
                crop_mode="alpha_isolation",
            )

            with Image.open(output) as asset:
                self.assertEqual(asset.getpixel((10, 9)), (255, 0, 0, 255))

    def test_background_preserved_is_an_exact_rgb_crop(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            image = Image.new("RGB", (11, 9), (239, 235, 227))
            ImageDraw.Draw(image).ellipse((3, 2, 8, 7), fill=(33, 91, 155))
            image.save(source)

            result = module.extract_icon_asset(
                source,
                output,
                (2, 1, 8, 7),
                icon_id="preserved",
                crop_mode="background_preserved",
            )

            with Image.open(output) as asset:
                self.assertEqual(asset.mode, "RGB")
                self.assertEqual(asset.tobytes(), image.crop((2, 1, 10, 8)).tobytes())
            self.assertIsNone(result["alpha_mask_sha256"])
            self.assertTrue(result["rgb_preserved"])

    def test_rejects_invalid_bbox_unknown_mode_and_wrong_output_location(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            Image.new("RGB", (10, 10), "white").save(source)

            with self.assertRaisesRegex(ValueError, "bbox"):
                module.extract_icon_asset(
                    source, output, (8, 8, 3, 3), icon_id="bad", crop_mode="alpha_isolation"
                )
            with self.assertRaisesRegex(ValueError, "crop_mode"):
                module.extract_icon_asset(
                    source, output, (0, 0, 10, 10), icon_id="bad", crop_mode="tight_rect"
                )
            with self.assertRaisesRegex(ValueError, "assets/icons"):
                module.extract_icon_asset(
                    source,
                    root / "icon.png",
                    (0, 0, 10, 10),
                    icon_id="bad",
                    crop_mode="background_preserved",
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
                    crop_mode="alpha_isolation",
                )

    def test_rejects_low_contrast_foreground_erased_before_edge_check(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = self._paths(root)
            image = Image.new("RGB", (12, 12), "white")
            draw = ImageDraw.Draw(image)
            draw.line((0, 6, 5, 6), fill=(242, 242, 242), width=1)
            draw.rectangle((5, 4, 9, 8), fill=(30, 30, 30))
            image.save(source)

            with self.assertRaisesRegex(
                ValueError,
                "raw foreground may touch crop edge: left",
            ):
                module.extract_icon_asset(
                    source,
                    output,
                    (0, 0, 12, 12),
                    icon_id="low-contrast-edge",
                    crop_mode="alpha_isolation",
                )

    def test_edge_models_remain_side_specific(self) -> None:
        module = load_module()
        crop = Image.new("RGBA", (12, 12), (255, 255, 255, 255))
        ImageDraw.Draw(crop).rectangle((0, 0, 2, 2), fill=(255, 0, 0, 255))

        models = module._edge_models(crop)

        self.assertIn((255, 0, 0), models["top"])
        self.assertNotIn((255, 0, 0), models["bottom"])

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
                    crop_mode="alpha_isolation",
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
                    "--crop-mode",
                    "alpha_isolation",
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

    def test_batch_extracts_in_spec_order_and_opens_source_once(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            image = Image.new("RGB", (40, 20), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((4, 4, 10, 10), fill="black")
            draw.rectangle((24, 4, 30, 10), fill="blue")
            image.save(source)
            output_dir = root / "assets" / "icons"
            output_dir.mkdir(parents=True)
            spec_path = root / "work" / "page-reconstruction.json"
            spec_path.parent.mkdir()
            icons = [
                {
                    "icon_id": "first",
                    "source_path": str(source),
                    "source_bbox": [0, 0, 16, 16],
                    "crop_mode": "alpha_isolation",
                    "asset_path": str(output_dir / "first.png"),
                },
                {
                    "icon_id": "second",
                    "source_path": str(source),
                    "source_bbox": [20, 0, 16, 16],
                    "crop_mode": "alpha_isolation",
                    "asset_path": str(output_dir / "second.png"),
                },
            ]
            spec_path.write_text(
                json.dumps({"modules": {"icons": {"icons": icons}}}),
                encoding="utf-8",
            )
            original_open = module.Image.open
            source_opens = 0

            def counted_open(path, *args, **kwargs):
                nonlocal source_opens
                if Path(path).resolve() == source.resolve():
                    source_opens += 1
                return original_open(path, *args, **kwargs)

            with mock.patch.object(module.Image, "open", side_effect=counted_open):
                result = module.extract_icon_assets_from_spec(spec_path, output_dir)

            self.assertTrue(result["ok"])
            self.assertEqual(
                [item["icon_id"] for item in result["results"]],
                ["first", "second"],
            )
            self.assertEqual(source_opens, 1)

    def test_batch_keeps_successes_and_does_not_overwrite_failed_asset(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            image = Image.new("RGB", (40, 20), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((4, 4, 10, 10), fill="black")
            draw.rectangle((20, 4, 26, 10), fill="blue")
            image.save(source)
            output_dir = root / "assets" / "icons"
            output_dir.mkdir(parents=True)
            first_path = output_dir / "first.png"
            second_path = output_dir / "second.png"
            second_path.write_bytes(b"sentinel")
            spec_path = root / "work" / "page-reconstruction.json"
            spec_path.parent.mkdir()
            spec_path.write_text(
                json.dumps(
                    {
                        "modules": {
                            "icons": {
                                "icons": [
                                    {
                                        "icon_id": "first",
                                        "source_path": str(source),
                                        "source_bbox": [0, 0, 16, 16],
                                        "crop_mode": "alpha_isolation",
                                        "asset_path": str(first_path),
                                    },
                                    {
                                        "icon_id": "second",
                                        "source_path": str(source),
                                        "source_bbox": [20, 0, 16, 16],
                                        "crop_mode": "alpha_isolation",
                                        "asset_path": str(second_path),
                                    },
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = module.extract_icon_assets_from_spec(spec_path, output_dir)

            self.assertFalse(result["ok"])
            self.assertTrue(first_path.is_file())
            self.assertEqual(second_path.read_bytes(), b"sentinel")
            self.assertEqual(result["failures"][0]["icon_id"], "second")


if __name__ == "__main__":
    unittest.main()
