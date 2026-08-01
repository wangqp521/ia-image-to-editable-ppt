from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixture_specs import make_minimal_spec


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "freeze_reconstruction_spec.py"


class FreezeReconstructionSpecTests(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_build_snapshot_is_validated_and_published_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "page-reconstruction.json"
            snapshot = root / "build-spec-snapshot.json"
            payload = make_minimal_spec(root / "fixture")
            source.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            first = self._run(
                str(source),
                "--purpose",
                "build",
                "--output",
                str(snapshot),
            )
            second = self._run(
                str(source),
                "--purpose",
                "build",
                "--output",
                str(snapshot),
            )

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(payload, json.loads(snapshot.read_text(encoding="utf-8")))
            report = json.loads(first.stdout)
            self.assertEqual("build", report["purpose"])
            self.assertEqual(str(snapshot.resolve()), report["snapshot"]["path"])
            self.assertEqual(
                hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                report["snapshot"]["sha256"],
            )
            self.assertEqual(2, second.returncode)
            self.assertIn("BUILD_OUTPUT_INCOMPLETE", second.stderr)

    def test_invalid_build_spec_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "page-reconstruction.json"
            snapshot = root / "build-spec-snapshot.json"
            payload = make_minimal_spec(root / "fixture")
            payload["modules"]["typography"]["slide_coordinate_unit"] = "px"
            source.write_text(json.dumps(payload), encoding="utf-8")

            result = self._run(
                str(source),
                "--purpose",
                "build",
                "--output",
                str(snapshot),
            )

            self.assertEqual(2, result.returncode)
            self.assertFalse(snapshot.exists())
            self.assertIn("SPEC_SNAPSHOT_INVALID", result.stderr)

    def test_pre_review_snapshot_preserves_current_review_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "page-reconstruction.json"
            snapshot = root / "pre-review-spec-snapshot.json"
            payload = make_minimal_spec(root / "fixture")
            payload["visual_gate"] = {
                "tripwire": {
                    "available": False,
                    "triggered": None,
                    "reason": "no_approved_baseline",
                }
            }
            source.write_text(json.dumps(payload), encoding="utf-8")

            result = self._run(
                str(source),
                "--purpose",
                "pre-review",
                "--output",
                str(snapshot),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(payload, json.loads(snapshot.read_text(encoding="utf-8")))
            report = json.loads(result.stdout)
            self.assertRegex(report["review_state_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
