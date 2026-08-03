import contextlib
import inspect
import io
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))

import create_reviewer_prompt
import create_visual_diff
from lib import final_identity, reviewer_contracts


class ReviewToolProfileTests(unittest.TestCase):
    def _artifacts(self):
        return {
            name: {"path": f"/tmp/{name}.json", "sha256": "0" * 64}
            for name in reviewer_contracts.REVIEW_CONTEXT_ARTIFACT_FIELDS
        }

    def test_review_context_accepts_only_reviewed(self):
        context = reviewer_contracts.build_review_context(
            page_id="page-001",
            review_round=1,
            verification_profile="reviewed",
            content_spec_sha256="1" * 64,
            artifacts=self._artifacts(),
            region_evidence=[],
        )
        self.assertEqual(context["verification_profile"], "reviewed")
        with self.assertRaisesRegex(
            ValueError, "verification_profile must be reviewed"
        ):
            reviewer_contracts.build_review_context(
                page_id="page-001",
                review_round=1,
                verification_profile="strict",
                content_spec_sha256="1" * 64,
                artifacts=self._artifacts(),
                region_evidence=[],
            )

    def test_final_identity_rejects_strict_before_collecting_artifacts(self):
        artifacts, errors = final_identity.collect_current_artifacts(
            {"verification_profile": "strict"}
        )
        self.assertIsNone(artifacts)
        self.assertEqual(errors[0]["path"], "verification_profile")
        self.assertIn("rapid or reviewed", errors[0]["detail"])

    def test_visual_diff_defaults_to_rapid_and_rejects_strict_cli(self):
        self.assertEqual(
            inspect.signature(create_visual_diff.build_visual_diff)
            .parameters["profile"]
            .default,
            "rapid",
        )
        self.assertEqual(
            inspect.signature(create_visual_diff.build_visual_diff_from_render_report)
            .parameters["profile"]
            .default,
            "rapid",
        )
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                create_visual_diff._parse_args(
                    [
                        "source.png",
                        "--render-report",
                        "render-report.json",
                        "--output-dir",
                        "visual-diff",
                        "--profile",
                        "strict",
                    ]
                )

    def test_prompt_output_option_is_removed(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                create_reviewer_prompt._parse_args(
                    [
                        "page-reconstruction.json",
                        "--review-round",
                        "1",
                        "--prompt-output",
                        "prompt.txt",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
