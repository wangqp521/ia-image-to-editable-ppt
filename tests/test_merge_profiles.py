import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))

import merge_pptx


class MergeProfileTests(unittest.TestCase):
    def test_merge_profile_constants_are_two_mode_only(self):
        self.assertEqual(merge_pptx.VERIFICATION_PROFILES, {"rapid", "reviewed"})
        self.assertEqual(
            merge_pptx.PROFILE_SUCCESS_STATUSES,
            {"rapid": "rapid_validated", "reviewed": "reviewed_passed"},
        )

    def test_strict_page_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pptx = Path(temp_dir) / "page.pptx"
            pptx.write_bytes(b"placeholder")
            with self.assertRaises(merge_pptx.MergeError) as caught:
                merge_pptx._validate_page_binding(
                    pptx,
                    {
                        "page_id": "page-001",
                        "verification_profile": "strict",
                        "delivery_status": "pending",
                    },
                    {},
                )
            self.assertEqual(
                caught.exception.code,
                "VERIFICATION_PROFILE_INVALID",
            )
            self.assertTrue(pptx.exists())

    def test_reviewed_failed_page_is_not_merged_or_deleted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pptx = Path(temp_dir) / "page.pptx"
            pptx.write_bytes(b"current reviewed output")
            with self.assertRaises(merge_pptx.MergeError) as caught:
                merge_pptx._validate_page_binding(
                    pptx,
                    {
                        "page_id": "page-001",
                        "verification_profile": "reviewed",
                        "delivery_status": "reviewed_failed",
                    },
                    {},
                )
            self.assertEqual(caught.exception.code, "FINAL_REPORT_INVALID")
            self.assertEqual(
                pptx.read_bytes(),
                b"current reviewed output",
            )


if __name__ == "__main__":
    unittest.main()
