from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock


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
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tool = self.root / "fake-tool"
        self.tool.write_text("#!/bin/sh\necho fake-tool 1.0\n", encoding="utf-8")
        self.tool.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def valid_expected_runtime(self) -> dict[str, object]:
        return {
            "renderer_backend": "libreoffice",
            "preview_size": [1920, 1080],
            "executables": {
                name: {
                    "path": str(self.tool.resolve()),
                    "version": "fake-tool 1.0",
                    "sha256": sha256(self.tool.read_bytes()).hexdigest(),
                    "dynamic_libraries": [],
                }
                for name in ("soffice", "pdftoppm", "pdffonts", "pdftotext")
            },
            "fontconfig": {
                "sha256": sha256(FONTCONFIG.read_bytes()).hexdigest(),
            },
        }

    def run_preflight(
        self,
        pdftotext: Path | None = None,
        expected: object | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        report = self.root / "preflight-runtime.json"
        command = [
            sys.executable,
            str(SCRIPT),
            "--soffice",
            str(self.tool),
            "--pdftoppm",
            str(self.tool),
            "--pdffonts",
            str(self.tool),
            "--pdftotext",
            str(pdftotext or self.tool),
            "--fontconfig",
            str(FONTCONFIG),
            "--output",
            str(report),
        ]
        if expected is not None:
            expected_path = self.root / "expected-runtime.json"
            expected_path.write_text(json.dumps(expected), encoding="utf-8")
            command.extend(["--expected-runtime", str(expected_path)])
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        return completed, json.loads(completed.stdout) if completed.stdout else {}

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
    def test_preflight_requires_and_hashes_pdftotext(self) -> None:
        completed, payload = self.run_preflight(pdftotext=self.tool)

        if completed.returncode != 0:
            self.fail(completed.stderr)
        self.assertEqual(
            sha256(self.tool.read_bytes()).hexdigest(),
            payload["executables"]["pdftotext"]["sha256"],
        )

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_expected_runtime_rejects_changed_pdftotext_identity(self) -> None:
        expected = self.valid_expected_runtime()
        expected["executables"]["pdftotext"]["sha256"] = "f" * 64

        completed, payload = self.run_preflight(expected=expected)

        self.assertEqual(2, completed.returncode)
        self.assertIn(
            "RUNTIME_RENDERER_IDENTITY_MISMATCH",
            {item["code"] for item in payload.get("errors", [])},
        )
        mismatch = next(
            item
            for item in payload["errors"]
            if item["code"] == "RUNTIME_RENDERER_IDENTITY_MISMATCH"
        )
        self.assertEqual("executables.pdftotext.sha256", mismatch["detail"])

    def test_expected_runtime_rejects_changed_pdffonts_dynamic_library_identity(
        self,
    ) -> None:
        expected = self.valid_expected_runtime()
        expected["executables"]["pdffonts"]["dynamic_libraries"] = [
            {
                "path": "/tmp/libfixture.dylib",
                "sha256": "f" * 64,
                "load_names": ["libfixture.dylib"],
            }
        ]

        completed, payload = self.run_preflight(expected=expected)

        self.assertEqual(2, completed.returncode)
        mismatch = next(
            item
            for item in payload["errors"]
            if item["code"] == "RUNTIME_RENDERER_IDENTITY_MISMATCH"
        )
        self.assertEqual(
            "executables.pdffonts.dynamic_libraries",
            mismatch["detail"],
        )

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_expected_runtime_rejects_missing_pdftotext_path(self) -> None:
        expected = self.valid_expected_runtime()
        expected["executables"]["pdftotext"].pop("path")

        completed, payload = self.run_preflight(expected=expected)

        self.assertEqual(2, completed.returncode)
        mismatch = next(
            item
            for item in payload["errors"]
            if item["code"] == "RUNTIME_RENDERER_IDENTITY_MISMATCH"
        )
        self.assertEqual("executables.pdftotext.path", mismatch["detail"])

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_expected_runtime_rejects_changed_pdftotext_path(self) -> None:
        alternate = self.root / "alternate-pdftotext"
        alternate.write_bytes(self.tool.read_bytes())
        alternate.chmod(0o755)
        expected = self.valid_expected_runtime()
        expected["executables"]["pdftotext"]["path"] = str(alternate.resolve())

        completed, payload = self.run_preflight(expected=expected)

        self.assertEqual(2, completed.returncode)
        mismatch = next(
            item
            for item in payload["errors"]
            if item["code"] == "RUNTIME_RENDERER_IDENTITY_MISMATCH"
        )
        self.assertEqual("executables.pdftotext.path", mismatch["detail"])

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_expected_runtime_malformed_containers_return_structured_mismatch(
        self,
    ) -> None:
        malformed = []
        invalid_executables = self.valid_expected_runtime()
        invalid_executables["executables"] = []
        invalid_fontconfig = self.valid_expected_runtime()
        invalid_fontconfig["fontconfig"] = []
        invalid_entry = self.valid_expected_runtime()
        invalid_entry["executables"]["pdftotext"] = []

        for name, expected in (
            ("top-level", malformed),
            ("executables", invalid_executables),
            ("fontconfig", invalid_fontconfig),
            ("tool-entry", invalid_entry),
        ):
            with self.subTest(name=name):
                completed, payload = self.run_preflight(expected=expected)

                if completed.returncode != 2:
                    self.fail(
                        f"expected structured exit 2, got {completed.returncode}: "
                        f"{completed.stderr}"
                    )
                self.assertNotIn("Traceback", completed.stderr)
                self.assertIn(
                    "RUNTIME_RENDERER_IDENTITY_MISMATCH",
                    {item["code"] for item in payload["errors"]},
                )
                report = self.root / "preflight-runtime.json"
                self.assertEqual(
                    payload,
                    json.loads(report.read_text(encoding="utf-8")),
                )

    @unittest.skipUnless(SCRIPT.is_file(), "preflight_runtime.py not implemented")
    def test_preflight_rejects_unavailable_pdftotext_version(self) -> None:
        for name, source in (
            ("failed-version", "#!/bin/sh\nexit 1\n"),
            ("empty-version", "#!/bin/sh\nexit 0\n"),
        ):
            with self.subTest(name=name):
                tool = self.root / name
                tool.write_text(source, encoding="utf-8")
                tool.chmod(0o755)

                completed, payload = self.run_preflight(pdftotext=tool)

                self.assertEqual(2, completed.returncode)
                self.assertIsNone(payload["executables"]["pdftotext"]["version"])
                self.assertIn(
                    "RUNTIME_EXECUTABLE_VERSION_UNAVAILABLE:pdftotext",
                    {item["code"] for item in payload["errors"]},
                )

    def test_preflight_timeout_version_is_explicitly_invalid(self) -> None:
        module = load_module()
        timeout_tool = self.root / "timeout-pdftotext"
        timeout_tool.write_bytes(self.tool.read_bytes())
        timeout_tool.chmod(0o755)
        args = module._parse_args(
            [
                "--soffice",
                str(self.tool),
                "--pdftoppm",
                str(self.tool),
                "--pdffonts",
                str(self.tool),
                "--pdftotext",
                str(timeout_tool),
                "--fontconfig",
                str(FONTCONFIG),
                "--output",
                str(self.root / "runtime.json"),
            ]
        )

        def probe(command, **kwargs):
            if Path(command[0]).resolve() == timeout_tool.resolve():
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="fake-tool 1.0\n",
                stderr="",
            )

        with mock.patch.object(module.subprocess, "run", side_effect=probe):
            payload = module.inspect_runtime(args)

        self.assertFalse(payload["valid"])
        self.assertIsNone(payload["executables"]["pdftotext"]["version"])
        self.assertIn(
            "RUNTIME_EXECUTABLE_VERSION_UNAVAILABLE:pdftotext",
            {item["code"] for item in payload["errors"]},
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
                    "--pdftotext",
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
                {"soffice", "pdftoppm", "pdffonts", "pdftotext"},
                set(payload["executables"]),
            )
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
                    "--pdftotext",
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
                            "pdftotext": {"version": "different", "sha256": "f" * 64},
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
                    "--pdftotext",
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
                    "--pdftotext",
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
                    "--pdftotext",
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
                    "RUNTIME_EXECUTABLE_MISSING:pdftotext",
                },
                codes,
            )
            self.assertFalse(any(report.parent.glob(f".{report.name}.*.tmp")))


if __name__ == "__main__":
    unittest.main()
