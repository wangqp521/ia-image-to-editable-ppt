from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_font_trials.py"
SPEC = importlib.util.spec_from_file_location("render_font_trials", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load {SCRIPT_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

FONTCONFIG = Path(__file__).resolve().parents[1] / "assets" / "fontconfig-macos.conf"


class FontTrialTests(unittest.TestCase):
    def test_subprocess_runner_has_finite_timeout(self):
        completed = mock.Mock()
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            result = MODULE._run(["tool"])

        self.assertIs(completed, result)
        self.assertIn("timeout", run.call_args.kwargs)
        self.assertEqual(120, run.call_args.kwargs["timeout"])

    def test_measurement_counts_lines_and_detects_clipping(self):
        image = Image.new("RGB", (200, 100), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 100, 28), fill="black")
        draw.rectangle((20, 50, 120, 58), fill="black")

        result = MODULE.measure_rendered_text(image, (10, 10, 150, 80))

        self.assertEqual(2, result["line_count"])
        self.assertEqual([20, 20, 121, 59], result["ink_bbox_px"])
        self.assertFalse(result["clipped"])

    def test_pdffonts_parser_returns_resolved_font(self):
        output = (
            "name type encoding emb sub uni object ID\n"
            "ABCDEE+MicrosoftYaHei TrueType Identity-H yes yes yes 7 0\n"
        )
        self.assertEqual(["ABCDEE+MicrosoftYaHei"], MODULE.parse_pdffonts(output))

    @unittest.skipUnless(
        all(shutil.which(name) for name in ("soffice", "pdftoppm", "pdffonts")),
        "LibreOffice and Poppler required",
    )
    def test_real_trial_produces_traceable_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            report = MODULE.render_font_trials(
                text="字体试排",
                fonts=["PingFang SC"],
                sizes_pt=[14],
                box_in=(2.4, 0.7),
                output_dir=Path(directory),
                fontconfig=FONTCONFIG,
            )

            self.assertEqual(1, len(report["trials"]))
            self.assertTrue(report["trials"][0]["resolved_fonts"])
            self.assertGreaterEqual(report["trials"][0]["line_count"], 1)
            self.assertTrue(Path(report["contact_sheet"]).is_file())
            self.assertTrue((Path(directory) / "font-trials.json").is_file())


if __name__ == "__main__":
    unittest.main()
