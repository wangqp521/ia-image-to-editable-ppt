from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "preflight_runtime.py"
FONTCONFIG = ROOT / "assets" / "fontconfig-macos.conf"


class PreflightRuntimeTest(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.is_file())

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_profile_is_created_next_to_report_with_uri_safe_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "中文 路径"
            root.mkdir()
            executable = root / "fake-tool"
            executable.write_text("#!/bin/sh\necho fake-tool 1.0\n", encoding="utf-8")
            executable.chmod(0o755)
            report = root / "work" / "preflight-runtime.json"

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

            payload = json.loads(completed.stdout)
            profile = report.parent / "libreoffice-profile"
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(profile.is_dir())
            self.assertEqual(
                str(profile.resolve()), payload["libreoffice_profile"]["path"]
            )
            self.assertEqual(
                profile.resolve().as_uri(), payload["libreoffice_profile"]["uri"]
            )
            self.assertTrue(payload["libreoffice_profile"]["writable"])

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_existing_file_at_profile_path_fails_with_specific_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "fake-tool"
            executable.write_text("#!/bin/sh\necho fake-tool 1.0\n", encoding="utf-8")
            executable.chmod(0o755)
            report = root / "work" / "preflight-runtime.json"
            report.parent.mkdir()
            (report.parent / "libreoffice-profile").write_text(
                "blocked", encoding="utf-8"
            )

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

            payload = json.loads(completed.stdout)
            self.assertEqual(2, completed.returncode)
            self.assertIn(
                "LIBREOFFICE_PROFILE_UNWRITABLE",
                {entry["code"] for entry in payload["errors"]},
            )

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
            self.assertRegex(payload["fontconfig"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(payload["python_modules"]["json"]["available"])

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
