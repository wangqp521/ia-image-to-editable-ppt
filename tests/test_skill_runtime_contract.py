from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixture_specs import make_asset_fallback_spec, make_minimal_spec


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
REFERENCES = ROOT / "references"
AUTHORITATIVE_REFERENCES = {
    "measurement-and-layout.md",
    "text-and-editability.md",
    "graphics-and-diagrams.md",
    "pictures-and-icons.md",
    "visual-audit-and-delivery.md",
}
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def chars(name: str) -> int:
    return len((REFERENCES / name).read_text(encoding="utf-8"))


def documented_python_commands() -> list[list[str]]:
    commands: list[list[str]] = []
    for raw_line in SKILL.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("python3 scripts/"):
            commands.append(shlex.split(line))
    return commands


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise AssertionError("missing YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError("unterminated YAML frontmatter") from exc
    result: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator:
            result[key.strip()] = value.strip()
    return result


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class SkillRuntimeContractTests(unittest.TestCase):
    def test_skill_metadata_and_agent_interface_are_compatible(self):
        metadata = frontmatter(SKILL)
        self.assertEqual(set(metadata), {"name", "description"})
        self.assertRegex(metadata["name"], r"^[a-z0-9-]{1,64}$")
        self.assertTrue(metadata["description"])
        self.assertLessEqual(len(metadata["description"]), 1024)

        agent_metadata = ROOT / "agents" / "openai.yaml"
        self.assertTrue(agent_metadata.is_file())
        agent_text = agent_metadata.read_text(encoding="utf-8")
        for field in ("display_name:", "short_description:", "default_prompt:"):
            self.assertEqual(agent_text.count(field), 1)

    def test_reference_routes_resolve_to_exactly_the_runtime_reference_set(self):
        runtime = SKILL.read_text(encoding="utf-8")
        linked = {
            Path(target).name
            for target in MARKDOWN_LINK.findall(runtime)
            if target.startswith("references/")
        }
        existing = {path.name for path in REFERENCES.glob("*.md")}
        self.assertEqual(existing, AUTHORITATIVE_REFERENCES)
        self.assertEqual(linked, existing)
        for target in MARKDOWN_LINK.findall(runtime):
            if target.startswith("references/"):
                self.assertTrue((ROOT / target).is_file(), target)

    def test_runtime_load_budgets(self):
        skill = len(SKILL.read_text(encoding="utf-8"))
        measurement, text, graphics, pictures, audit = (
            chars(name)
            for name in (
                "measurement-and-layout.md",
                "text-and-editability.md",
                "graphics-and-diagrams.md",
                "pictures-and-icons.md",
                "visual-audit-and-delivery.md",
            )
        )
        # Attempt 8 documents five immutable pre-review/review commands in the
        # runtime Skill itself; keep bounded headroom without hiding that fixed
        # executable contract in a conditionally loaded reference.
        self.assertLess(skill + measurement + text + audit, 17500)
        self.assertLess(skill + measurement + graphics + audit, 17500)
        self.assertLess(
            skill + measurement + text + graphics + pictures + audit, 25000
        )

    def test_documented_page_pipeline_matches_runnable_cli_interfaces(self):
        commands = documented_python_commands()
        command_names = [Path(command[1]).name for command in commands]

        def only_command_index(script_name: str) -> int:
            indices = [
                index
                for index, command_name in enumerate(command_names)
                if command_name == script_name
            ]
            self.assertEqual(
                1,
                len(indices),
                f"expected exactly one documented {script_name} command",
            )
            return indices[0]

        def option_value(command: list[str], option: str) -> str:
            self.assertEqual(1, command.count(option), command)
            option_index = next(
                index for index, token in enumerate(command) if token == option
            )
            self.assertLess(option_index + 1, len(command), command)
            return command[option_index + 1]

        initializer_index = only_command_index("init_reconstruction_spec.py")
        compiler_index = only_command_index("build_pptx_from_spec.py")
        structure_index = only_command_index("validate_pptx.py")
        reconstruction_validator_indices = [
            index
            for index, command_name in enumerate(command_names)
            if command_name == "validate_reconstruction_spec.py"
        ]
        self.assertEqual(3, len(reconstruction_validator_indices))
        authoring_indices = [
            index
            for index in reconstruction_validator_indices
            if option_value(commands[index], "--stage") == "authoring"
        ]
        prebuild_indices = [
            index
            for index in reconstruction_validator_indices
            if option_value(commands[index], "--stage") == "prebuild"
        ]
        final_indices = [
            index
            for index in reconstruction_validator_indices
            if option_value(commands[index], "--stage") == "final"
        ]
        self.assertEqual(1, len(authoring_indices), "authoring command must be unique")
        self.assertEqual(1, len(prebuild_indices), "prebuild command must be unique")
        self.assertEqual(1, len(final_indices), "final command must be unique")
        authoring_index = authoring_indices[0]
        prebuild_index = prebuild_indices[0]
        final_index = final_indices[0]
        self.assertLess(initializer_index, authoring_index)
        self.assertLess(authoring_index, prebuild_index)
        self.assertLess(prebuild_index, compiler_index)
        self.assertLess(compiler_index, structure_index)
        self.assertLess(structure_index, final_index)

        initializer = commands[initializer_index]
        for option in ("--source", "--overlay", "--page-id", "--output"):
            self.assertIn(option, initializer)
        for validator_index, expected_output in (
            (authoring_index, "work/authoring-validation.json"),
            (prebuild_index, "work/prebuild-validation.json"),
            (final_index, "work/final-validation.json"),
        ):
            validator = commands[validator_index]
            self.assertIn("--stage", validator)
            self.assertIn("--output", validator)
            self.assertEqual(expected_output, option_value(validator, "--output"))
        compiler = commands[compiler_index]
        for option in ("--spec", "--prebuild-report", "--output", "--build-report"):
            self.assertIn(option, compiler)
        validator = commands[structure_index]
        for option in ("--expected-slides", "--spec", "--build-report", "--output"):
            self.assertIn(option, validator)

        for script_name in {
            "init_reconstruction_spec.py",
            "validate_reconstruction_spec.py",
            "build_pptx_from_spec.py",
            "validate_pptx.py",
        }:
            result = run_cli(str(ROOT / "scripts" / script_name), "--help")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_documented_pre_review_controls_follow_the_fixed_runtime_order(self):
        commands = documented_python_commands()
        command_names = [Path(command[1]).name for command in commands]

        def unique(script_name: str, predicate=lambda _command: True) -> int:
            indices = [
                index
                for index, command in enumerate(commands)
                if Path(command[1]).name == script_name and predicate(command)
            ]
            self.assertEqual(1, len(indices), (script_name, indices))
            return indices[0]

        def option_value(command: list[str], option: str) -> str:
            self.assertEqual(1, command.count(option), command)
            index = command.index(option)
            self.assertLess(index + 1, len(command), command)
            return command[index + 1]

        ordered = [
            unique(
                "validate_reconstruction_spec.py",
                lambda command: option_value(command, "--stage") == "authoring",
            ),
            unique(
                "validate_reconstruction_spec.py",
                lambda command: option_value(command, "--stage") == "prebuild",
            ),
            unique("build_pptx_from_spec.py"),
            unique("render_preview.py"),
            unique("create_rendered_text_geometry.py"),
            unique("validate_pptx.py"),
            unique("validate_background_contract.py"),
            unique("create_visual_diff.py"),
            unique(
                "review_admission.py",
                lambda command: len(command) > 2 and command[2] == "issue",
            ),
            unique(
                "review_admission.py",
                lambda command: len(command) > 2 and command[2] == "invoke",
            ),
            unique(
                "review_admission.py",
                lambda command: len(command) > 2
                and command[2] == "validate-response",
            ),
            unique(
                "validate_reconstruction_spec.py",
                lambda command: option_value(command, "--stage") == "final",
            ),
        ]

        self.assertEqual(ordered, sorted(ordered), command_names)

    def test_documented_pipeline_hands_off_two_immutable_spec_snapshots(self):
        commands = documented_python_commands()

        def option(command: list[str], name: str) -> str:
            return command[command.index(name) + 1]

        freezes = [
            command
            for command in commands
            if Path(command[1]).name == "freeze_reconstruction_spec.py"
        ]
        self.assertEqual(2, len(freezes))
        by_purpose = {option(command, "--purpose"): command for command in freezes}
        self.assertEqual({"build", "pre-review"}, set(by_purpose))
        self.assertEqual(
            "work/build-spec-snapshot.json",
            option(by_purpose["build"], "--output"),
        )
        self.assertEqual(
            "work/pre-review-spec-snapshot.json",
            option(by_purpose["pre-review"], "--output"),
        )

        build_snapshot = "work/build-spec-snapshot.json"
        for script_name in (
            "build_pptx_from_spec.py",
            "validate_pptx.py",
            "create_visual_diff.py",
        ):
            command = next(
                command
                for command in commands
                if Path(command[1]).name == script_name
            )
            self.assertEqual(build_snapshot, option(command, "--spec"))
        for script_name in (
            "create_rendered_text_geometry.py",
            "validate_background_contract.py",
        ):
            command = next(
                command
                for command in commands
                if Path(command[1]).name == script_name
            )
            self.assertEqual(build_snapshot, command[2])

        issue = next(
            command
            for command in commands
            if Path(command[1]).name == "review_admission.py"
            and command[2] == "issue"
        )
        self.assertEqual(
            "work/pre-review-spec-snapshot.json",
            option(issue, "--spec"),
        )

    def test_documented_reviewer_prompt_is_admission_derived_without_manual_page_id(self):
        commands = documented_python_commands()
        review_commands = [
            command
            for command in commands
            if Path(command[1]).name == "review_admission.py"
        ]
        self.assertEqual(3, len(review_commands))
        self.assertTrue(all("--page-id" not in command for command in review_commands))
        issue = next(command for command in review_commands if command[2] == "issue")
        invoke = next(command for command in review_commands if command[2] == "invoke")
        validation = next(
            command for command in review_commands if command[2] == "validate-response"
        )
        for option in (
            "--spec",
            "--pptx",
            "--build-report",
            "--structure-report",
            "--render-report",
            "--text-geometry",
            "--background-report",
            "--visual-diff",
            "--review-round",
            "--output-dir",
        ):
            self.assertIn(option, issue)
        for option in ("--admission", "--invocation-dir"):
            self.assertIn(option, invoke)
        for option in ("--admission", "--invocation", "--response", "--output"):
            self.assertIn(option, validation)

    def test_generated_schema_matches_public_describe_output(self):
        schema_path = ROOT / "schemas" / "page-reconstruction-v2.schema.json"
        self.assertTrue(schema_path.is_file())

        completed = run_cli("scripts/init_reconstruction_spec.py", "--describe")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        described = json.loads(completed.stdout)
        tracked = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(described["json_schema"], tracked)
        self.assertEqual(
            described["contract"]["contract_sha256"],
            tracked["x-schema-contract-sha256"],
        )

    def test_documented_initializer_command_executes_with_absolute_inputs(self):
        initializer = next(
            command
            for command in documented_python_commands()
            if Path(command[1]).name == "init_reconstruction_spec.py"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_minimal_spec(root)
            source = (root / "source.png").resolve()
            overlay = (root / "coordinate-overlay.png").resolve()
            output = (root / "initialized.json").resolve()
            replacements = {
                "<absolute-source>": str(source),
                "<absolute-clean-visual>": str(source),
                "<absolute-overlay>": str(overlay),
                "<absolute-output>": str(output),
                "page-NNN": "page-001",
                "<rapid|reviewed|strict>": "strict",
            }
            resolved = [replacements.get(token, token) for token in initializer]
            for option in ("--source", "--visual", "--overlay", "--output"):
                value = Path(resolved[resolved.index(option) + 1])
                self.assertTrue(value.is_absolute(), f"{option} must document an absolute path")

            completed = run_cli(*resolved[1:])

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            self.assertTrue(output.is_file())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["verification_profile"], "strict")
            self.assertEqual(payload["content_reference"]["path"], str(source))
            self.assertEqual(payload["clean_visual_reference"]["path"], str(source))

    def test_documented_script_entrypoints_exist_and_offer_help(self):
        paths = {ROOT / command[1] for command in documented_python_commands()}
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())
                result = run_cli(str(path), "--help")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_documented_pipeline_completes_a_three_way_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec_path = root / "page-reconstruction.json"
            authoring_path = root / "authoring-validation.json"
            prebuild_path = root / "prebuild-validation.json"
            pptx_path = root / "page.pptx"
            build_report_path = root / "build-report.json"
            structure_path = root / "structure-validation.json"
            spec_path.write_text(
                json.dumps(make_minimal_spec(root), ensure_ascii=False),
                encoding="utf-8",
            )

            authoring = run_cli(
                "scripts/validate_reconstruction_spec.py",
                str(spec_path),
                "--stage",
                "authoring",
                "--output",
                str(authoring_path),
            )
            self.assertEqual(authoring.returncode, 0, authoring.stdout)
            self.assertEqual(
                "authoring",
                json.loads(authoring_path.read_text(encoding="utf-8"))["stage"],
            )
            prebuild = run_cli(
                "scripts/validate_reconstruction_spec.py",
                str(spec_path),
                "--stage",
                "prebuild",
                "--output",
                str(prebuild_path),
            )
            self.assertEqual(prebuild.returncode, 0, prebuild.stdout)
            build = run_cli(
                "scripts/build_pptx_from_spec.py",
                "--spec",
                str(spec_path),
                "--prebuild-report",
                str(prebuild_path),
                "--output",
                str(pptx_path),
                "--build-report",
                str(build_report_path),
            )
            self.assertEqual(build.returncode, 0, build.stdout)
            validate = run_cli(
                "scripts/validate_pptx.py",
                str(pptx_path),
                "--expected-slides",
                "1",
                "--spec",
                str(spec_path),
                "--build-report",
                str(build_report_path),
                "--output",
                str(structure_path),
            )
            self.assertEqual(validate.returncode, 0, validate.stdout)
            report = json.loads(structure_path.read_text(encoding="utf-8"))
            self.assertTrue(report["valid"], report["errors"])
            self.assertGreater(report["build_report_objects_checked"], 0)
            self.assertGreater(report["representation_facts_checked"], 0)

    def test_full_editability_asset_fallback_fails_before_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec_path = root / "page-reconstruction.json"
            report_path = root / "prebuild-validation.json"
            spec_path.write_text(
                json.dumps(
                    make_asset_fallback_spec(root, required_editability="full"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            completed = run_cli(
                "scripts/validate_reconstruction_spec.py",
                str(spec_path),
                "--stage",
                "prebuild",
                "--output",
                str(report_path),
            )
            self.assertNotEqual(completed.returncode, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["valid"])
            self.assertIn(
                "REPRESENTATION_FALLBACK_FORBIDDEN",
                {issue["code"] for issue in report["errors"]},
            )
            self.assertFalse((root / "page.pptx").exists())
            self.assertFalse((root / "build-report.json").exists())


if __name__ == "__main__":
    unittest.main()
