from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "preflight_runtime.py"
FONTCONFIG = ROOT / "assets" / "fontconfig-macos.conf"


def load_module():
    spec = importlib.util.spec_from_file_location("preflight_runtime", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PreflightRuntimeTest(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.is_file())

    def test_stable_libreoffice_version_contract(self) -> None:
        module = load_module()
        self.assertTrue(
            module.is_stable_libreoffice_version("LibreOffice 26.2.3.2 abc")
        )
        for value in (
            "LibreOfficeDev 26.8.0.0.alpha0 abc",
            "LibreOffice 26.3.0 beta1",
            "LibreOffice 26.3.0 rc1",
        ):
            self.assertFalse(module.is_stable_libreoffice_version(value), value)

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_valid_runtime_writes_traceable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "fake-tool"
            executable.write_text("#!/bin/sh\necho fake-tool 1.0\n", encoding="utf-8")
            executable.chmod(0o755)
            report = root / "reports" / "preflight-runtime.json"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--soffice",
                    str(executable),
                    "--pdftoppm",
                    str(executable),
                    "--pdffonts",
                    str(executable),
                    "--fontconfig",
                    str(FONTCONFIG),
                    "--python-module",
                    "json",
                    "--output",
                    str(report),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload, json.loads(report.read_text(encoding="utf-8")))
            self.assertTrue(payload["valid"])
            self.assertEqual([], payload["errors"])
            self.assertEqual(
                str(executable.resolve()), payload["executables"]["soffice"]["path"]
            )
            self.assertRegex(
                payload["executables"]["soffice"]["sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertEqual("libreoffice", payload["renderer_backend"])
            self.assertEqual([1920, 1080], payload["preview_size"])
            self.assertRegex(payload["fontconfig"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(payload["python_modules"]["json"]["available"])

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_prerelease_soffice_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            soffice = root / "soffice"
            soffice.write_text(
                "#!/bin/sh\n"
                "echo 'LibreOfficeDev 26.8.0.0.alpha0 abc'\n",
                encoding="utf-8",
            )
            soffice.chmod(0o755)
            tool = root / "tool"
            tool.write_text("#!/bin/sh\necho tool 1.0\n", encoding="utf-8")
            tool.chmod(0o755)
            report = root / "preflight-runtime.json"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--soffice",
                    str(soffice),
                    "--pdftoppm",
                    str(tool),
                    "--pdffonts",
                    str(tool),
                    "--fontconfig",
                    str(FONTCONFIG),
                    "--output",
                    str(report),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(2, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertIn(
                "RUNTIME_RENDERER_PRERELEASE_FORBIDDEN",
                {entry["code"] for entry in payload["errors"]},
            )

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_expected_runtime_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool = root / "tool"
            tool.write_text("#!/bin/sh\necho tool 1.0\n", encoding="utf-8")
            tool.chmod(0o755)
            expected = root / "expected.json"
            expected.write_text(
                json.dumps(
                    {
                        "renderer_backend": "libreoffice",
                        "preview_size": [1920, 1080],
                        "executables": {
                            "soffice": {"version": "different", "sha256": "f" * 64},
                            "pdftoppm": {"version": "different", "sha256": "f" * 64},
                            "pdffonts": {"version": "different", "sha256": "f" * 64},
                        },
                        "fontconfig": {"sha256": "f" * 64},
                    }
                ),
                encoding="utf-8",
            )
            report = root / "preflight-runtime.json"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--soffice",
                    str(tool),
                    "--pdftoppm",
                    str(tool),
                    "--pdffonts",
                    str(tool),
                    "--fontconfig",
                    str(FONTCONFIG),
                    "--expected-runtime",
                    str(expected),
                    "--output",
                    str(report),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(2, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertIn(
                "RUNTIME_RENDERER_IDENTITY_MISMATCH",
                {entry["code"] for entry in payload["errors"]},
            )

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_version_probe_falls_back_to_short_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "short-version-tool"
            executable.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo unsupported >&2; exit 1; fi\n"
                "if [ \"$1\" = \"-v\" ]; then echo short-version-tool 2.0 >&2; exit 0; fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            report = root / "preflight-runtime.json"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--soffice",
                    str(executable),
                    "--pdftoppm",
                    str(executable),
                    "--pdffonts",
                    str(executable),
                    "--fontconfig",
                    str(FONTCONFIG),
                    "--output",
                    str(report),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                "short-version-tool 2.0",
                payload["executables"]["pdftoppm"]["version"],
            )

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_missing_required_executable_fails_without_partial_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = root / "missing-tool"
            report = root / "preflight-runtime.json"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--soffice",
                    str(missing),
                    "--pdftoppm",
                    str(missing),
                    "--pdffonts",
                    str(missing),
                    "--fontconfig",
                    str(FONTCONFIG),
                    "--output",
                    str(report),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(2, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["valid"])
            self.assertEqual(payload, json.loads(report.read_text(encoding="utf-8")))
            codes = {entry["code"] for entry in payload["errors"]}
            self.assertEqual(
                {
                    "RUNTIME_EXECUTABLE_MISSING:soffice",
                    "RUNTIME_EXECUTABLE_MISSING:pdftoppm",
                    "RUNTIME_EXECUTABLE_MISSING:pdffonts",
                },
                codes,
            )
            self.assertFalse(any(report.parent.glob(f".{report.name}.*.tmp")))


if __name__ == "__main__":
    unittest.main()
