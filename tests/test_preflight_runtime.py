from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "preflight_runtime.py"
SKILL_PATH = SKILL_ROOT / "SKILL.md"


def load_module():
    module_spec = importlib.util.spec_from_file_location(
        "image_to_editable_ppt_preflight_runtime", SCRIPT_PATH
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


class PreflightRuntimeDefaultsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_poppler_arguments_default_to_path_command_names(self) -> None:
        args = self.module._parse_args(
            [
                "--soffice",
                "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                "--fontconfig",
                str(SKILL_ROOT / "assets" / "fontconfig-macos.conf"),
                "--output",
                "/tmp/runtime-preflight.json",
            ]
        )
        self.assertEqual(args.pdftoppm, "pdftoppm")
        self.assertEqual(args.pdffonts, "pdffonts")
        self.assertEqual(args.pdftotext, "pdftotext")

    def test_explicit_missing_absolute_path_still_fails_closed(self) -> None:
        missing = "/definitely-missing-codex-runtime/pdftoppm"
        self.assertIsNone(self.module._resolve_executable(missing))

    def test_skill_example_does_not_hardcode_intel_homebrew_poppler(self) -> None:
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn("--pdftoppm pdftoppm", skill_text)
        self.assertIn("--pdffonts pdffonts", skill_text)
        self.assertIn("--pdftotext pdftotext", skill_text)
        self.assertNotIn("/usr/local/bin/pdftoppm", skill_text)


if __name__ == "__main__":
    unittest.main()
