from __future__ import annotations

import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixture_specs import make_text_spec


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_pptx_from_spec
from lib.hashing import canonical_json_sha256


VALIDATOR_PATH = SCRIPTS / "validate_pptx.py"
SPEC = importlib.util.spec_from_file_location("validate_pptx_build_report", VALIDATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load validator")
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


class BuildReportValidationTests(unittest.TestCase):
    def test_validator_detects_claim_not_present_in_pptx(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = make_text_spec(root)
            prebuild = {
                "valid": True,
                "stage": "prebuild",
                "spec_sha256": canonical_json_sha256(spec),
            }
            pptx = root / "page.pptx"
            report = build_pptx_from_spec.build_single_page(spec, prebuild, pptx)
            tampered = copy.deepcopy(report)
            tampered["elements"]["title"]["ooxml_names"] = ["ia:not-present"]

            result = VALIDATOR.validate_pptx(
                pptx,
                reconstruction_spec=spec,
                build_report=tampered,
            )

            self.assertIn("PPTX_BUILD_CLAIM_MISMATCH", result["errors"])
            self.assertGreaterEqual(result["build_claims_checked"], 1)

    def test_validator_accepts_current_build_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = make_text_spec(root)
            prebuild = {
                "valid": True,
                "stage": "prebuild",
                "spec_sha256": canonical_json_sha256(spec),
            }
            pptx = root / "page.pptx"
            report = build_pptx_from_spec.build_single_page(spec, prebuild, pptx)

            result = VALIDATOR.validate_pptx(
                pptx,
                reconstruction_spec=spec,
                build_report=report,
            )

            self.assertNotIn("PPTX_BUILD_CLAIM_MISMATCH", result["errors"])
            self.assertEqual(result["build_claims_checked"], 1)


if __name__ == "__main__":
    unittest.main()
