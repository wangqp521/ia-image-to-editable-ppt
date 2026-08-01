"""Public schema-v2 initializer behavior and fail-closed I/O tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "init_reconstruction_spec.py"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
OVERLAY_SCRIPT = ROOT / "scripts" / "create_coordinate_overlay.py"
VALIDATOR_SCRIPT = ROOT / "scripts" / "validate_reconstruction_spec.py"


def _load(path: Path, name: str):
    module_spec = importlib.util.spec_from_file_location(name, path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


class InitReconstructionSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source.png"
        self.visual = self.root / "visual.png"
        Image.new("RGB", (160, 90), "white").save(self.source)
        Image.new("RGBA", (320, 180), (240, 240, 240, 255)).save(self.visual)
        self.overlay = self.root / "coordinate-overlay.png"
        _load(OVERLAY_SCRIPT, "init_test_overlay").create_coordinate_overlay(
            self.visual, self.overlay
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _assert_script(self) -> None:
        self.assertTrue(SCRIPT.is_file(), "initializer CLI is not implemented")

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        self._assert_script()
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )

    def _valid_arguments(self, output: Path) -> list[str]:
        return [
            "--source",
            str(self.source.resolve()),
            "--visual",
            str(self.visual.resolve()),
            "--overlay",
            str(self.overlay.resolve()),
            "--page-id",
            "page-001",
            "--profile",
            "strict",
            "--output",
            str(output),
        ]

    def test_real_inputs_bind_absolute_identity_dimensions_and_exact_envelopes(self) -> None:
        output = self.root / "page-reconstruction.json"

        completed = self._run(*self._valid_arguments(output))

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        spec = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(spec["schema_version"], 2)
        self.assertEqual(spec["page_id"], "page-001")
        self.assertEqual(spec["verification_profile"], "strict")
        self.assertEqual(spec["delivery_status"], "pending")
        self.assertEqual(
            spec["session_reuse"],
            {
                "mode": "fresh_reconstruction",
                "reason": "new_session",
                "artifacts": [],
            },
        )
        self.assertEqual(spec["content_reference"]["path"], str(self.source.resolve()))
        self.assertEqual(spec["clean_visual_reference"]["path"], str(self.visual.resolve()))
        self.assertEqual(
            spec["content_reference"]["sha256"],
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            spec["clean_visual_reference"]["sha256"],
            hashlib.sha256(self.visual.read_bytes()).hexdigest(),
        )
        self.assertEqual(spec["canvas"]["source_size"], [160, 90])
        self.assertEqual(spec["canvas"]["visual_size"], [320, 180])
        self.assertEqual(spec["canvas"]["page_frame_bbox"], [0, 0, 320, 180])
        self.assertEqual(spec["canvas"]["slide_size_emu"], [12192000, 6858000])
        evidence = spec["modules"]["page_layout"]["coordinate_overlay_evidence"]
        self.assertEqual(evidence["path"], str(self.overlay.resolve()))
        self.assertEqual(evidence["source_sha256"], spec["clean_visual_reference"]["sha256"])
        self.assertEqual(evidence["inspection"], "passed")
        self.assertEqual(spec["activated_modules"][-1], "background")
        self.assertEqual(spec["regions"][0]["layer"], 10)
        self.assertEqual(spec["regions"][0]["element_ids"], ["background-base"])
        self.assertEqual(spec["elements"][0]["element_id"], "background-base")
        self.assertEqual(spec["elements"][0]["kind"], "shape")
        self.assertEqual(spec["elements"][0]["layer"], 0)
        self.assertEqual(spec["elements"][0]["source_bbox"], [0, 0, 320, 180])
        self.assertEqual(
            spec["elements"][0]["slide_bbox"], [0, 0, 12192000, 6858000]
        )
        self.assertTrue(spec["elements"][0]["editable"])
        self.assertEqual(
            spec["elements"][0]["style"],
            {
                "shape_type": "rectangle",
                "fill": {"type": "solid", "color": "#FFFFFF", "opacity": 1},
                "effects": "none",
                "rotation": 0,
            },
        )
        self.assertEqual(spec["reading_order"], ["background-base"])
        self.assertEqual(
            spec["modules"]["background"]["items"][0]["bound_element_id"],
            "background-base",
        )
        background_item = spec["modules"]["background"]["items"][0]
        self.assertEqual(
            background_item["source_provenance"],
            {
                "kind": "native_measurement",
                "source_path": str(self.visual.resolve()),
                "source_sha256": spec["clean_visual_reference"]["sha256"],
            },
        )
        self.assertEqual(background_item["evidence"], [str(self.visual.resolve())])
        self.assertEqual(
            spec["visual_gate"],
            {"status": "pending", "evidence": [], "tripwire": None},
        )
        self.assertEqual(
            spec["editability_gate"], {"status": "pending", "evidence": []}
        )
        self.assertEqual(spec["modules"]["typography"]["items"], [])
        self.assertEqual(spec["modules"]["representation_plan"]["items"], [])
        self.assertNotIn("coverage", spec)
        self.assertNotIn("prebuild", spec)

        from lib.background_contracts import (
            resolved_element_mode_map,
            validate_background_prebuild,
        )
        from lib.element_contracts import validate_element_contract
        from pptx_builder.contracts import validate_renderer_contracts

        modes = resolved_element_mode_map(spec)
        self.assertEqual(validate_background_prebuild(spec), [])
        self.assertEqual(validate_element_contract(spec["elements"][0]), [])
        self.assertEqual(
            validate_renderer_contracts(
                spec,
                {"background-base": spec["elements"][0]},
                modes,
                {},
            ),
            [],
        )

    def test_visual_defaults_to_source(self) -> None:
        overlay = self.root / "source-overlay.png"
        _load(OVERLAY_SCRIPT, "init_test_default_overlay").create_coordinate_overlay(
            self.source, overlay
        )
        output = self.root / "default-visual.json"

        completed = self._run(
            "--source",
            str(self.source.resolve()),
            "--overlay",
            str(overlay.resolve()),
            "--page-id",
            "page-002",
            "--output",
            str(output),
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        spec = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(spec["content_reference"], spec["clean_visual_reference"])
        self.assertEqual(spec["verification_profile"], "strict")

    def test_invalid_page_profile_paths_and_symlinks_fail_closed(self) -> None:
        linked = self.root / "linked-source.png"
        linked.symlink_to(self.source)
        bad_page = self._valid_arguments(self.root / "bad-page.json")
        bad_page[bad_page.index("--page-id") + 1] = "page-1"
        bad_profile = self._valid_arguments(self.root / "bad-profile.json")
        bad_profile[bad_profile.index("--profile") + 1] = "low"
        relative_source = self._valid_arguments(self.root / "relative.json")
        relative_source[relative_source.index("--source") + 1] = self.source.name
        missing_source = self._valid_arguments(self.root / "missing.json")
        missing_source[missing_source.index("--source") + 1] = str(
            (self.root / "missing.png").resolve()
        )
        symlink_source = self._valid_arguments(self.root / "symlink.json")
        symlink_source[symlink_source.index("--source") + 1] = str(linked)
        cases = (
            ("bad-page", bad_page),
            ("bad-profile", bad_profile),
            ("relative-source", relative_source),
            ("missing-source", missing_source),
            ("symlink-source", symlink_source),
        )

        for name, arguments in cases:
            with self.subTest(name=name):
                completed = self._run(*arguments)
                self.assertNotEqual(completed.returncode, 0)
                payload = json.loads(completed.stdout)
                self.assertFalse(payload["ok"])
                output = Path(arguments[arguments.index("--output") + 1])
                self.assertFalse(output.exists())

    def test_output_is_never_overwritten(self) -> None:
        output = self.root / "existing.json"
        output.write_text("keep-me\n", encoding="utf-8")

        completed = self._run(*self._valid_arguments(output))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(output.read_text(encoding="utf-8"), "keep-me\n")
        self.assertEqual(json.loads(completed.stdout)["errors"][0]["code"], "INIT_OUTPUT_EXISTS")

    def test_api_rejects_relative_output_before_reaching_the_writer(self) -> None:
        self._assert_script()
        module = _load(SCRIPT, "init_relative_output_api")
        writer = mock.Mock()

        with mock.patch.object(
            module, "atomic_write_json_no_overwrite", writer
        ), self.assertRaises(module.ToolError) as raised:
            module.initialize_reconstruction_spec(
                source=self.source.resolve(),
                visual=self.visual.resolve(),
                overlay=self.overlay.resolve(),
                page_id="page-001",
                profile="strict",
                output=Path("relative-api.json"),
            )

        self.assertEqual(
            raised.exception.as_dict(),
            {
                "code": "INIT_OUTPUT_INVALID",
                "path": "output",
                "detail": "output must be a literal absolute path",
            },
        )
        writer.assert_not_called()

    def test_cli_rejects_relative_output_without_publishing(self) -> None:
        output = Path("relative-cli.json")
        completed = self._run(*self._valid_arguments(output))

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "ok": False,
                "errors": [
                    {
                        "code": "INIT_OUTPUT_INVALID",
                        "path": "output",
                        "detail": "output must be a literal absolute path",
                    }
                ],
            },
        )
        self.assertFalse((self.root / output).exists())

    def test_api_rejects_nested_output_ancestor_symlink_before_publishing(self) -> None:
        self._assert_script()
        module = _load(SCRIPT, "init_nested_output_symlink_api")
        real_parent = self.root / "real-output"
        (real_parent / "nested").mkdir(parents=True)
        linked_parent = self.root / "linked-output"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        output = linked_parent / "nested" / "api.json"
        real_output = real_parent / "nested" / "api.json"
        caught = None

        try:
            module.initialize_reconstruction_spec(
                source=self.source.resolve(),
                visual=self.visual.resolve(),
                overlay=self.overlay.resolve(),
                page_id="page-001",
                profile="strict",
                output=output,
            )
        except module.ToolError as exc:
            caught = exc

        self.assertFalse(
            real_output.exists(),
            "initializer followed a nested output ancestor symlink and published to its real target",
        )
        self.assertIsNotNone(caught, "initializer must reject the symlink ancestor")
        assert caught is not None
        self.assertEqual(
            caught.as_dict(),
            {
                "code": "INIT_OUTPUT_INVALID",
                "path": str(linked_parent),
                "detail": "output parent must be an existing real directory",
            },
        )

    def test_cli_rejects_nested_output_ancestor_symlink_without_publishing(self) -> None:
        real_parent = self.root / "real-cli-output"
        (real_parent / "nested").mkdir(parents=True)
        linked_parent = self.root / "linked-cli-output"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        output = linked_parent / "nested" / "cli.json"
        real_output = real_parent / "nested" / "cli.json"

        completed = self._run(*self._valid_arguments(output))

        self.assertFalse(
            real_output.exists(),
            "initializer CLI followed a nested output ancestor symlink and published to its real target",
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "ok": False,
                "errors": [
                    {
                        "code": "INIT_OUTPUT_INVALID",
                        "path": str(linked_parent),
                        "detail": "output parent must be an existing real directory",
                    }
                ],
            },
        )

    def test_atomic_write_failure_is_stable_and_publishes_nothing(self) -> None:
        self._assert_script()
        module = _load(SCRIPT, "init_atomic_failure")
        output = self.root / "atomic-failure.json"

        with mock.patch.object(
            module,
            "atomic_write_json_no_overwrite",
            side_effect=OSError("simulated atomic failure"),
        ), self.assertRaises(module.ToolError) as raised:
            module.initialize_reconstruction_spec(
                source=self.source.resolve(),
                visual=self.visual.resolve(),
                overlay=self.overlay.resolve(),
                page_id="page-001",
                profile="strict",
                output=output,
            )

        self.assertEqual(raised.exception.code, "INIT_OUTPUT_WRITE_FAILED")
        self.assertFalse(output.exists())

    def test_post_link_directory_failures_roll_back_output_and_preserve_cause(self) -> None:
        self._assert_script()
        module = _load(SCRIPT, "init_post_link_failures")
        real_open = module.os.open
        real_fsync = module.os.fsync
        real_close = module.os.close

        for operation in ("open", "fsync", "close"):
            with self.subTest(operation=operation):
                output = self.root / f"post-link-{operation}.json"
                directory_fds: set[int] = set()
                failed = False

                def tracked_open(path, flags, *args):
                    nonlocal failed
                    if Path(path) == output.parent and operation == "open" and not failed:
                        failed = True
                        raise OSError("post-link directory open failure")
                    descriptor = real_open(path, flags, *args)
                    if Path(path) == output.parent:
                        directory_fds.add(descriptor)
                    return descriptor

                def tracked_fsync(descriptor):
                    nonlocal failed
                    if descriptor in directory_fds and operation == "fsync" and not failed:
                        failed = True
                        raise OSError("post-link directory fsync failure")
                    return real_fsync(descriptor)

                def tracked_close(descriptor):
                    nonlocal failed
                    if descriptor in directory_fds and operation == "close" and not failed:
                        failed = True
                        real_close(descriptor)
                        raise OSError("post-link directory close failure")
                    return real_close(descriptor)

                with mock.patch.object(module.os, "open", side_effect=tracked_open), mock.patch.object(
                    module.os, "fsync", side_effect=tracked_fsync
                ), mock.patch.object(module.os, "close", side_effect=tracked_close), self.assertRaises(
                    module.ToolError
                ) as raised:
                    module.initialize_reconstruction_spec(
                        source=self.source.resolve(),
                        visual=self.visual.resolve(),
                        overlay=self.overlay.resolve(),
                        page_id="page-001",
                        profile="strict",
                        output=output,
                    )

                self.assertTrue(failed, f"{operation} fault was not injected")
                self.assertEqual(raised.exception.code, "INIT_OUTPUT_WRITE_FAILED")
                self.assertIsInstance(raised.exception.__cause__, OSError)
                self.assertIn(
                    f"post-link directory {operation} failure",
                    str(raised.exception.__cause__),
                )
                self.assertFalse(output.exists())

    def test_initialized_skeleton_has_only_missing_authoring_content_errors(self) -> None:
        output = self.root / "skeleton.json"
        completed = self._run(*self._valid_arguments(output))
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        validator = _load(VALIDATOR_SCRIPT, "init_skeleton_validator")

        result = validator.validate_spec(
            json.loads(output.read_text(encoding="utf-8")), stage="prebuild"
        )

        self.assertFalse(result["valid"])
        self.assertEqual(
            {(item["code"], item["path"]) for item in result["errors"]},
            {
                ("REPRESENTATION_INCOMPLETE", "modules.representation_plan.items"),
                ("SPEC_TYPOGRAPHY_ITEMS_INVALID", "modules.typography.items"),
            },
        )
        forbidden_fragments = ("SCHEMA", "SESSION", "CANVAS", "REFERENCE", "OVERLAY", "UNKNOWN")
        self.assertFalse(
            [
                item
                for item in result["errors"]
                if any(fragment in item["code"] for fragment in forbidden_fragments)
            ]
        )

    def test_help_and_describe_are_public_and_machine_readable(self) -> None:
        help_result = self._run("--help")
        describe_result = self._run("--describe")

        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--source", help_result.stdout)
        self.assertIn("--overlay", help_result.stdout)
        self.assertIn("--describe", help_result.stdout)
        self.assertEqual(describe_result.returncode, 0, describe_result.stderr)
        payload = json.loads(describe_result.stdout)
        self.assertEqual(payload["contract"]["contract_id"], "page-reconstruction-v2")
        self.assertEqual(payload["json_schema"]["$ref"], "#/$defs/PageReconstruction")


if __name__ == "__main__":
    unittest.main()
