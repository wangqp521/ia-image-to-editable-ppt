import json
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))

import validate_reconstruction_spec as spec_validator
from lib import schema_contracts


class ProfileContractTests(unittest.TestCase):
    def test_public_profiles_and_delivery_statuses_are_two_mode_only(self):
        self.assertEqual(
            schema_contracts.VERIFICATION_PROFILES,
            frozenset({"rapid", "reviewed"}),
        )
        self.assertEqual(
            schema_contracts.DELIVERY_STATUSES,
            frozenset(
                {
                    "pending",
                    "rapid_validated",
                    "rapid_validation_failed",
                    "reviewed_passed",
                    "reviewed_failed",
                }
            ),
        )

    def test_strict_profile_is_rejected_by_spec_identity(self):
        errors = []
        spec_validator._validate_verification_identity(
            {"verification_profile": "strict", "delivery_status": "pending"},
            "strict",
            errors,
        )
        self.assertEqual(errors[0]["code"], "SPEC_VERIFICATION_PROFILE_INVALID")
        self.assertEqual(errors[0]["path"], "verification_profile")
        self.assertEqual(
            errors[0]["detail"],
            "verification_profile must be rapid or reviewed",
        )

    def test_tracked_schema_matches_generated_two_mode_schema(self):
        tracked = json.loads(
            (SKILL_ROOT / "schemas/page-reconstruction-v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        generated = schema_contracts.json_schema_document()
        self.assertEqual(tracked, generated)
        enum = tracked["$defs"]["PageReconstruction"]["properties"][
            "verification_profile"
        ]["enum"]
        self.assertEqual(enum, ["rapid", "reviewed"])


if __name__ == "__main__":
    unittest.main()
