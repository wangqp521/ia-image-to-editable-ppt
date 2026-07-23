from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "create_icon_crop_review.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CreateIconCropReviewTest(unittest.TestCase):
    def _ensure_batch_extraction(self, spec_path: Path) -> None:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        module = spec.get("modules", {}).get("icons")
        if not isinstance(module, dict) or "batch_extraction" in module:
            return
        icons = module.get("icons")
        if not isinstance(icons, list) or not icons:
            return
        source_path = Path(icons[0]["source_path"])
        module["batch_extraction"] = {
            "processor": "extract_icon_asset.py",
            "algorithm_version": "edge-connected-v2",
            "processor_sha256": "1" * 64,
            "source_path": str(source_path),
            "source_sha256": sha256(source_path),
            "icon_count": len(icons),
            "result": "passed",
        }
        spec_path.write_text(json.dumps(spec), encoding="utf-8")

    def _run(self, spec_path: Path, output: Path) -> subprocess.CompletedProcess[str]:
        self._ensure_batch_extraction(spec_path)
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), str(spec_path), "--output", str(output)],
            check=False,
            capture_output=True,
            text=True,
        )

    def _write_alpha_case(self, root: Path) -> tuple[Path, Path, Path]:
        icons_dir = root / "assets" / "icons"
        icons_dir.mkdir(parents=True)
        source_path = root / "source.png"
        source = Image.new("RGB", (12, 12), "white")
        ImageDraw.Draw(source).rectangle((4, 4, 7, 7), fill=(255, 0, 0))
        source.save(source_path)

        asset_path = icons_dir / "first.png"
        asset = source.crop((2, 2, 10, 10)).convert("RGBA")
        alpha = Image.new("L", asset.size, 0)
        ImageDraw.Draw(alpha).rectangle((2, 2, 5, 5), fill=255)
        asset.putalpha(alpha)
        asset.save(asset_path)

        spec_path = root / "page-reconstruction.json"
        spec = {
            "modules": {
                "typography": {"items": [{"text": "第一版"}]},
                "icons": {
                    "icons": [
                        {
                            "icon_id": "first",
                            "crop_mode": "alpha_isolation",
                            "source_path": str(source_path),
                            "source_bbox": [2, 2, 8, 8],
                            "padding": 0,
                            "asset_path": str(asset_path),
                            "asset_sha256": sha256(asset_path),
                            "background_handling": "transparent",
                            "fallback_reason": None,
                            "alpha_mask_sha256": sha256(asset_path),
                        }
                    ]
                },
            }
        }
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        return spec_path, source_path, asset_path

    def test_alpha_review_shows_source_and_asset_and_records_evidence_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icons_dir = root / "assets" / "icons"
            icons_dir.mkdir(parents=True)

            source = Image.new("RGB", (10, 10), "white")
            ImageDraw.Draw(source).rectangle((3, 3, 6, 6), fill=(255, 0, 0))
            source_path = root / "source.png"
            source.save(source_path)

            asset = source.convert("RGBA")
            alpha = Image.new("L", asset.size, 0)
            ImageDraw.Draw(alpha).rectangle((3, 3, 6, 6), fill=255)
            asset.putalpha(alpha)
            asset_path = icons_dir / "first.png"
            asset.save(asset_path)

            spec_path = root / "page-reconstruction.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "modules": {
                            "icons": {
                                "icons": [
                                    {
                                        "crop_mode": "alpha_isolation",
                                        "source_path": str(source_path),
                                        "source_bbox": [0, 0, 10, 10],
                                        "padding": 0,
                                        "asset_path": str(asset_path),
                                        "asset_sha256": sha256(asset_path),
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = root / "comparisons" / "icon-crop-review.png"
            completed = self._run(spec_path, output)

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["spec_sha256"], sha256(spec_path))
            self.assertEqual(result["output_sha256"], sha256(output))
            self.assertIn("reused", result)
            self.assertIn("icon_manifest_sha256", result)
            self.assertFalse(result["reused"])
            self.assertEqual(len(result["icon_manifest_sha256"]), 64)
            self.assertEqual(
                result["assets"],
                [{"path": str(asset_path.resolve()), "sha256": sha256(asset_path)}],
            )
            self.assertEqual(result["background"], "#00FF00")
            self.assertEqual(result["scale"], 4)
            with Image.open(output) as review:
                self.assertEqual(
                    review.info["icon_manifest_sha256"],
                    result["icon_manifest_sha256"],
                )
                colors = {color for _, color in review.getcolors(review.width * review.height) or []}
            self.assertIn((255, 255, 255), colors, "source crop panel must be present")
            self.assertIn((255, 0, 0), colors)
            self.assertIn((0, 255, 0), colors, "final asset panel must use green screen")

    def test_review_includes_labeled_roi_context_before_source_and_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icons_dir = root / "assets" / "icons"
            icons_dir.mkdir(parents=True)

            source = Image.new("RGB", (24, 24), (20, 40, 180))
            source.paste((255, 255, 255), (7, 7, 17, 17))
            ImageDraw.Draw(source).rectangle((10, 10, 13, 13), fill=(255, 0, 0))
            source_path = root / "source.png"
            source.save(source_path)

            crop = source.crop((7, 7, 17, 17)).convert("RGBA")
            alpha = Image.new("L", crop.size, 0)
            ImageDraw.Draw(alpha).rectangle((3, 3, 6, 6), fill=255)
            crop.putalpha(alpha)
            asset_path = icons_dir / "context-icon.png"
            crop.save(asset_path)

            spec_path = root / "page-reconstruction.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "modules": {
                            "icons": {
                                "icons": [
                                    {
                                        "icon_id": "context-icon",
                                        "crop_mode": "alpha_isolation",
                                        "source_path": str(source_path),
                                        "source_bbox": [7, 7, 10, 10],
                                        "padding": 0,
                                        "asset_path": str(asset_path),
                                        "asset_sha256": sha256(asset_path),
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            completed = self._run(spec_path, root / "review.png")

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(
                result["panels"],
                ["context_with_bbox", "source_crop", "asset_on_green"],
            )
            self.assertEqual(result["icon_ids"], ["context-icon"])
            with Image.open(root / "review.png") as review:
                colors = {
                    color
                    for _, color in review.getcolors(review.width * review.height) or []
                }
            self.assertIn((20, 40, 180), colors, "context must show pixels outside the ROI")
            self.assertIn((255, 0, 255), colors, "context must draw the ROI bbox")

    def test_context_is_true_400_percent_and_labels_bind_crop_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path, _, _ = self._write_alpha_case(root)
            output = root / "review.png"

            completed = self._run(spec_path, output)

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["context_scale"], 4)
            self.assertEqual(
                result["labels"],
                [
                    {
                        "icon_id": "first",
                        "crop_mode": "alpha_isolation",
                        "source_bbox": [2, 2, 8, 8],
                    }
                ],
            )

    def test_background_preserved_also_shows_asset_on_green_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icons_dir = root / "assets" / "icons"
            icons_dir.mkdir(parents=True)
            source = Image.new("RGB", (12, 10), (240, 238, 230))
            source.paste((50, 90, 160), (3, 2, 9, 8))
            source_path = root / "source.png"
            source.save(source_path)
            asset_path = icons_dir / "preserved.png"
            source.save(asset_path)
            spec_path = root / "page-reconstruction.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "modules": {
                            "icons": {
                                "icons": [
                                    {
                                        "crop_mode": "background_preserved",
                                        "source_path": str(source_path),
                                        "source_bbox": [0, 0, 12, 10],
                                        "padding": 0,
                                        "asset_path": str(asset_path),
                                        "asset_sha256": sha256(asset_path),
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = root / "review.png"
            completed = self._run(spec_path, output)

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["background_preserved_icon_count"], 1)
            self.assertEqual(result["background"], "#00FF00")
            with Image.open(output) as review:
                colors = {color for _, color in review.getcolors(review.width * review.height) or []}
            self.assertIn((0, 255, 0), colors)
            self.assertIn((240, 238, 230), colors)
            self.assertIn((50, 90, 160), colors)

    def test_mixed_mode_contact_sheet_never_uses_old_gray_background(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icons_dir = root / "assets" / "icons"
            icons_dir.mkdir(parents=True)
            source_path = root / "source.png"
            Image.new("RGB", (8, 8), "white").save(source_path)
            alpha_path = icons_dir / "alpha.png"
            alpha = Image.new("RGBA", (8, 8), (255, 255, 255, 0))
            alpha.paste((0, 0, 255, 255), (2, 2, 6, 6))
            alpha.save(alpha_path)
            preserved_path = icons_dir / "preserved.png"
            Image.new("RGB", (8, 8), (255, 192, 0)).save(preserved_path)
            icons = [
                {
                    "crop_mode": "alpha_isolation",
                    "source_path": str(source_path),
                    "source_bbox": [0, 0, 8, 8],
                    "padding": 0,
                    "asset_path": str(alpha_path),
                    "asset_sha256": sha256(alpha_path),
                },
                {
                    "crop_mode": "background_preserved",
                    "source_path": str(source_path),
                    "source_bbox": [0, 0, 8, 8],
                    "padding": 0,
                    "asset_path": str(preserved_path),
                    "asset_sha256": sha256(preserved_path),
                },
            ]
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps({"modules": {"icons": {"icons": icons}}}), encoding="utf-8")
            output = root / "review.png"
            completed = self._run(spec_path, output)

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            with Image.open(output) as review:
                colors = {color for _, color in review.getcolors(review.width * review.height) or []}
            self.assertIn((0, 255, 0), colors)
            self.assertNotIn((224, 224, 224), colors)

    def test_rejects_asset_when_declared_hash_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icons_dir = root / "assets" / "icons"
            icons_dir.mkdir(parents=True)
            source_path = root / "source.png"
            asset_path = icons_dir / "asset.png"
            Image.new("RGB", (4, 4), "white").save(source_path)
            Image.new("RGB", (4, 4), "white").save(asset_path)
            spec_path = root / "spec.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "modules": {
                            "icons": {
                                "icons": [
                                    {
                                        "crop_mode": "background_preserved",
                                        "source_path": str(source_path),
                                        "source_bbox": [0, 0, 4, 4],
                                        "padding": 0,
                                        "asset_path": str(asset_path),
                                        "asset_sha256": "0" * 64,
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            completed = self._run(spec_path, root / "review.png")

            self.assertEqual(completed.returncode, 1)
            self.assertIn("asset_sha256 mismatch", completed.stdout)

    def test_background_preserved_rejects_partially_transparent_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icons_dir = root / "assets" / "icons"
            icons_dir.mkdir(parents=True)
            asset = Image.new("RGBA", (4, 4), (255, 255, 255, 255))
            asset.putpixel((0, 0), (255, 255, 255, 128))
            asset_path = icons_dir / "asset.png"
            asset.save(asset_path)
            source_path = root / "source.png"
            Image.new("RGB", (4, 4), "white").save(source_path)
            spec_path = root / "spec.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "modules": {
                            "icons": {
                                "icons": [
                                    {
                                        "crop_mode": "background_preserved",
                                        "source_path": str(source_path),
                                        "source_bbox": [0, 0, 4, 4],
                                        "padding": 0,
                                        "asset_path": str(asset_path),
                                        "asset_sha256": sha256(asset_path),
                                    }
                                ]
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            completed = self._run(spec_path, root / "review.png")

            self.assertEqual(completed.returncode, 1)
            self.assertIn("must be RGB or fully opaque RGBA", completed.stdout)

    def test_text_only_spec_change_reuses_existing_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path, _, _ = self._write_alpha_case(root)
            output = root / "review.png"

            first = self._run(spec_path, output)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            first_result = json.loads(first.stdout)
            self.assertIn("reused", first_result)
            self.assertIn("icon_manifest_sha256", first_result)
            sentinel_ns = 1_700_000_000_000_000_000
            os.utime(output, ns=(sentinel_ns, sentinel_ns))

            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["modules"]["typography"]["items"][0]["text"] = "第二版"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            second = self._run(spec_path, output)

            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            second_result = json.loads(second.stdout)
            self.assertIn("reused", second_result)
            self.assertIn("icon_manifest_sha256", second_result)
            self.assertFalse(first_result["reused"])
            self.assertTrue(second_result["reused"])
            self.assertEqual(
                first_result["icon_manifest_sha256"],
                second_result["icon_manifest_sha256"],
            )
            self.assertNotEqual(first_result["spec_sha256"], second_result["spec_sha256"])
            self.assertEqual(output.stat().st_mtime_ns, sentinel_ns)

    def test_source_bytes_change_invalidates_existing_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path, source_path, _ = self._write_alpha_case(root)
            output = root / "review.png"
            first = self._run(spec_path, output)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            first_result = json.loads(first.stdout)
            self.assertIn("icon_manifest_sha256", first_result)

            with Image.open(source_path) as source:
                changed = source.convert("RGB")
            changed.putpixel((0, 0), (0, 0, 0))
            changed.save(source_path)
            second = self._run(spec_path, output)

            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            second_result = json.loads(second.stdout)
            self.assertIn("reused", second_result)
            self.assertIn("icon_manifest_sha256", second_result)
            self.assertFalse(second_result["reused"])
            self.assertNotEqual(
                first_result["icon_manifest_sha256"],
                second_result["icon_manifest_sha256"],
            )

    def test_processor_hash_change_invalidates_existing_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path, _, _ = self._write_alpha_case(root)
            output = root / "review.png"
            first = self._run(spec_path, output)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            first_result = json.loads(first.stdout)

            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["modules"]["icons"]["batch_extraction"]["processor_sha256"] = "2" * 64
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            second = self._run(spec_path, output)

            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            second_result = json.loads(second.stdout)
            self.assertFalse(second_result["reused"])
            self.assertNotEqual(
                first_result["icon_manifest_sha256"],
                second_result["icon_manifest_sha256"],
            )

    def test_icon_bbox_change_invalidates_existing_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path, _, asset_path = self._write_alpha_case(root)
            output = root / "review.png"
            first = self._run(spec_path, output)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            first_result = json.loads(first.stdout)
            self.assertIn("icon_manifest_sha256", first_result)

            with Image.open(asset_path) as asset:
                resized = asset.resize((7, 8))
            resized.save(asset_path)
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            icon = spec["modules"]["icons"]["icons"][0]
            icon["source_bbox"] = [3, 2, 7, 8]
            icon["asset_sha256"] = sha256(asset_path)
            icon["alpha_mask_sha256"] = sha256(asset_path)
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            second = self._run(spec_path, output)

            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            second_result = json.loads(second.stdout)
            self.assertIn("reused", second_result)
            self.assertIn("icon_manifest_sha256", second_result)
            self.assertFalse(second_result["reused"])
            self.assertNotEqual(
                first_result["icon_manifest_sha256"],
                second_result["icon_manifest_sha256"],
            )

    def test_legacy_png_without_manifest_is_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path, _, _ = self._write_alpha_case(root)
            output = root / "review.png"
            Image.new("RGB", (3, 3), "green").save(output)

            completed = self._run(spec_path, output)

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertIn("reused", result)
            self.assertIn("icon_manifest_sha256", result)
            self.assertFalse(result["reused"])
            with Image.open(output) as review:
                self.assertEqual(
                    review.info["icon_manifest_sha256"],
                    result["icon_manifest_sha256"],
                )

    def test_cache_hit_still_rejects_changed_asset_with_stale_declared_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path, _, asset_path = self._write_alpha_case(root)
            output = root / "review.png"
            first = self._run(spec_path, output)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

            with Image.open(asset_path) as asset:
                changed = asset.convert("RGBA")
            changed.putpixel((4, 4), (0, 0, 255, 255))
            changed.save(asset_path)
            second = self._run(spec_path, output)

            self.assertEqual(second.returncode, 1)
            self.assertIn("asset_sha256 mismatch", second.stdout)


if __name__ == "__main__":
    unittest.main()
