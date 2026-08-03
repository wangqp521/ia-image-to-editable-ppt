from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from PIL import Image, PngImagePlugin


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from lib.error_codes import ToolError
import lib.repair_budget as repair_budget_module
from lib.repair_budget import enforce_repair_budget
from lib.spec_identity import content_spec_sha256
import validate_reconstruction_spec as validator


def make_spec(*, marker: str = "candidate-1", profile: str = "rapid") -> dict:
    return {
        "schema_version": 2,
        "page_id": "page-001",
        "verification_profile": profile,
        "content_reference": {
            "path": "/tmp/source.png",
            "sha256": "a" * 64,
        },
        "modules": {"high_risk": {}, "test_marker": marker},
    }


def authorize(spec: dict, *, source_hash: str, trigger: str = "rapid_review") -> None:
    target_hash = content_spec_sha256(spec)
    spec["modules"]["high_risk"]["repair_budget"] = {
        "schema_version": 1,
        "max_content_versions": 2,
        "repair_batches": [
            {
                "batch_index": 1,
                "source_content_spec_sha256": source_hash,
                "target_content_spec_sha256": target_hash,
                "trigger": trigger,
                "issue_ids": ["title-overflow"],
                "status": "consumed",
            }
        ],
    }


class RepairBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state_path = Path(self.tempdir.name) / "repair-budget.json"

    def test_initial_prebuild_registers_candidate_one(self) -> None:
        spec = make_spec()
        state = enforce_repair_budget(spec, self.state_path, stage="prebuild")
        self.assertEqual(state["candidates"], [content_spec_sha256(spec)])
        self.assertIsNone(state["repair_event"])
        self.assertTrue(self.state_path.is_file())

    def test_same_content_hash_is_idempotent(self) -> None:
        spec = make_spec()
        first = enforce_repair_budget(spec, self.state_path, stage="prebuild")
        before = self.state_path.read_bytes()
        second = enforce_repair_budget(spec, self.state_path, stage="prebuild")
        self.assertEqual(second, first)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_second_hash_requires_complete_authorization(self) -> None:
        first = make_spec()
        enforce_repair_budget(first, self.state_path, stage="prebuild")
        with self.assertRaises(ToolError) as raised:
            enforce_repair_budget(
                make_spec(marker="candidate-2"), self.state_path, stage="prebuild"
            )
        self.assertEqual(raised.exception.code, "REPAIR_BUDGET_AUTHORIZATION_REQUIRED")

    def test_authorized_second_hash_consumes_budget(self) -> None:
        first = make_spec()
        first_hash = content_spec_sha256(first)
        enforce_repair_budget(first, self.state_path, stage="prebuild")
        second = make_spec(marker="candidate-2")
        authorize(second, source_hash=first_hash)
        state = enforce_repair_budget(second, self.state_path, stage="prebuild")
        self.assertEqual(state["candidates"], [first_hash, content_spec_sha256(second)])
        self.assertEqual(state["repair_event"]["trigger"], "rapid_review")

    def test_registered_candidate_two_still_requires_matching_authorization(self) -> None:
        first = make_spec()
        first_hash = content_spec_sha256(first)
        enforce_repair_budget(first, self.state_path, stage="prebuild")
        second = make_spec(marker="candidate-2")
        authorize(second, source_hash=first_hash)
        enforce_repair_budget(second, self.state_path, stage="prebuild")
        second["modules"]["high_risk"].pop("repair_budget")

        with self.assertRaises(ToolError) as raised:
            enforce_repair_budget(second, self.state_path, stage="final")

        self.assertEqual(raised.exception.code, "REPAIR_BUDGET_AUTHORIZATION_REQUIRED")

    def test_concurrent_second_candidates_cannot_both_consume_budget(self) -> None:
        first = make_spec()
        first_hash = content_spec_sha256(first)
        enforce_repair_budget(first, self.state_path, stage="prebuild")
        candidates = [
            make_spec(marker="candidate-2-a"),
            make_spec(marker="candidate-2-b"),
        ]
        for candidate in candidates:
            authorize(candidate, source_hash=first_hash)
        original_write = repair_budget_module.atomic_write_json

        def slow_write(path: Path, payload: dict) -> None:
            if len(payload.get("candidates", [])) == 2:
                time.sleep(0.05)
            original_write(path, payload)

        def register(candidate: dict) -> str:
            try:
                enforce_repair_budget(candidate, self.state_path, stage="prebuild")
            except ToolError as exc:
                return exc.code
            return "accepted"

        with mock.patch.object(
            repair_budget_module, "atomic_write_json", side_effect=slow_write
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(register, candidates))

        self.assertEqual(sorted(results), ["REPAIR_BUDGET_EXHAUSTED", "accepted"])

    def test_authorization_requires_json_integers_not_bool_or_float(self) -> None:
        first = make_spec()
        first_hash = content_spec_sha256(first)
        enforce_repair_budget(first, self.state_path, stage="prebuild")
        second = make_spec(marker="candidate-2")
        authorize(second, source_hash=first_hash)
        second["modules"]["high_risk"]["repair_budget"]["schema_version"] = True
        second["modules"]["high_risk"]["repair_budget"]["max_content_versions"] = 2.0

        with self.assertRaises(ToolError) as raised:
            enforce_repair_budget(second, self.state_path, stage="prebuild")

        self.assertEqual(raised.exception.code, "REPAIR_BUDGET_AUTHORIZATION_INVALID")

    def test_uppercase_source_sha_is_accepted_and_normalized(self) -> None:
        spec = make_spec()
        spec["content_reference"]["sha256"] = "A" * 64

        state = enforce_repair_budget(spec, self.state_path, stage="prebuild")

        self.assertEqual(state["source_sha256"], "a" * 64)

    def test_third_hash_is_rejected_after_budget_is_consumed(self) -> None:
        first = make_spec()
        first_hash = content_spec_sha256(first)
        enforce_repair_budget(first, self.state_path, stage="prebuild")
        second = make_spec(marker="candidate-2")
        authorize(second, source_hash=first_hash)
        enforce_repair_budget(second, self.state_path, stage="prebuild")
        with self.assertRaises(ToolError) as raised:
            enforce_repair_budget(
                make_spec(marker="candidate-3"), self.state_path, stage="prebuild"
            )
        self.assertEqual(raised.exception.code, "REPAIR_BUDGET_EXHAUSTED")

    def test_profile_change_requires_a_new_batch(self) -> None:
        enforce_repair_budget(make_spec(), self.state_path, stage="prebuild")
        with self.assertRaises(ToolError) as raised:
            enforce_repair_budget(
                make_spec(profile="reviewed"), self.state_path, stage="prebuild"
            )
        self.assertEqual(raised.exception.code, "REPAIR_BUDGET_IDENTITY_MISMATCH")

    def test_final_is_read_only_and_requires_current_candidate(self) -> None:
        first = make_spec()
        enforce_repair_budget(first, self.state_path, stage="prebuild")
        before = self.state_path.read_bytes()
        state = enforce_repair_budget(first, self.state_path, stage="final")
        self.assertEqual(state["candidates"], [content_spec_sha256(first)])
        self.assertEqual(self.state_path.read_bytes(), before)
        with self.assertRaises(ToolError) as raised:
            enforce_repair_budget(
                make_spec(marker="unregistered"), self.state_path, stage="final"
            )
        self.assertEqual(raised.exception.code, "REPAIR_BUDGET_CURRENT_MISMATCH")
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_final_rejects_missing_state_without_creating_it(self) -> None:
        with self.assertRaises(ToolError) as raised:
            enforce_repair_budget(make_spec(), self.state_path, stage="final")
        self.assertEqual(raised.exception.code, "REPAIR_BUDGET_MISSING")
        self.assertFalse(self.state_path.exists())


class ValidatorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.spec_path = self.root / "page-reconstruction.json"
        self.snapshot_path = self.root / "build-spec-snapshot.json"

    def write_spec(self, spec: dict) -> None:
        self.spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    def valid_report(self, spec: dict, *, stage: str) -> dict:
        return {
            "valid": True,
            "stage": stage,
            "verification_profile": spec["verification_profile"],
            "spec_sha256": "b" * 64,
            "errors": [],
            "warnings": [],
        }

    def test_prebuild_registers_budget_before_writing_snapshot(self) -> None:
        spec = make_spec()
        self.write_spec(spec)
        with mock.patch.object(
            validator,
            "validate_spec",
            return_value=self.valid_report(spec, stage="prebuild"),
        ):
            result = validator.validate_spec_file(
                self.spec_path,
                stage="prebuild",
                snapshot_path=self.snapshot_path,
            )
        self.assertTrue(result["valid"])
        self.assertEqual(result["repair_budget"]["candidates"], [content_spec_sha256(spec)])
        self.assertTrue((self.root / "repair-budget.json").is_file())
        self.assertTrue(self.snapshot_path.is_file())

    def test_budget_error_becomes_validation_error_and_blocks_snapshot(self) -> None:
        first = make_spec()
        self.write_spec(first)
        enforce_repair_budget(first, self.root / "repair-budget.json", stage="prebuild")
        second = make_spec(marker="candidate-2")
        self.write_spec(second)
        with mock.patch.object(
            validator,
            "validate_spec",
            return_value=self.valid_report(second, stage="prebuild"),
        ):
            result = validator.validate_spec_file(
                self.spec_path,
                stage="prebuild",
                snapshot_path=self.snapshot_path,
            )
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "REPAIR_BUDGET_AUTHORIZATION_REQUIRED")
        self.assertFalse(self.snapshot_path.exists())

    def test_final_checks_budget_without_creating_missing_state(self) -> None:
        spec = make_spec()
        self.write_spec(spec)
        with mock.patch.object(
            validator,
            "validate_spec",
            return_value=self.valid_report(spec, stage="final"),
        ):
            result = validator.validate_spec_file(self.spec_path, stage="final")
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "REPAIR_BUDGET_MISSING")
        self.assertFalse((self.root / "repair-budget.json").exists())

    def test_final_coordinate_check_does_not_load_overlay_producer(self) -> None:
        overlay_path = self.root / "coordinate-overlay.png"
        manifest = "c" * 64
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text(validator.COORDINATE_MANIFEST_METADATA_KEY, manifest)
        Image.new("RGB", (2, 2), "white").save(overlay_path, pnginfo=metadata)
        overlay_sha = hashlib.sha256(overlay_path.read_bytes()).hexdigest()
        page_layout = {
            "coordinate_overlay_evidence": {
                "path": str(overlay_path),
                "sha256": overlay_sha,
                "source_sha256": "a" * 64,
                "manifest_sha256": manifest,
                "grid": {"cols": 20, "rows": 12, "labels": "both"},
                "inspection": "passed",
            }
        }
        clean_reference = {"path": "/tmp/source.png", "sha256": "a" * 64}
        errors: list[dict[str, str]] = []

        with mock.patch.object(
            validator,
            "_load_coordinate_overlay_module",
            side_effect=AssertionError("producer must not load during final"),
        ):
            validator._validate_coordinate_overlay_evidence(
                page_layout,
                clean_reference,
                errors,
                stage="final",
            )

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
