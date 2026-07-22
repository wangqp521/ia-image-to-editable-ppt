from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "create_visual_diff.py"
SPEC = importlib.util.spec_from_file_location("create_visual_diff", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load {SCRIPT_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CreateVisualDiffTests(unittest.TestCase):
    def _save(self, path: Path, color: tuple[int, int, int]) -> None:
        Image.new("RGB", (160, 90), color).save(path)

    def test_identical_images_generate_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.png"
            preview = root / "preview.png"
            output = root / "output"
            self._save(reference, (255, 255, 255))
            self._save(preview, (255, 255, 255))

            report = MODULE.build_visual_diff(reference, preview, output)

            self.assertEqual(1.0, report["full_page"]["similarity"])
            self.assertEqual(0.0, report["full_page"]["changed_pixel_ratio"])
            self.assertTrue((output / "overlay.png").exists())
            self.assertTrue((output / "diff.png").exists())
            self.assertTrue((output / "visual-diff.json").exists())

    def test_tripwire_triggers_below_minimum_similarity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.png"
            preview = root / "preview.png"
            output = root / "output"
            self._save(reference, (255, 255, 255))
            self._save(preview, (0, 0, 0))

            report = MODULE.build_visual_diff(
                reference,
                preview,
                output,
                minimum_similarity=0.95,
            )

            self.assertTrue(report["tripwire"]["triggered"])
            self.assertEqual("below_minimum_similarity", report["tripwire"]["reason"])

    def test_region_evidence_uses_source_bbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.png"
            preview = root / "preview.png"
            output = root / "output"
            self._save(reference, (255, 255, 255))
            self._save(preview, (250, 250, 250))

            report = MODULE.build_visual_diff(
                reference,
                preview,
                output,
                regions=[{"region_id": "header", "source_bbox": [0, 0, 160, 20]}],
            )

            self.assertEqual("header", report["regions"][0]["region_id"])
            self.assertTrue((output / "regions" / "001-header.png").exists())
            self.assertEqual({"requested": 1, "generated": 1, "skipped": 0}, report["region_summary"])

    def test_invalid_region_is_reported_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.png"
            preview = root / "preview.png"
            output = root / "output"
            self._save(reference, (255, 255, 255))
            self._save(preview, (255, 255, 255))
            report = MODULE.build_visual_diff(
                reference,
                preview,
                output,
                regions=[{"region_id": "outside", "source_bbox": [200, 200, 10, 10]}],
            )
            self.assertEqual(1, report["region_summary"]["skipped"])
            self.assertEqual("bbox_out_of_bounds", report["skipped_regions"][0]["reason"])

    def test_preview_is_resized_to_reference_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.png"
            preview = root / "preview.png"
            output = root / "output"
            self._save(reference, (255, 255, 255))
            Image.new("RGB", (320, 180), (255, 255, 255)).save(preview)

            report = MODULE.build_visual_diff(reference, preview, output)

            self.assertEqual([160, 90], report["reference_size"])
            self.assertEqual([320, 180], report["preview_size"])
            self.assertEqual("resized_to_reference", report["alignment"])

    def test_region_evidence_is_200_percent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.png"
            preview = root / "preview.png"
            self._save(reference, (255, 255, 255))
            self._save(preview, (255, 255, 255))

            report = MODULE.build_visual_diff(
                reference,
                preview,
                root / "out",
                regions=[{"region_id": "header", "source_bbox": [0, 0, 80, 20]}],
            )

            with Image.open(report["regions"][0]["evidence"]) as evidence:
                self.assertEqual((80 * 4 + 24, 20 * 2 + 48), evidence.size)

    def test_no_threshold_means_tripwire_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.png"
            preview = root / "preview.png"
            self._save(reference, (255, 255, 255))
            self._save(preview, (255, 255, 255))

            report = MODULE.build_visual_diff(reference, preview, root / "out")

            self.assertFalse(report["tripwire"]["available"])
            self.assertIsNone(report["tripwire"]["triggered"])
            self.assertEqual("no_approved_baseline", report["tripwire"]["reason"])

    def test_evidence_paths_have_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.png"
            preview = root / "preview.png"
            self._save(reference, (255, 255, 255))
            self._save(preview, (255, 255, 255))

            report = MODULE.build_visual_diff(reference, preview, root / "out")

            self.assertRegex(
                report["evidence"]["overlay"]["sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertRegex(
                report["evidence"]["diff"]["sha256"], r"^[0-9a-f]{64}$"
            )

    def test_metrics_include_foreground_and_edge_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.png"
            preview = root / "preview.png"
            self._save(reference, (255, 255, 255))
            self._save(preview, (255, 255, 255))

            report = MODULE.build_visual_diff(reference, preview, root / "out")

            self.assertEqual(1.0, report["full_page"]["foreground_similarity"])
            self.assertEqual(1.0, report["full_page"]["edge_f1"])
            self.assertNotIn("passed", report["full_page"])

    def test_reused_output_removes_stale_region_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.png"
            preview = root / "preview.png"
            output = root / "out"
            stale = output / "regions" / "stale.png"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale")
            self._save(reference, (255, 255, 255))
            self._save(preview, (255, 255, 255))

            MODULE.build_visual_diff(reference, preview, output, regions=[])

            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
