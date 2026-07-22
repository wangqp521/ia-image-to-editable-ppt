from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixture_specs import make_text_spec


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import scaffold_reconstruction as MODULE
from lib.error_codes import ToolError


class ScaffoldReconstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scaffold_fills_only_derived_bboxes(self) -> None:
        spec = make_text_spec(self.root)
        del spec["elements"][0]["slide_bbox"]
        del spec["regions"][0]["slide_bbox"]
        reading_order = copy.deepcopy(spec["reading_order"])

        updated, report = MODULE.scaffold_spec(spec, {"valid": True})

        self.assertEqual(
            updated["elements"][0]["slide_bbox"],
            [228600, 228600, 6096000, 457200],
        )
        self.assertEqual(updated["regions"][0]["slide_bbox"], [0, 0, 12192000, 914400])
        self.assertEqual(updated["reading_order"], reading_order)
        self.assertEqual(
            report["changed"],
            ["elements[0].slide_bbox", "regions[0].slide_bbox"],
        )
        self.assertNotIn("report", updated)

    def test_scaffold_does_not_mutate_input(self) -> None:
        spec = make_text_spec(self.root)
        del spec["elements"][0]["slide_bbox"]
        original = copy.deepcopy(spec)
        MODULE.scaffold_spec(spec, {"valid": True})
        self.assertEqual(spec, original)

    def test_scaffold_does_not_invent_reading_order(self) -> None:
        spec = make_text_spec(self.root)
        del spec["reading_order"]
        with self.assertRaisesRegex(ToolError, "MISSING_REQUIRED_FIELD"):
            MODULE.scaffold_spec(spec, {"valid": True})

    def test_scaffold_rejects_failed_preflight(self) -> None:
        with self.assertRaisesRegex(ToolError, "PREFLIGHT_STALE"):
            MODULE.scaffold_spec(make_text_spec(self.root), {"valid": False})

    def test_scaffold_rejects_conflicting_existing_derived_value(self) -> None:
        spec = make_text_spec(self.root)
        spec["elements"][0]["slide_bbox"][0] += 100
        with self.assertRaisesRegex(ToolError, "SPEC_DERIVED_FIELD_CONFLICT"):
            MODULE.scaffold_spec(spec, {"valid": True})

    def test_cli_writes_updated_spec_and_report(self) -> None:
        spec = make_text_spec(self.root)
        del spec["elements"][0]["slide_bbox"]
        spec_path = self.root / "page-reconstruction.json"
        preflight_path = self.root / "preflight.json"
        output_path = self.root / "updated.json"
        report_path = self.root / "scaffold-report.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        preflight_path.write_text('{"valid": true}', encoding="utf-8")

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "scaffold_reconstruction.py"),
                "--spec",
                str(spec_path),
                "--preflight-report",
                str(preflight_path),
                "--output",
                str(output_path),
                "--report",
                str(report_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(output_path.is_file())
        self.assertTrue(report_path.is_file())
        self.assertEqual(json.loads(completed.stdout)["valid"], True)


if __name__ == "__main__":
    unittest.main()
