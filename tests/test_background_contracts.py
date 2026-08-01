"""Isolated schema-v2 background prebuild contract regressions."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def background_spec(root: Path, *, mode: str, kind: str) -> dict[str, Any]:
    """Replace the shared base with one exact, full-page background binding."""
    from tests.fixture_specs import make_minimal_spec

    spec = make_minimal_spec(root)
    spec["elements"][0]["layer"] = 10
    source = spec["clean_visual_reference"]
    provenance = {
        "kind": "native_measurement",
        "source_path": source["path"],
        "source_sha256": source["sha256"],
    }
    style: dict[str, Any]
    content: dict[str, Any]
    if kind == "shape":
        style = {
            "shape_type": "rectangle",
            "fill": {"type": "solid", "color": "#FFFFFF", "opacity": 1},
            "line": "noLine",
            "effects": "none",
            "rotation": 0,
        }
        content = {}
    else:
        clean_path = root / "clean-background.png"
        Image.new("RGB", (1600, 900), (244, 247, 250)).save(clean_path)
        clean_sha256 = hashlib.sha256(clean_path.read_bytes()).hexdigest()
        asset = {
            "path": str(clean_path.resolve()),
            "asset_sha256": clean_sha256,
            "pixel_size": [1600, 900],
        }
        style = {"rotation": 0, "opacity": 1}
        content = {
            "asset": asset,
            "mode": "none",
            "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
        }
        provenance = {
            "kind": "clean_background_asset",
            "source_path": asset["path"],
            "source_sha256": asset["asset_sha256"],
        }
    background = next(
        element
        for element in spec["elements"]
        if element["element_id"] == "background-base"
    )
    background.update(
        {
            "kind": kind,
            "editable": kind == "shape",
            "style": style,
            "content": content,
        }
    )
    spec["modules"]["background"]["items"][0].update(
        {
            "selected_mode": mode,
            "source_provenance": provenance,
            "reason": "isolated page background",
            "evidence": [source["path"]],
        }
    )
    return spec


def background_validation_fixture(
    root: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Compile the shared native background and retain an overlap mutation fact."""
    from tests.fixture_specs import make_minimal_spec
    from tests.test_build_pptx_from_spec import compile_fixture

    spec = make_minimal_spec(root)
    pptx, report = compile_fixture(root / "compiled", spec)
    source = spec["clean_visual_reference"]
    background_fact = {
        "source_fact_id": "fact-background-base",
        "semantic_role": "shape",
        "source_bbox": [0, 0, 1600, 900],
        "required": True,
        "selected_mode": "native",
        "required_editability": "full",
        "fallback_policy": "forbid",
        "bound_element_ids": ["background-base"],
        "reason": "controlled representation overlap mutation",
        "coverage_status": "covered",
        "evidence": [source["path"]],
    }
    return pptx, spec, report, background_fact


class BackgroundContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _contracts(self):
        try:
            from lib import background_contracts
        except ImportError as exc:
            self.fail(f"background contracts are not implemented: {exc}")
        return background_contracts

    def _issue_codes(self, spec: dict[str, Any]) -> set[str]:
        return {
            issue.code
            for issue in self._contracts().validate_background_prebuild(spec)
        }

    def test_native_base_is_valid_and_resolves_without_extending_asset_modes(self) -> None:
        from lib import representation_contracts

        contracts = self._contracts()
        spec = background_spec(self.root, mode="native", kind="shape")
        self.assertEqual([], contracts.validate_background_prebuild(spec))
        self.assertEqual(
            "native", contracts.resolved_element_mode_map(spec)["background-base"]
        )
        self.assertNotIn("background_picture", representation_contracts.MODES)

    def test_shared_fixture_background_helper_has_unique_ids_and_bindings(self) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        element_ids = [item["element_id"] for item in spec["elements"]]
        bindings = [
            item["bound_element_id"]
            for item in spec["modules"]["background"]["items"]
        ]

        self.assertEqual(len(element_ids), len(set(element_ids)))
        self.assertEqual(bindings, ["background-base"])

    def test_background_picture_cannot_bind_an_icon(self) -> None:
        contracts = self._contracts()
        spec = background_spec(self.root, mode="background_picture", kind="icon")
        issues = contracts.validate_background_prebuild(spec)
        self.assertIn(
            "BACKGROUND_ICON_BINDING_FORBIDDEN", {issue.code for issue in issues}
        )

    def test_independent_clean_background_picture_is_valid(self) -> None:
        contracts = self._contracts()
        spec = background_spec(self.root, mode="background_picture", kind="picture")
        self.assertEqual([], contracts.validate_background_prebuild(spec))

    def test_original_full_slide_reference_is_not_a_background_asset(self) -> None:
        contracts = self._contracts()
        spec = background_spec(self.root, mode="background_picture", kind="picture")
        spec["elements"][-1]["content"]["asset"] = {
            "path": spec["clean_visual_reference"]["path"],
            "asset_sha256": spec["clean_visual_reference"]["sha256"],
            "pixel_size": [1600, 900],
        }
        issues = contracts.validate_background_prebuild(spec)
        self.assertIn(
            "BACKGROUND_FOREGROUND_CONTAMINATION_RISK",
            {issue.code for issue in issues},
        )

    def test_original_reference_hash_is_rejected_case_insensitively(self) -> None:
        contracts = self._contracts()
        spec = background_spec(self.root, mode="background_picture", kind="picture")
        reference = spec["clean_visual_reference"]
        spec["elements"][-1]["content"]["asset"] = {
            "path": reference["path"],
            "asset_sha256": reference["sha256"],
            "pixel_size": [1600, 900],
        }
        spec["modules"]["background"]["items"][0]["source_provenance"] = {
            "kind": "clean_background_asset",
            "source_path": reference["path"],
            "source_sha256": reference["sha256"].upper(),
        }
        reference["sha256"] = reference["sha256"].upper()
        issues = contracts.validate_background_prebuild(spec)
        self.assertIn(
            "BACKGROUND_FOREGROUND_CONTAMINATION_RISK",
            {issue.code for issue in issues},
        )
        self.assertNotIn(
            "BACKGROUND_PROVENANCE_INVALID", {issue.code for issue in issues}
        )

    def test_background_module_and_nested_records_are_exact(self) -> None:
        contracts = self._contracts()
        cases = (
            ("modules.background", lambda spec: spec["modules"]["background"].update({"stale": True})),
            ("modules.background.items[0]", lambda spec: spec["modules"]["background"]["items"][0].update({"stale": True})),
            ("modules.background.items[0].source_provenance", lambda spec: spec["modules"]["background"]["items"][0]["source_provenance"].update({"stale": True})),
        )
        for expected_path, mutate in cases:
            with self.subTest(path=expected_path):
                spec = background_spec(self.root, mode="native", kind="shape")
                mutate(spec)
                issues = contracts.validate_background_prebuild(spec)
                self.assertTrue(issues)
                self.assertEqual(expected_path, issues[0].path)

    def test_non_string_mapping_keys_return_stable_contract_issues(self) -> None:
        contracts = self._contracts()
        cases = (
            (
                "module",
                "native",
                "shape",
                lambda spec: spec["modules"]["background"].__setitem__(
                    1, "stale"
                ),
                "modules.background",
            ),
            (
                "item",
                "native",
                "shape",
                lambda spec: spec["modules"]["background"]["items"][
                    0
                ].__setitem__(1, "stale"),
                "modules.background.items[0]",
            ),
            (
                "provenance",
                "native",
                "shape",
                lambda spec: spec["modules"]["background"]["items"][0][
                    "source_provenance"
                ].__setitem__(1, "stale"),
                "modules.background.items[0].source_provenance",
            ),
            (
                "asset",
                "background_picture",
                "picture",
                lambda spec: spec["elements"][-1]["content"][
                    "asset"
                ].__setitem__(1, "stale"),
                "elements.background-base.content.asset",
            ),
        )
        for name, mode, kind, mutate, expected_path in cases:
            with self.subTest(name=name):
                spec = background_spec(self.root, mode=mode, kind=kind)
                mutate(spec)

                issues = contracts.validate_background_prebuild(spec)

                self.assertIn(
                    (
                        "BACKGROUND_INCOMPLETE",
                        expected_path,
                        "record field names must be strings",
                    ),
                    {(issue.code, issue.path, issue.detail) for issue in issues},
                )

    def test_module_requires_nonempty_items_and_exactly_one_base(self) -> None:
        contracts = self._contracts()
        spec = background_spec(self.root, mode="native", kind="shape")
        spec["modules"]["background"]["items"] = []
        self.assertIn(
            "BACKGROUND_INCOMPLETE",
            {issue.code for issue in contracts.validate_background_prebuild(spec)},
        )
        spec = background_spec(self.root, mode="native", kind="shape")
        spec["modules"]["background"]["items"][0]["role"] = "texture"
        self.assertIn(
            "BACKGROUND_INCOMPLETE",
            {issue.code for issue in contracts.validate_background_prebuild(spec)},
        )

    def test_duplicate_background_id_fails_at_its_own_field(self) -> None:
        contracts = self._contracts()
        spec = background_spec(self.root, mode="native", kind="shape")
        duplicate = dict(spec["modules"]["background"]["items"][0])
        duplicate["role"] = "texture"
        duplicate["bound_element_id"] = "unknown-background"
        spec["modules"]["background"]["items"].append(duplicate)
        self.assertIn(
            (
                "BACKGROUND_BINDING_CONFLICT",
                "modules.background.items[1].background_id",
            ),
            {
                (issue.code, issue.path)
                for issue in contracts.validate_background_prebuild(spec)
            },
        )

    def test_duplicate_bound_element_fails_at_its_own_field(self) -> None:
        contracts = self._contracts()
        spec = background_spec(self.root, mode="native", kind="shape")
        duplicate = dict(spec["modules"]["background"]["items"][0])
        duplicate["background_id"] = "background-002"
        duplicate["role"] = "texture"
        spec["modules"]["background"]["items"].append(duplicate)
        self.assertIn(
            (
                "BACKGROUND_BINDING_CONFLICT",
                "modules.background.items[1].bound_element_id",
            ),
            {
                (issue.code, issue.path)
                for issue in contracts.validate_background_prebuild(spec)
            },
        )

    def test_representation_overlap_fails_in_validator_and_resolved_map(self) -> None:
        from lib.error_codes import ToolError

        contracts = self._contracts()
        spec = background_spec(self.root, mode="native", kind="shape")
        spec["modules"]["representation_plan"]["items"][0][
            "bound_element_ids"
        ].append("background-base")
        self.assertIn(
            (
                "BACKGROUND_BINDING_CONFLICT",
                "modules.background.items[0].bound_element_id",
            ),
            {
                (issue.code, issue.path)
                for issue in contracts.validate_background_prebuild(spec)
            },
        )
        with self.assertRaises(ToolError) as raised:
            contracts.resolved_element_mode_map(spec)
        self.assertEqual("BACKGROUND_BINDING_CONFLICT", raised.exception.code)
        self.assertEqual("modules.background", raised.exception.path)

    def test_validate_pptx_checks_resolved_background_mode_in_real_report(
        self,
    ) -> None:
        import validate_pptx

        pptx, spec, report, _background_fact = background_validation_fixture(
            self.root
        )

        valid = validate_pptx.validate_pptx(pptx, 1, spec, report)

        self.assertEqual([], valid["errors"])
        self.assertTrue(valid["valid"])
        self.assertEqual(
            "native", report["elements"]["background-base"]["selected_mode"]
        )

        report["elements"]["background-base"][
            "selected_mode"
        ] = "background_picture"
        mismatched = validate_pptx.validate_pptx(pptx, 1, spec, report)
        self.assertIn("BUILD_REPORT_MISMATCH", mismatched["errors"])
        self.assertTrue(
            any(
                "build_report.elements.background-base.selected_mode" in warning
                for warning in mismatched["warnings"]
            )
        )

    def test_validate_pptx_returns_spec_invalid_for_background_prebuild_defect(
        self,
    ) -> None:
        import validate_pptx
        from lib.hashing import canonical_json_sha256

        pptx, spec, report, _background_fact = background_validation_fixture(
            self.root
        )
        spec["modules"]["background"]["items"][0][
            "contains_foreground_semantics"
        ] = True
        report["schema_sha256"] = canonical_json_sha256(spec)

        result = validate_pptx.validate_pptx(pptx, 1, spec, report)

        self.assertIn("RECONSTRUCTION_SPEC_INVALID", result["errors"])
        self.assertTrue(
            any(
                "modules.background.items[0].contains_foreground_semantics"
                in warning
                for warning in result["warnings"]
            )
        )

    def test_validate_pptx_rejects_real_report_with_representation_overlap(
        self,
    ) -> None:
        import validate_pptx
        from lib.hashing import canonical_json_sha256
        from lib.representation_contracts import representation_summary

        pptx, spec, report, background_fact = background_validation_fixture(
            self.root
        )
        spec["modules"]["representation_plan"]["items"].append(background_fact)
        report["schema_sha256"] = canonical_json_sha256(spec)
        report["representation_summary"] = representation_summary(spec)

        result = validate_pptx.validate_pptx(pptx, 1, spec, report)

        self.assertIn("RECONSTRUCTION_SPEC_INVALID", result["errors"])
        self.assertTrue(
            any(
                "modules.background.items[0].bound_element_id" in warning
                for warning in result["warnings"]
            )
        )

    def test_mode_kind_provenance_and_foreground_flag_are_strict(self) -> None:
        contracts = self._contracts()
        mutations = (
            lambda item: item.update({"selected_mode": "native", "source_provenance": {**item["source_provenance"], "kind": "clean_background_asset"}}),
            lambda item: item.update({"contains_foreground_semantics": 0}),
            lambda item: item.update({"selected_mode": "asset"}),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                spec = background_spec(self.root, mode="native", kind="shape")
                mutate(spec["modules"]["background"]["items"][0])
                self.assertTrue(contracts.validate_background_prebuild(spec))

    def test_non_string_enums_fail_closed_without_type_errors(self) -> None:
        from lib.error_codes import ToolError

        contracts = self._contracts()
        cases = (
            ("role", []),
            ("selected_mode", {}),
            ("source_provenance.kind", []),
        )
        for field, value in cases:
            with self.subTest(field=field):
                spec = background_spec(self.root, mode="native", kind="shape")
                item = spec["modules"]["background"]["items"][0]
                if field == "source_provenance.kind":
                    item["source_provenance"]["kind"] = value
                else:
                    item[field] = value
                self.assertTrue(contracts.validate_background_prebuild(spec))
                if field == "selected_mode":
                    with self.assertRaises(ToolError) as raised:
                        contracts.background_element_modes(spec)
                    self.assertEqual("BACKGROUND_INCOMPLETE", raised.exception.code)

    def test_icon_module_binding_is_forbidden_even_for_picture_kind(self) -> None:
        contracts = self._contracts()
        spec = background_spec(self.root, mode="background_picture", kind="picture")
        spec["modules"]["icons"] = {"icons": [{"element_id": "background-base"}]}
        issues = contracts.validate_background_prebuild(spec)
        self.assertIn(
            "BACKGROUND_ICON_BINDING_FORBIDDEN", {issue.code for issue in issues}
        )

    def test_background_must_be_below_every_foreground_element(self) -> None:
        contracts = self._contracts()
        spec = background_spec(self.root, mode="native", kind="shape")
        spec["elements"][-1]["layer"] = 10
        issues = contracts.validate_background_prebuild(spec)
        self.assertIn("BACKGROUND_LAYER_INVALID", {issue.code for issue in issues})

    def test_picture_asset_identity_must_match_clean_provenance(self) -> None:
        contracts = self._contracts()
        spec = background_spec(self.root, mode="background_picture", kind="picture")
        spec["modules"]["background"]["items"][0]["source_provenance"][
            "source_sha256"
        ] = "f" * 64
        issues = contracts.validate_background_prebuild(spec)
        self.assertIn("BACKGROUND_PROVENANCE_INVALID", {issue.code for issue in issues})

    def test_fact_source_bbox_must_exactly_match_bound_element(self) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        spec["modules"]["background"]["items"][0]["source_bbox"] = [
            1,
            0,
            1599,
            900,
        ]
        self.assertIn("BACKGROUND_GEOMETRY_INVALID", self._issue_codes(spec))

    def test_base_source_bbox_must_exactly_cover_page_frame(self) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        inset = [1, 1, 1598, 898]
        spec["modules"]["background"]["items"][0]["source_bbox"] = inset
        spec["elements"][-1]["source_bbox"] = inset
        self.assertIn("BACKGROUND_GEOMETRY_INVALID", self._issue_codes(spec))

    def test_background_slide_bbox_rejects_inset_and_overscan(self) -> None:
        cases = ([1, 1, 12191998, 6857998], [0, 0, 12192001, 6858000])
        for slide_bbox in cases:
            with self.subTest(slide_bbox=slide_bbox):
                spec = background_spec(self.root, mode="native", kind="shape")
                spec["elements"][-1]["slide_bbox"] = slide_bbox
                self.assertIn(
                    "BACKGROUND_GEOMETRY_INVALID", self._issue_codes(spec)
                )

    def test_background_picture_rejects_nonzero_crop(self) -> None:
        spec = background_spec(self.root, mode="background_picture", kind="picture")
        spec["elements"][-1]["content"]["crop"]["left"] = 0.01
        self.assertIn("BACKGROUND_FRAMING_INVALID", self._issue_codes(spec))

    def test_background_picture_rejects_wrong_opacity_and_rotation(self) -> None:
        for field, value in (("opacity", 0.99), ("rotation", 1)):
            with self.subTest(field=field):
                spec = background_spec(
                    self.root, mode="background_picture", kind="picture"
                )
                spec["elements"][-1]["style"][field] = value
                self.assertIn(
                    "BACKGROUND_FRAMING_INVALID", self._issue_codes(spec)
                )

    def test_background_picture_rejects_automatic_framing_modes(self) -> None:
        for mode in ("contain", "cover"):
            with self.subTest(mode=mode):
                spec = background_spec(
                    self.root, mode="background_picture", kind="picture"
                )
                spec["elements"][-1]["content"]["mode"] = mode
                self.assertIn(
                    "BACKGROUND_FRAMING_INVALID", self._issue_codes(spec)
                )

    def test_background_picture_rejects_asset_aspect_ratio_change(self) -> None:
        spec = background_spec(self.root, mode="background_picture", kind="picture")
        asset_path = self.root / "wrong-ratio.png"
        Image.new("RGB", (1600, 800), (244, 247, 250)).save(asset_path)
        asset_sha256 = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        spec["elements"][-1]["content"]["asset"] = {
            "path": str(asset_path.resolve()),
            "asset_sha256": asset_sha256,
            "pixel_size": [1600, 800],
        }
        provenance = spec["modules"]["background"]["items"][0][
            "source_provenance"
        ]
        provenance["source_path"] = str(asset_path.resolve())
        provenance["source_sha256"] = asset_sha256
        self.assertIn("BACKGROUND_FRAMING_INVALID", self._issue_codes(spec))

    def test_native_provenance_rejects_missing_file(self) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        provenance = spec["modules"]["background"]["items"][0][
            "source_provenance"
        ]
        provenance["source_path"] = str((self.root / "missing.json").resolve())
        self.assertIn("BACKGROUND_PROVENANCE_INVALID", self._issue_codes(spec))

    def test_native_provenance_rejects_hash_mismatch(self) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        provenance = spec["modules"]["background"]["items"][0][
            "source_provenance"
        ]
        provenance["source_sha256"] = "f" * 64
        self.assertIn("BACKGROUND_PROVENANCE_INVALID", self._issue_codes(spec))

    def test_native_provenance_rejects_symlink(self) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        provenance = spec["modules"]["background"]["items"][0][
            "source_provenance"
        ]
        source = Path(provenance["source_path"])
        link = self.root / "measurement-link.png"
        link.symlink_to(source)
        provenance["source_path"] = str(link)
        self.assertIn("BACKGROUND_PROVENANCE_INVALID", self._issue_codes(spec))

    def test_native_provenance_rejects_symlink_parent(self) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        actual_parent = self.root / "actual-measurements"
        actual_parent.mkdir()
        measurement = actual_parent / "measurement.json"
        measurement.write_text('{"verified": true}\n', encoding="utf-8")
        linked_parent = self.root / "linked-measurements"
        linked_parent.symlink_to(actual_parent, target_is_directory=True)
        provenance = spec["modules"]["background"]["items"][0][
            "source_provenance"
        ]
        provenance["source_path"] = str(linked_parent / measurement.name)
        provenance["source_sha256"] = hashlib.sha256(
            measurement.read_bytes()
        ).hexdigest()
        self.assertIn("BACKGROUND_PROVENANCE_INVALID", self._issue_codes(spec))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_native_provenance_rejects_fifo_without_blocking(self) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        fifo = self.root / "measurement.fifo"
        os.mkfifo(fifo)
        provenance = spec["modules"]["background"]["items"][0][
            "source_provenance"
        ]
        provenance["source_path"] = str(fifo.resolve())
        provenance["source_sha256"] = "0" * 64
        spec_path = self.root / "fifo-spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        child = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from lib.background_contracts import validate_background_prebuild\n"
            "spec = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))\n"
            "issues = validate_background_prebuild(spec)\n"
            "print(json.dumps([[issue.code, issue.path, issue.detail] "
            "for issue in issues]))\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "-c", child, str(SCRIPTS_ROOT), str(spec_path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("validator blocked opening a FIFO without a writer")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(
            [
                "BACKGROUND_PROVENANCE_INVALID",
                "modules.background.items[0].source_provenance.source_path",
                "native measurement source must be a readable local regular file",
            ],
            json.loads(result.stdout),
        )

    def test_native_provenance_fails_closed_without_nonblocking_open(
        self,
    ) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")

        with mock.patch.object(os, "O_NONBLOCK", None, create=True):
            issues = self._contracts().validate_background_prebuild(spec)

        self.assertIn(
            (
                "BACKGROUND_PROVENANCE_INVALID",
                "modules.background.items[0].source_provenance.source_path",
            ),
            {(issue.code, issue.path) for issue in issues},
        )

    def test_native_provenance_fails_closed_when_leaf_is_replaced_during_hash(
        self,
    ) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        provenance = spec["modules"]["background"]["items"][0][
            "source_provenance"
        ]
        source = Path(provenance["source_path"])
        replacement = self.root / "replacement-measurement.png"
        replacement.write_bytes(source.read_bytes())
        real_sha256 = hashlib.sha256
        swapped = False

        class SwapOnFirstUpdate:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self._digest = real_sha256(*args, **kwargs)

            def update(self, payload: bytes) -> None:
                nonlocal swapped
                if not swapped:
                    source.unlink()
                    source.symlink_to(replacement)
                    swapped = True
                self._digest.update(payload)

            def hexdigest(self) -> str:
                return self._digest.hexdigest()

        with mock.patch.object(hashlib, "sha256", SwapOnFirstUpdate):
            issues = self._contracts().validate_background_prebuild(spec)

        self.assertTrue(swapped, "test did not reach the controlled hash race")
        self.assertIn(
            (
                "BACKGROUND_PROVENANCE_INVALID",
                "modules.background.items[0].source_provenance.source_path",
            ),
            {(issue.code, issue.path) for issue in issues},
        )

    def test_native_provenance_fails_closed_when_parent_is_replaced_during_hash(
        self,
    ) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        declared_parent = self.root / "declared-measurements"
        declared_parent.mkdir()
        source = declared_parent / "measurement.json"
        source.write_text('{"verified": true}\n', encoding="utf-8")
        replacement_parent = self.root / "replacement-measurements"
        replacement_parent.mkdir()
        (replacement_parent / source.name).write_bytes(source.read_bytes())
        moved_parent = self.root / "original-measurements"
        provenance = spec["modules"]["background"]["items"][0][
            "source_provenance"
        ]
        provenance["source_path"] = str(source.resolve())
        provenance["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        real_sha256 = hashlib.sha256
        swapped = False

        class SwapOnFirstUpdate:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self._digest = real_sha256(*args, **kwargs)

            def update(self, payload: bytes) -> None:
                nonlocal swapped
                if not swapped:
                    declared_parent.rename(moved_parent)
                    declared_parent.symlink_to(
                        replacement_parent, target_is_directory=True
                    )
                    swapped = True
                self._digest.update(payload)

            def hexdigest(self) -> str:
                return self._digest.hexdigest()

        with mock.patch.object(hashlib, "sha256", SwapOnFirstUpdate):
            issues = self._contracts().validate_background_prebuild(spec)

        self.assertTrue(swapped, "test did not reach the controlled hash race")
        self.assertIn(
            (
                "BACKGROUND_PROVENANCE_INVALID",
                "modules.background.items[0].source_provenance.source_path",
            ),
            {(issue.code, issue.path) for issue in issues},
        )

    def test_native_provenance_rejects_swapped_parent_hash_after_restore(
        self,
    ) -> None:
        contracts = self._contracts()
        spec = background_spec(self.root, mode="native", kind="shape")
        declared_parent = self.root / "declared-measurements"
        declared_parent.mkdir()
        source = declared_parent / "measurement.json"
        original_payload = b'{"source": "original"}\n'
        source.write_bytes(original_payload)
        competitor_parent = self.root / "competitor-measurements"
        competitor_parent.mkdir()
        competitor_source = competitor_parent / source.name
        competitor_payload = b'{"source": "competitor"}\n'
        competitor_source.write_bytes(competitor_payload)
        saved_original = self.root / "saved-original-measurements"
        provenance = spec["modules"]["background"]["items"][0][
            "source_provenance"
        ]
        provenance["source_path"] = str(source.resolve())
        provenance["source_sha256"] = hashlib.sha256(
            competitor_payload
        ).hexdigest()
        self.assertIn("BACKGROUND_PROVENANCE_INVALID", self._issue_codes(spec))

        real_sha256 = hashlib.sha256
        swapped = False
        restored = False

        class SwapParentBeforeContentRead:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                nonlocal swapped
                self._digest = real_sha256(*args, **kwargs)
                if not swapped:
                    declared_parent.rename(saved_original)
                    competitor_parent.rename(declared_parent)
                    swapped = True

            def update(self, payload: bytes) -> None:
                nonlocal restored
                if swapped and not restored:
                    declared_parent.rename(competitor_parent)
                    saved_original.rename(declared_parent)
                    restored = True

                self._digest.update(payload)

            def hexdigest(self) -> str:
                return self._digest.hexdigest()

        with mock.patch.object(hashlib, "sha256", SwapParentBeforeContentRead):
            issues = contracts.validate_background_prebuild(spec)

        self.assertTrue(swapped, "test did not reach the content hash boundary")
        self.assertTrue(restored, "test did not restore the original parent")
        self.assertEqual(original_payload, source.read_bytes())
        self.assertNotEqual(
            provenance["source_sha256"],
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        self.assertIn(
            "BACKGROUND_PROVENANCE_INVALID", {issue.code for issue in issues}
        )

    def test_local_native_light_bands_are_valid(self) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        element_id = "background-light-band"
        source_bbox = [120, 160, 560, 80]
        spec["elements"].append(
            {
                "element_id": element_id,
                "kind": "shape",
                "source_bbox": source_bbox,
                "slide_bbox": [914400, 1219200, 4267200, 609600],
                "layer": 1,
                "editable": True,
                "confidence": "high",
                "style": {
                    "shape_type": "rectangle",
                    "fill": {
                        "type": "solid",
                        "color": "#DDEEFF",
                        "opacity": 0.5,
                    },
                    "line": "noLine",
                    "effects": "none",
                    "rotation": 0,
                },
                "content": {},
            }
        )
        source = spec["clean_visual_reference"]
        spec["modules"]["background"]["items"].append(
            {
                "background_id": "background-002",
                "role": "light_bands",
                "source_bbox": source_bbox,
                "selected_mode": "native",
                "bound_element_id": element_id,
                "source_provenance": {
                    "kind": "native_measurement",
                    "source_path": source["path"],
                    "source_sha256": source["sha256"],
                },
                "reason": "local ambient light band",
                "evidence": [source["path"]],
                "contains_foreground_semantics": False,
            }
        )
        self.assertEqual([], self._contracts().validate_background_prebuild(spec))

    def test_local_background_picture_allows_local_framing(self) -> None:
        spec = background_spec(self.root, mode="native", kind="shape")
        asset_path = self.root / "local-texture.png"
        Image.new("RGB", (320, 120), (220, 225, 235)).save(asset_path)
        asset_sha256 = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        element_id = "background-local-texture"
        source_bbox = [900, 620, 320, 120]
        spec["elements"].append(
            {
                "element_id": element_id,
                "kind": "picture",
                "source_bbox": source_bbox,
                "slide_bbox": [6858000, 4724400, 2438400, 914400],
                "layer": 1,
                "editable": False,
                "confidence": "high",
                "style": {"rotation": 5, "opacity": 0.8},
                "content": {
                    "asset": {
                        "path": str(asset_path.resolve()),
                        "asset_sha256": asset_sha256,
                        "pixel_size": [320, 120],
                    },
                    "mode": "contain",
                    "crop": {"left": 0.05, "top": 0, "right": 0, "bottom": 0},
                },
            }
        )
        spec["modules"]["background"]["items"].append(
            {
                "background_id": "background-002",
                "role": "texture",
                "source_bbox": source_bbox,
                "selected_mode": "background_picture",
                "bound_element_id": element_id,
                "source_provenance": {
                    "kind": "clean_background_asset",
                    "source_path": str(asset_path.resolve()),
                    "source_sha256": asset_sha256,
                },
                "reason": "local ambient texture",
                "evidence": [str(asset_path.resolve())],
                "contains_foreground_semantics": False,
            }
        )
        self.assertEqual([], self._contracts().validate_background_prebuild(spec))

    def test_full_page_non_base_picture_enforces_full_page_framing(self) -> None:
        cases = ("mode", "crop", "ratio")
        for case in cases:
            with self.subTest(case=case):
                spec = background_spec(self.root, mode="native", kind="shape")
                asset_size = (1600, 800) if case == "ratio" else (1600, 900)
                asset_path = self.root / f"full-page-texture-{case}.png"
                Image.new("RGB", asset_size, (215, 225, 235)).save(asset_path)
                asset_sha256 = hashlib.sha256(asset_path.read_bytes()).hexdigest()
                content_mode = "cover" if case == "mode" else "none"
                crop = {
                    "left": 0.01 if case == "crop" else 0,
                    "top": 0,
                    "right": 0,
                    "bottom": 0,
                }
                element_id = f"background-full-texture-{case}"
                source_bbox = [0, 0, 1600, 900]
                spec["elements"].append(
                    {
                        "element_id": element_id,
                        "kind": "picture",
                        "source_bbox": source_bbox,
                        "slide_bbox": [0, 0, 12192000, 6858000],
                        "layer": 1,
                        "editable": False,
                        "confidence": "high",
                        "style": {"rotation": 0, "opacity": 1},
                        "content": {
                            "asset": {
                                "path": str(asset_path.resolve()),
                                "asset_sha256": asset_sha256,
                                "pixel_size": list(asset_size),
                            },
                            "mode": content_mode,
                            "crop": crop,
                        },
                    }
                )
                spec["modules"]["background"]["items"].append(
                    {
                        "background_id": f"background-{case}",
                        "role": "texture",
                        "source_bbox": source_bbox,
                        "selected_mode": "background_picture",
                        "bound_element_id": element_id,
                        "source_provenance": {
                            "kind": "clean_background_asset",
                            "source_path": str(asset_path.resolve()),
                            "source_sha256": asset_sha256,
                        },
                        "reason": "full-page ambient texture",
                        "evidence": [str(asset_path.resolve())],
                        "contains_foreground_semantics": False,
                    }
                )
                self.assertIn(
                    "BACKGROUND_FRAMING_INVALID", self._issue_codes(spec)
                )


if __name__ == "__main__":
    unittest.main()
