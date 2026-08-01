"""Postbuild regressions for declared background evidence."""

from __future__ import annotations

import copy
import errno
import hashlib
import importlib.util
import json
import subprocess
import sys
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_ROOT / "validate_background_contract.py"
VALIDATOR_PATH = SCRIPTS_ROOT / "validate_pptx.py"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def _load_script(path: Path, module_name: str):
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def _module():
    if not SCRIPT_PATH.is_file():
        raise AssertionError("background postbuild CLI is not implemented")
    return _load_script(SCRIPT_PATH, "test_validate_background_contract_script")


def _validate_structure(
    spec: dict[str, Any], pptx: Path, build: dict[str, Any]
) -> dict[str, Any]:
    validator = _load_script(VALIDATOR_PATH, "background_postbuild_pptx_validator")
    structure = validator.validate_pptx(pptx, 1, spec, build)
    if not structure["valid"]:
        raise AssertionError(f"fixture structure must pass: {structure!r}")
    return structure


def compiled_background_fixture(
    root: Path,
) -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, Any]]:
    """Compile a native base and validate its real structure snapshot."""
    from tests.fixture_specs import make_minimal_spec
    from tests.test_build_pptx_from_spec import compile_fixture

    spec = make_minimal_spec(root / "source")
    pptx, build = compile_fixture(root / "compiled", spec)
    return spec, pptx, build, _validate_structure(spec, pptx, build)


def compiled_picture_background_fixture(
    root: Path,
) -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, Any]]:
    """Compile an independent clean 1600x900 picture as the base."""
    from tests.test_background_contracts import background_spec
    from tests.test_build_pptx_from_spec import compile_fixture

    spec = background_spec(
        root / "source", mode="background_picture", kind="picture"
    )
    pptx, build = compile_fixture(root / "compiled", spec)
    return spec, pptx, build, _validate_structure(spec, pptx, build)


def compiled_local_texture_fixture(
    root: Path,
) -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, Any]]:
    """Compile a native base plus a clean, non-full-slide texture picture."""
    from tests.fixture_specs import make_minimal_spec
    from tests.test_build_pptx_from_spec import compile_fixture

    source_root = root / "source"
    spec = make_minimal_spec(source_root)
    texture_path = source_root / "texture.png"
    Image.new("RGB", (800, 450), (232, 238, 244)).save(texture_path)
    texture_sha256 = hashlib.sha256(texture_path.read_bytes()).hexdigest()
    texture_id = "background-texture"
    spec["elements"].append(
        {
            "element_id": texture_id,
            "kind": "picture",
            "source_bbox": [0, 0, 800, 450],
            "slide_bbox": [0, 0, 6_096_000, 3_429_000],
            "layer": 1,
            "editable": False,
            "confidence": "high",
            "style": {"rotation": 0, "opacity": 1},
            "content": {
                "asset": {
                    "path": str(texture_path.resolve()),
                    "asset_sha256": texture_sha256,
                    "pixel_size": [800, 450],
                },
                "mode": "none",
                "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            },
        }
    )
    spec["regions"][0]["element_ids"].append(texture_id)
    spec["reading_order"].insert(1, texture_id)
    spec["modules"]["background"]["items"].append(
        {
            "background_id": "background-texture-001",
            "role": "texture",
            "source_bbox": [0, 0, 800, 450],
            "selected_mode": "background_picture",
            "bound_element_id": texture_id,
            "source_provenance": {
                "kind": "clean_background_asset",
                "source_path": str(texture_path.resolve()),
                "source_sha256": texture_sha256,
            },
            "reason": "independent local texture layer",
            "evidence": [str(texture_path.resolve())],
            "contains_foreground_semantics": False,
        }
    )
    pptx, build = compile_fixture(root / "compiled", spec)
    return spec, pptx, build, _validate_structure(spec, pptx, build)


class BackgroundPostbuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_native_background_records_bound_expected_and_actual_facts(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual("page-001", report["page_id"])
        self.assertEqual(build["content_spec_sha256"], report["spec_sha256"])
        self.assertEqual(build["input_spec_sha256"], report["input_spec_sha256"])
        self.assertEqual(build["pptx_sha256"], report["pptx_sha256"])
        self.assertRegex(report["build_report_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(report["structure_report_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(report["full_slide_picture_risk"])
        self.assertEqual([], report["errors"])
        self.assertEqual(1, len(report["items"]))
        item = report["items"][0]
        self.assertEqual("background-001", item["background_id"])
        self.assertEqual("background-base", item["bound_element_id"])
        self.assertEqual("native", item["selected_mode"])
        self.assertEqual("ia:background-base", item["expected"]["object_name"])
        self.assertEqual(
            [0, 0, 12_192_000, 6_858_000], item["expected"]["bbox"]
        )
        self.assertEqual(item["expected"]["object_name"], item["actual"]["object_name"])
        self.assertEqual(item["expected"]["bbox"], item["actual"]["bbox"])
        self.assertEqual(item["expected"]["object_type"], item["actual"]["object_type"])
        self.assertIsNone(item["actual"]["media_sha256"])
        self.assertTrue(item["valid"])
        self.assertEqual([], item["errors"])

    def test_native_background_must_be_below_every_foreground_object(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        structure["structure_objects"][0]["layer"] = 99

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "BACKGROUND_NOT_BOTTOM_LAYER",
            {item["code"] for item in report["errors"]},
        )

    def test_background_picture_media_hash_must_match_build_and_pptx(self) -> None:
        spec, pptx, build, structure = compiled_picture_background_fixture(
            self.root
        )
        structure["picture_objects"][0]["media_sha256"] = "f" * 64

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "BACKGROUND_ASSET_INVALID",
            {item["code"] for item in report["errors"]},
        )

    def test_valid_full_slide_picture_remains_explicit_risk_fact(self) -> None:
        spec, pptx, build, structure = compiled_picture_background_fixture(
            self.root
        )

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertTrue(report["valid"], report["errors"])
        self.assertTrue(structure["full_slide_picture_risk"])
        self.assertTrue(report["full_slide_picture_risk"])
        item = report["items"][0]
        self.assertTrue(item["actual"]["full_slide"])
        self.assertEqual(
            build["background_pictures"][0]["media_sha256"],
            item["actual"]["media_sha256"],
        )

    def test_local_background_texture_does_not_require_full_slide_framing(self) -> None:
        spec, pptx, build, structure = compiled_local_texture_fixture(self.root)

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertTrue(report["valid"], report["errors"])
        texture = next(
            item
            for item in report["items"]
            if item["background_id"] == "background-texture-001"
        )
        self.assertFalse(texture["expected"]["full_slide"])
        self.assertFalse(texture["actual"]["full_slide"])

    def test_full_slide_risk_snapshot_cannot_be_silently_cleared(self) -> None:
        spec, pptx, build, structure = compiled_picture_background_fixture(
            self.root
        )
        structure["full_slide_picture_risk"] = False

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertTrue(report["full_slide_picture_risk"])
        self.assertIn(
            "BACKGROUND_ASSET_INVALID",
            {item["code"] for item in report["errors"]},
        )

    def test_undeclared_full_slide_picture_identity_is_blocked(self) -> None:
        spec, pptx, build, structure = compiled_picture_background_fixture(
            self.root
        )
        declared_picture = structure["picture_objects"][0]
        rogue_picture = copy.deepcopy(declared_picture)
        rogue_picture["object_name"] = "ia:undeclared-full-slide"
        rogue_picture["object_id"] = "999"
        rogue_picture["object_key"] = "ppt/slides/slide1.xml#picture-999"
        rogue_picture["layer"] = declared_picture["layer"] + 1
        structure["picture_objects"].append(rogue_picture)
        declared_object = next(
            item
            for item in structure["structure_objects"]
            if item["object_name"] == "ia:background-base"
        )
        rogue_object = copy.deepcopy(declared_object)
        rogue_object["object_name"] = "ia:undeclared-full-slide"
        rogue_object["object_id"] = "999"
        rogue_object["layer"] = rogue_picture["layer"]
        structure["structure_objects"].append(rogue_object)

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "BACKGROUND_FOREGROUND_CONTAMINATION_RISK",
            {item["code"] for item in report["errors"]},
        )

    def test_duplicate_full_slide_picture_identity_is_blocked(self) -> None:
        spec, pptx, build, structure = compiled_picture_background_fixture(
            self.root
        )
        structure["picture_objects"].append(
            copy.deepcopy(structure["picture_objects"][0])
        )

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "BACKGROUND_FOREGROUND_CONTAMINATION_RISK",
            {item["code"] for item in report["errors"]},
        )

    def test_full_slide_declaration_that_fails_matching_is_not_proven(self) -> None:
        spec, pptx, build, structure = compiled_picture_background_fixture(
            self.root
        )
        structure["picture_objects"][0]["media_sha256"] = "f" * 64

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "BACKGROUND_FOREGROUND_CONTAMINATION_RISK",
            {item["code"] for item in report["errors"]},
        )

    def test_structure_object_media_hash_must_match_picture_snapshot(self) -> None:
        spec, pptx, build, structure = compiled_picture_background_fixture(
            self.root
        )
        next(
            item
            for item in structure["structure_objects"]
            if item["object_name"] == "ia:background-base"
        )["media_sha256"] = "f" * 64

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "BACKGROUND_ASSET_INVALID",
            {item["code"] for item in report["errors"]},
        )

    def test_native_structure_media_pollution_is_reported_from_actual_input(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        structure_object = next(
            item
            for item in structure["structure_objects"]
            if item["object_name"] == "ia:background-base"
        )
        structure_object["media_sha256"] = "f" * 64

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertEqual("f" * 64, report["items"][0]["actual"]["media_sha256"])
        self.assertIn(
            "BACKGROUND_ASSET_INVALID",
            {item["code"] for item in report["errors"]},
        )

    def test_native_build_media_pollution_fails_with_empty_actual_media(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        build["elements"]["background-base"]["objects"][0][
            "media_sha256"
        ] = "f" * 64

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIsNone(report["items"][0]["actual"]["media_sha256"])
        self.assertIn(
            "BACKGROUND_ASSET_INVALID",
            {item["code"] for item in report["errors"]},
        )

    def test_native_media_pollution_on_both_sides_is_not_normalized_away(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        structure_object = next(
            item
            for item in structure["structure_objects"]
            if item["object_name"] == "ia:background-base"
        )
        structure_object["media_sha256"] = "f" * 64
        build["elements"]["background-base"]["objects"][0][
            "media_sha256"
        ] = "f" * 64

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertEqual("f" * 64, report["items"][0]["actual"]["media_sha256"])
        self.assertIn(
            "BACKGROUND_ASSET_INVALID",
            {item["code"] for item in report["errors"]},
        )

    def test_native_malformed_media_facts_fail_closed(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        malformed = ["not", "a", "media", "hash"]
        structure_object = next(
            item
            for item in structure["structure_objects"]
            if item["object_name"] == "ia:background-base"
        )
        structure_object["media_sha256"] = malformed
        build["elements"]["background-base"]["objects"][0][
            "media_sha256"
        ] = copy.deepcopy(malformed)

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertEqual(malformed, report["items"][0]["actual"]["media_sha256"])
        self.assertIn(
            "BACKGROUND_ASSET_INVALID",
            {item["code"] for item in report["errors"]},
        )

    def test_malformed_postbuild_field_types_return_structured_invalid_reports(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        cases: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []

        bad_mode = copy.deepcopy(spec)
        bad_mode["modules"]["background"]["items"][0]["selected_mode"] = []
        cases.append(("selected-mode", bad_mode, build, structure))

        bad_binding = copy.deepcopy(spec)
        bad_binding["modules"]["background"]["items"][0][
            "bound_element_id"
        ] = {}
        cases.append(("bound-element", bad_binding, build, structure))

        for field, value in (
            ("object_name", []),
            ("layer", {}),
            ("visible", None),
        ):
            malformed_structure = copy.deepcopy(structure)
            malformed_structure["structure_objects"][0][field] = value
            cases.append((f"structure-{field}", spec, build, malformed_structure))

        malformed_builds: list[tuple[str, Any]] = [
            ("elements", None),
            ("background_pictures", {}),
        ]
        for field, value in malformed_builds:
            malformed_build = copy.deepcopy(build)
            malformed_build[field] = value
            cases.append((f"build-{field}", spec, malformed_build, structure))

        for field, value in (
            ("semantic_kind", []),
            ("selected_mode", {}),
            ("objects", None),
        ):
            malformed_build = copy.deepcopy(build)
            malformed_build["elements"]["background-base"][field] = value
            cases.append((f"build-element-{field}", spec, malformed_build, structure))

        for name, candidate_spec, candidate_build, candidate_structure in cases:
            with self.subTest(name=name):
                report = _module().validate_background_postbuild(
                    candidate_spec, pptx, candidate_build, candidate_structure
                )
                self.assertFalse(report["valid"])
                self.assertIn(
                    "BACKGROUND_ASSET_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_non_utf8_canonical_input_returns_structured_invalid_report(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        build["elements"]["background-base"]["selected_mode"] = "\ud800"

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "BACKGROUND_ASSET_INVALID",
            {item["code"] for item in report["errors"]},
        )

    def test_build_mode_fact_must_match_the_background_declaration(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        build["elements"]["background-base"][
            "selected_mode"
        ] = "background_picture"

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "BACKGROUND_ASSET_INVALID",
            {item["code"] for item in report["errors"]},
        )

    def test_extra_build_background_picture_fact_fails_closed(self) -> None:
        spec, pptx, build, structure = compiled_picture_background_fixture(
            self.root
        )
        extra = copy.deepcopy(build["background_pictures"][0])
        extra["background_id"] = "undeclared-background"
        build["background_pictures"].append(extra)

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "BACKGROUND_ASSET_INVALID",
            {item["code"] for item in report["errors"]},
        )

    def test_incomplete_or_stale_build_background_fields_fail_closed(self) -> None:
        spec, pptx, original, structure = compiled_picture_background_fixture(
            self.root
        )
        malformed_picture = copy.deepcopy(original)
        malformed_picture["background_pictures"].append("not-an-object")
        malformed_object = copy.deepcopy(original)
        malformed_object["elements"]["background-base"]["objects"].append(
            "not-an-object"
        )
        stale_declaration = copy.deepcopy(original)
        stale_declaration["background_pictures"][0]["reason"] = "stale"

        for name, build in (
            ("picture-item", malformed_picture),
            ("object-item", malformed_object),
            ("declaration", stale_declaration),
        ):
            with self.subTest(name=name):
                report = _module().validate_background_postbuild(
                    spec, pptx, build, structure
                )
                self.assertFalse(report["valid"])
                self.assertIn(
                    "BACKGROUND_ASSET_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_multipart_background_object_is_not_accepted_as_one_object(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        extra_structure = copy.deepcopy(structure["structure_objects"][0])
        extra_structure["object_name"] = "ia:background-base:part-2"
        extra_structure["layer"] = 2
        structure["structure_objects"].append(extra_structure)
        extra_build = copy.deepcopy(
            build["elements"]["background-base"]["objects"][0]
        )
        extra_build["ooxml_name"] = "ia:background-base:part-2"
        build["elements"]["background-base"]["objects"].append(extra_build)

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "BACKGROUND_ASSET_INVALID",
            {item["code"] for item in report["errors"]},
        )

    def test_unreadable_spec_still_hashes_other_available_inputs(self) -> None:
        _spec, pptx, build, structure = compiled_background_fixture(self.root)

        report = _module().validate_background_postbuild(
            self.root / "missing-spec.json", pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertEqual(build["pptx_sha256"], report["pptx_sha256"])
        self.assertRegex(report["build_report_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(report["structure_report_sha256"], r"^[0-9a-f]{64}$")

    def test_report_identity_is_canonical_while_file_identity_tracks_bytes(
        self,
    ) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        build_pretty = self.root / "build-pretty.json"
        build_compact = self.root / "build-compact.json"
        structure_pretty = self.root / "structure-pretty.json"
        structure_compact = self.root / "structure-compact.json"
        build_pretty.write_text(
            json.dumps(build, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        build_compact.write_text(
            json.dumps(build, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        structure_pretty.write_text(
            json.dumps(structure, ensure_ascii=False, indent=4, sort_keys=False),
            encoding="utf-8",
        )
        structure_compact.write_text(
            json.dumps(
                structure,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        from_dict = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )
        from_pretty = _module().validate_background_postbuild(
            spec, pptx, build_pretty, structure_pretty
        )
        from_compact = _module().validate_background_postbuild(
            spec, pptx, build_compact, structure_compact
        )

        self.assertTrue(from_dict["valid"], from_dict["errors"])
        self.assertEqual(
            from_dict["build_report_sha256"], from_pretty["build_report_sha256"]
        )
        self.assertEqual(
            from_pretty["build_report_sha256"], from_compact["build_report_sha256"]
        )
        self.assertEqual(
            from_dict["structure_report_sha256"],
            from_pretty["structure_report_sha256"],
        )
        self.assertEqual(
            from_pretty["structure_report_sha256"],
            from_compact["structure_report_sha256"],
        )
        self.assertIsNone(from_dict["build_report_file_sha256"])
        self.assertIsNone(from_dict["structure_report_file_sha256"])
        self.assertEqual(
            hashlib.sha256(build_pretty.read_bytes()).hexdigest(),
            from_pretty["build_report_file_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(build_compact.read_bytes()).hexdigest(),
            from_compact["build_report_file_sha256"],
        )
        self.assertNotEqual(
            from_pretty["build_report_file_sha256"],
            from_compact["build_report_file_sha256"],
        )

    def test_invalid_report_files_preserve_available_raw_digest(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        malformed = self.root / "malformed.json"
        malformed.write_bytes(b'{"valid":')
        non_object = self.root / "non-object.json"
        non_object.write_text("[]", encoding="utf-8")

        malformed_report = _module().validate_background_postbuild(
            spec, pptx, malformed, structure
        )
        non_object_report = _module().validate_background_postbuild(
            spec, pptx, build, non_object
        )
        unreadable_report = _module().validate_background_postbuild(
            spec, pptx, self.root / "missing.json", structure
        )

        self.assertFalse(malformed_report["valid"])
        self.assertIsNone(malformed_report["build_report_sha256"])
        self.assertEqual(
            hashlib.sha256(malformed.read_bytes()).hexdigest(),
            malformed_report["build_report_file_sha256"],
        )
        self.assertFalse(non_object_report["valid"])
        self.assertIsNone(non_object_report["structure_report_sha256"])
        self.assertEqual(
            hashlib.sha256(non_object.read_bytes()).hexdigest(),
            non_object_report["structure_report_file_sha256"],
        )
        self.assertFalse(unreadable_report["valid"])
        self.assertIsNone(unreadable_report["build_report_sha256"])
        self.assertIsNone(unreadable_report["build_report_file_sha256"])

    def test_missing_duplicate_bbox_and_type_facts_fail_closed(self) -> None:
        spec, pptx, build, original = compiled_background_fixture(self.root)
        background = next(
            item
            for item in original["structure_objects"]
            if item["object_name"] == "ia:background-base"
        )
        cases = []
        missing = copy.deepcopy(original)
        missing["structure_objects"] = [
            item
            for item in missing["structure_objects"]
            if item["object_name"] != "ia:background-base"
        ]
        cases.append(("missing", missing))
        duplicate = copy.deepcopy(original)
        duplicate["structure_objects"].append(copy.deepcopy(background))
        cases.append(("duplicate", duplicate))
        bbox = copy.deepcopy(original)
        bbox_record = next(
            item
            for item in bbox["structure_objects"]
            if item["object_name"] == "ia:background-base"
        )
        bbox_record.setdefault(
            "bbox",
            [
                bbox_record["x"],
                bbox_record["y"],
                bbox_record["cx"],
                bbox_record["cy"],
            ],
        )[0] += 1
        cases.append(("bbox", bbox))
        object_type = copy.deepcopy(original)
        next(
            item
            for item in object_type["structure_objects"]
            if item["object_name"] == "ia:background-base"
        )["object_type"] = "pic"
        cases.append(("type", object_type))

        for name, structure in cases:
            with self.subTest(name=name):
                report = _module().validate_background_postbuild(
                    spec, pptx, build, structure
                )
                self.assertFalse(report["valid"])
                self.assertIn(
                    "BACKGROUND_ASSET_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_structure_snapshot_must_exactly_match_current_pptx_objects(self) -> None:
        spec, pptx, build, original = compiled_background_fixture(self.root)
        foreground = next(
            item
            for item in original["structure_objects"]
            if item["object_name"] != "ia:background-base"
        )
        cases: list[tuple[str, dict[str, Any]]] = []

        missing = copy.deepcopy(original)
        missing["structure_objects"] = [
            item
            for item in missing["structure_objects"]
            if item["object_name"] != foreground["object_name"]
        ]
        cases.append(("missing-foreground", missing))

        hidden = copy.deepcopy(original)
        next(
            item
            for item in hidden["structure_objects"]
            if item["object_name"] == foreground["object_name"]
        )["visible"] = False
        cases.append(("changed-visible", hidden))

        duplicate = copy.deepcopy(original)
        duplicate["structure_objects"].append(copy.deepcopy(foreground))
        cases.append(("duplicate-foreground", duplicate))

        for name, structure in cases:
            with self.subTest(name=name):
                report = _module().validate_background_postbuild(
                    spec, pptx, build, structure
                )
                self.assertFalse(report["valid"])
                self.assertIn(
                    "BACKGROUND_ASSET_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_structure_snapshot_closes_all_trusted_identity_and_state_fields(
        self,
    ) -> None:
        spec, pptx, build, original = compiled_background_fixture(self.root)
        foreground_index = next(
            index
            for index, item in enumerate(original["structure_objects"])
            if item["object_name"] != "ia:background-base"
        )
        mutations = (
            ("object_id", "forged-id"),
            ("slide_part", "ppt/slides/slide999.xml"),
            ("hidden", True),
            ("geometry_known", False),
        )

        for field, value in mutations:
            with self.subTest(field=field):
                structure = copy.deepcopy(original)
                structure["structure_objects"][foreground_index][field] = value

                report = _module().validate_background_postbuild(
                    spec, pptx, build, structure
                )

                self.assertFalse(report["valid"])
                self.assertIn(
                    "BACKGROUND_ASSET_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_picture_snapshot_must_close_one_to_one_with_current_pptx(self) -> None:
        spec, pptx, build, original = compiled_picture_background_fixture(
            self.root
        )
        cases: list[tuple[str, dict[str, Any]]] = []

        missing_picture_fact = copy.deepcopy(original)
        missing_picture_fact["picture_objects"] = []
        cases.append(("picture-side-deletion", missing_picture_fact))

        missing_structure_fact = copy.deepcopy(original)
        missing_structure_fact["structure_objects"] = [
            item
            for item in missing_structure_fact["structure_objects"]
            if item["object_type"] != "pic"
        ]
        cases.append(("structure-side-deletion", missing_structure_fact))

        orphan = copy.deepcopy(original)
        orphan_picture = copy.deepcopy(orphan["picture_objects"][0])
        orphan_picture.update(
            {
                "object_name": "ia:orphan-local-picture",
                "object_id": "999",
                "object_key": "ppt/slides/slide1.xml#picture-999",
                "layer": 99,
                "x": 100,
                "y": 100,
                "cx": 100,
                "cy": 100,
                "bbox": [100, 100, 100, 100],
                "full_slide": False,
            }
        )
        orphan["picture_objects"].append(orphan_picture)
        orphan_structure = copy.deepcopy(
            next(
                item
                for item in orphan["structure_objects"]
                if item["object_type"] == "pic"
            )
        )
        orphan_structure.update(
            {
                "object_name": "ia:orphan-local-picture",
                "object_id": "999",
                "layer": 99,
                "x": 100,
                "y": 100,
                "cx": 100,
                "cy": 100,
                "bbox": [100, 100, 100, 100],
            }
        )
        orphan["structure_objects"].append(orphan_structure)
        cases.append(("non-full-orphan", orphan))

        for name, structure in cases:
            with self.subTest(name=name):
                report = _module().validate_background_postbuild(
                    spec, pptx, build, structure
                )
                self.assertFalse(report["valid"])
                self.assertIn(
                    "BACKGROUND_ASSET_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_picture_snapshot_closes_trusted_identity_and_source_fields(self) -> None:
        spec, pptx, build, original = compiled_picture_background_fixture(
            self.root
        )
        mutations = (
            ("object_key", "ppt/slides/slide1.xml#picture-999"),
            ("object_id", "999"),
            ("slide_part", "ppt/slides/slide999.xml"),
            ("slide_position", 999),
            ("relationship_id", "rId999"),
            ("media_part", "ppt/media/forged.png"),
            ("media_basename", "forged.png"),
            ("hidden", True),
            ("geometry_known", False),
        )

        for field, value in mutations:
            with self.subTest(field=field):
                structure = copy.deepcopy(original)
                structure["picture_objects"][0][field] = value

                report = _module().validate_background_postbuild(
                    spec, pptx, build, structure
                )

                self.assertFalse(report["valid"])
                self.assertIn(
                    "BACKGROUND_ASSET_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_stale_spec_pptx_build_or_structure_identity_fails_closed(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        stale_spec = copy.deepcopy(spec)
        stale_spec["elements"][0]["content"]["text"] = "stale"
        stale_pptx = self.root / "stale.pptx"
        stale_pptx.write_bytes(pptx.read_bytes() + b"stale")
        stale_build = copy.deepcopy(build)
        stale_build["pptx_sha256"] = "f" * 64
        stale_structure = copy.deepcopy(structure)
        stale_structure["pptx_sha256"] = "f" * 64

        cases = (
            ("spec", stale_spec, pptx, build, structure),
            ("pptx", spec, stale_pptx, build, structure),
            ("build", spec, pptx, stale_build, structure),
            ("structure", spec, pptx, build, stale_structure),
        )
        for name, candidate_spec, candidate_pptx, candidate_build, candidate_structure in cases:
            with self.subTest(name=name):
                report = _module().validate_background_postbuild(
                    candidate_spec,
                    candidate_pptx,
                    candidate_build,
                    candidate_structure,
                )
                self.assertFalse(report["valid"])
                self.assertTrue(report["errors"])

    def test_foreground_contamination_declaration_is_not_accepted_postbuild(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        spec["modules"]["background"]["items"][0][
            "contains_foreground_semantics"
        ] = True

        report = _module().validate_background_postbuild(
            spec, pptx, build, structure
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "BACKGROUND_FOREGROUND_CONTAMINATION_RISK",
            {item["code"] for item in report["errors"]},
        )

    def test_cli_writes_complete_invalid_report_and_returns_two(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        structure["pptx_sha256"] = "f" * 64
        spec_path = self.root / "spec.json"
        build_path = self.root / "build.json"
        structure_path = self.root / "structure.json"
        output = self.root / "background-contract.json"
        for path, payload in (
            (spec_path, spec),
            (build_path, build),
            (structure_path, structure),
        ):
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                str(spec_path),
                "--pptx",
                str(pptx),
                "--build-report",
                str(build_path),
                "--structure-report",
                str(structure_path),
                "--output",
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, completed.returncode, completed.stderr)
        self.assertTrue(output.is_file())
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(payload["valid"])
        self.assertTrue(payload["errors"])
        self.assertEqual(
            hashlib.sha256(build_path.read_bytes()).hexdigest(),
            payload["build_report_file_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(structure_path.read_bytes()).hexdigest(),
            payload["structure_report_file_sha256"],
        )

    def test_cli_malformed_typed_input_publishes_invalid_report_without_traceback(
        self,
    ) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        spec["modules"]["background"]["items"][0]["selected_mode"] = []
        spec_path = self.root / "spec.json"
        build_path = self.root / "build.json"
        structure_path = self.root / "structure.json"
        output = self.root / "background-contract.json"
        for path, payload in (
            (spec_path, spec),
            (build_path, build),
            (structure_path, structure),
        ):
            path.write_text(json.dumps(payload), encoding="utf-8")

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                str(spec_path),
                "--pptx",
                str(pptx),
                "--build-report",
                str(build_path),
                "--structure-report",
                str(structure_path),
                "--output",
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, completed.returncode)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertTrue(output.is_file())
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(report["valid"])
        self.assertTrue(report["errors"])

    def test_cli_existing_output_returns_two_without_overwrite(self) -> None:
        spec, pptx, build, structure = compiled_background_fixture(self.root)
        spec_path = self.root / "spec.json"
        build_path = self.root / "build.json"
        structure_path = self.root / "structure.json"
        output = self.root / "background-contract.json"
        for path, payload in (
            (spec_path, spec),
            (build_path, build),
            (structure_path, structure),
        ):
            path.write_text(json.dumps(payload), encoding="utf-8")
        sentinel = b"do-not-overwrite\n"
        output.write_bytes(sentinel)

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                str(spec_path),
                "--pptx",
                str(pptx),
                "--build-report",
                str(build_path),
                "--structure-report",
                str(structure_path),
                "--output",
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, completed.returncode)
        self.assertEqual(sentinel, output.read_bytes())
        self.assertTrue(completed.stderr.startswith("{"), completed.stderr)
        self.assertEqual(
            "BUILD_OUTPUT_INCOMPLETE", json.loads(completed.stderr)["code"]
        )

    def test_publisher_rejects_symlink_parent_and_final_without_overwrite(
        self,
    ) -> None:
        module = _module()
        real_parent = self.root / "real"
        real_parent.mkdir()
        symlink_parent = self.root / "linked"
        symlink_parent.symlink_to(real_parent, target_is_directory=True)
        target = self.root / "target.json"
        target.write_bytes(b"sentinel\n")
        final_symlink = real_parent / "report.json"
        final_symlink.symlink_to(target)

        for name, output in (
            ("parent", symlink_parent / "new.json"),
            ("final", final_symlink),
        ):
            with self.subTest(name=name):
                with self.assertRaises(module.ToolError):
                    module._publish_json_no_overwrite(output, {"valid": False})
        self.assertEqual(b"sentinel\n", target.read_bytes())
        self.assertFalse((real_parent / "new.json").exists())

    def test_publisher_normalizes_link_failures_and_never_overwrites_racer(
        self,
    ) -> None:
        module = _module()
        output = self.root / "report.json"
        original_link = module.os.link

        def race_link(*args: Any, **kwargs: Any) -> None:
            output.write_bytes(b"racer\n")
            raise FileExistsError(errno.EEXIST, "exists")

        with mock.patch.object(module.os, "link", side_effect=race_link):
            with self.assertRaises(module.ToolError):
                module._publish_json_no_overwrite(output, {"valid": False})
        self.assertEqual(b"racer\n", output.read_bytes())
        self.assertEqual([], list(self.root.glob(".report.json.*.tmp")))

        output.unlink()
        for name, failure in (
            ("cross-device", OSError(errno.EXDEV, "cross device")),
            ("general", OSError(errno.EIO, "io error")),
        ):
            with self.subTest(name=name):
                with mock.patch.object(module.os, "link", side_effect=failure):
                    with self.assertRaises(module.ToolError):
                        module._publish_json_no_overwrite(
                            output, {"valid": False}
                        )
                self.assertFalse(output.exists())
                self.assertEqual([], list(self.root.glob(".report.json.*.tmp")))
        self.assertIsNotNone(original_link)

    def test_first_directory_fsync_failure_preserves_visible_destination(
        self,
    ) -> None:
        module = _module()
        output = self.root / "report.json"
        original_fsync = module.os.fsync
        directory_calls = 0

        def fail_first_directory_fsync(fd: int) -> None:
            nonlocal directory_calls
            if stat.S_ISDIR(module.os.fstat(fd).st_mode):
                directory_calls += 1
                if directory_calls == 1:
                    raise OSError(errno.EIO, "directory fsync failed")
            original_fsync(fd)

        with mock.patch.object(
            module.os, "fsync", side_effect=fail_first_directory_fsync
        ):
            with self.assertRaises(module.ToolError) as raised:
                module._publish_json_no_overwrite(output, {"valid": False})

        self.assertEqual({"valid": False}, json.loads(output.read_text("utf-8")))
        self.assertEqual([], list(self.root.glob(".report.json.*.tmp")))
        self.assertEqual(2, directory_calls)
        self.assertIn("directory fsync failed", raised.exception.detail)
        self.assertIn(
            "destination visible/preserved; durability or ownership uncertain",
            raised.exception.detail,
        )
        self.assertIn(
            "temporary name removed; cleanup directory fsync completed",
            raised.exception.detail,
        )

    def test_directory_close_failure_keeps_first_fsync_cleanup_detail(
        self,
    ) -> None:
        module = _module()
        output = self.root / "report.json"
        original_close = module.os.close
        original_fsync = module.os.fsync
        directory_calls = 0

        def fail_first_directory_fsync(fd: int) -> None:
            nonlocal directory_calls
            if stat.S_ISDIR(module.os.fstat(fd).st_mode):
                directory_calls += 1
                if directory_calls == 1:
                    raise OSError(errno.EIO, "first directory fsync failed")
            original_fsync(fd)

        def close_then_fail_directory(fd: int) -> None:
            is_directory = stat.S_ISDIR(module.os.fstat(fd).st_mode)
            original_close(fd)
            if is_directory:
                raise OSError(errno.EIO, "directory close failed")

        with (
            mock.patch.object(
                module.os, "fsync", side_effect=fail_first_directory_fsync
            ),
            mock.patch.object(
                module.os, "close", side_effect=close_then_fail_directory
            ),
            self.assertRaises(module.ToolError) as raised,
        ):
            module._publish_json_no_overwrite(output, {"valid": False})

        self.assertEqual({"valid": False}, json.loads(output.read_text("utf-8")))
        self.assertEqual([], list(self.root.glob(".report.json.*.tmp")))
        self.assertEqual(2, directory_calls)
        self.assertIn("first directory fsync failed", raised.exception.detail)
        self.assertIn(
            "temporary name removed; cleanup directory fsync completed",
            raised.exception.detail,
        )
        self.assertIn("directory close failed", raised.exception.detail)
        self.assertIsInstance(raised.exception.__cause__, module.ToolError)
        self.assertIn(
            "first directory fsync failed",
            raised.exception.__cause__.detail,
        )
        self.assertIsInstance(raised.exception.__cause__.__cause__, OSError)
        self.assertIn(
            "first directory fsync failed",
            str(raised.exception.__cause__.__cause__),
        )

    def test_directory_close_failure_keeps_post_link_temp_residue_detail(
        self,
    ) -> None:
        module = _module()
        output = self.root / "report.json"
        original_close = module.os.close
        original_fsync = module.os.fsync
        original_unlink = module.os.unlink
        directory_calls = 0

        def fail_first_directory_fsync(fd: int) -> None:
            nonlocal directory_calls
            if stat.S_ISDIR(module.os.fstat(fd).st_mode):
                directory_calls += 1
                if directory_calls == 1:
                    raise OSError(errno.EIO, "first directory fsync failed")
            original_fsync(fd)

        def fail_temp_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
            if str(path).startswith(".report.json."):
                raise OSError(errno.EIO, "cleanup unlink failed")
            original_unlink(path, *args, **kwargs)

        def close_then_fail_directory(fd: int) -> None:
            is_directory = stat.S_ISDIR(module.os.fstat(fd).st_mode)
            original_close(fd)
            if is_directory:
                raise OSError(errno.EIO, "directory close failed")

        with (
            mock.patch.object(
                module.os, "fsync", side_effect=fail_first_directory_fsync
            ),
            mock.patch.object(module.os, "unlink", side_effect=fail_temp_unlink),
            mock.patch.object(
                module.os, "close", side_effect=close_then_fail_directory
            ),
            self.assertRaises(module.ToolError) as raised,
        ):
            module._publish_json_no_overwrite(output, {"valid": False})

        residues = list(self.root.glob(".report.json.*.tmp"))
        self.assertEqual({"valid": False}, json.loads(output.read_text("utf-8")))
        self.assertEqual(1, len(residues))
        self.assertEqual(2, directory_calls)
        self.assertIn("first directory fsync failed", raised.exception.detail)
        self.assertIn("cleanup unlink failed", raised.exception.detail)
        self.assertIn(residues[0].name, raised.exception.detail)
        self.assertIn("cleanup directory fsync completed", raised.exception.detail)
        self.assertIn("directory close failed", raised.exception.detail)
        original_unlink(residues[0])

    def test_publisher_keeps_complete_output_if_second_directory_fsync_fails(
        self,
    ) -> None:
        module = _module()
        output = self.root / "report.json"
        original_fsync = module.os.fsync
        directory_calls = 0

        def fail_second_directory_fsync(fd: int) -> None:
            nonlocal directory_calls
            if stat.S_ISDIR(module.os.fstat(fd).st_mode):
                directory_calls += 1
                if directory_calls == 2:
                    raise OSError(errno.EIO, "cleanup fsync failed")
            original_fsync(fd)

        with mock.patch.object(
            module.os, "fsync", side_effect=fail_second_directory_fsync
        ):
            with self.assertRaises(module.ToolError):
                module._publish_json_no_overwrite(output, {"valid": False})

        self.assertEqual({"valid": False}, json.loads(output.read_text("utf-8")))
        self.assertEqual([], list(self.root.glob(".report.json.*.tmp")))

    def test_publisher_defines_complete_residue_when_temp_unlink_fails(self) -> None:
        module = _module()
        output = self.root / "report.json"
        original_unlink = module.os.unlink

        def fail_temp_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
            if str(path).startswith(".report.json."):
                raise OSError(errno.EIO, "unlink failed")
            original_unlink(path, *args, **kwargs)

        with mock.patch.object(module.os, "unlink", side_effect=fail_temp_unlink):
            with self.assertRaises(module.ToolError):
                module._publish_json_no_overwrite(output, {"valid": False})

        self.assertEqual({"valid": False}, json.loads(output.read_text("utf-8")))
        residues = list(self.root.glob(".report.json.*.tmp"))
        self.assertEqual(1, len(residues))
        original_unlink(residues[0])

    def test_uncommitted_write_and_unlink_failures_disclose_exact_temp_residue(
        self,
    ) -> None:
        module = _module()
        output = self.root / "report.json"
        original_unlink = module.os.unlink

        def fail_temp_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
            if str(path).startswith(".report.json."):
                raise OSError(errno.EIO, "cleanup unlink failed")
            original_unlink(path, *args, **kwargs)

        with (
            mock.patch.object(
                module.os,
                "write",
                side_effect=OSError(errno.ENOSPC, "primary write failed"),
            ),
            mock.patch.object(
                module.os, "unlink", side_effect=fail_temp_unlink
            ),
            self.assertRaises(module.ToolError) as raised,
        ):
            module._publish_json_no_overwrite(output, {"valid": False})

        residues = list(self.root.glob(".report.json.*.tmp"))
        self.assertEqual(1, len(residues))
        self.assertFalse(output.exists())
        self.assertIn("primary write failed", raised.exception.detail)
        self.assertIn("cleanup unlink failed", raised.exception.detail)
        self.assertIn(residues[0].name, raised.exception.detail)
        original_unlink(residues[0])

    def test_uncommitted_link_and_cleanup_fsync_failures_disclose_uncertainty(
        self,
    ) -> None:
        module = _module()
        output = self.root / "report.json"
        original_fsync = module.os.fsync

        def fail_cleanup_directory_fsync(fd: int) -> None:
            if stat.S_ISDIR(module.os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "cleanup directory fsync failed")
            original_fsync(fd)

        with (
            mock.patch.object(
                module.os,
                "link",
                side_effect=OSError(errno.EXDEV, "primary link failed"),
            ),
            mock.patch.object(
                module.os, "fsync", side_effect=fail_cleanup_directory_fsync
            ),
            self.assertRaises(module.ToolError) as raised,
        ):
            module._publish_json_no_overwrite(output, {"valid": False})

        self.assertFalse(output.exists())
        self.assertEqual([], list(self.root.glob(".report.json.*.tmp")))
        self.assertIn("primary link failed", raised.exception.detail)
        self.assertIn("cleanup directory fsync failed", raised.exception.detail)
        self.assertIn(".report.json.", raised.exception.detail)
        self.assertIn("cleanup durability is unconfirmed", raised.exception.detail)

    def test_post_link_identity_mismatch_preserves_competing_destination(self) -> None:
        module = _module()
        output = self.root / "report.json"
        competitor = b"competing destination\n"
        original_stat = module.os.stat
        original_replace = module.os.replace
        destination_stats = 0

        def replace_before_identity_stat(
            path: Any, *args: Any, **kwargs: Any
        ) -> os.stat_result:
            nonlocal destination_stats
            if path == output.name and kwargs.get("dir_fd") is not None:
                destination_stats += 1
                if destination_stats == 2:
                    candidate = self.root / "competitor.json"
                    candidate.write_bytes(competitor)
                    original_replace(candidate, output)
            return original_stat(path, *args, **kwargs)

        with (
            mock.patch.object(module.os, "stat", side_effect=replace_before_identity_stat),
            self.assertRaises(module.ToolError) as raised,
        ):
            module._publish_json_no_overwrite(output, {"valid": False})

        self.assertEqual(competitor, output.read_bytes())
        self.assertIn("competing destination", raised.exception.detail)
        self.assertIn(
            "destination visible/preserved; durability or ownership uncertain",
            raised.exception.detail,
        )
        self.assertIn(
            "temporary name removed; cleanup directory fsync completed",
            raised.exception.detail,
        )
        self.assertEqual([], list(self.root.glob(".report.json.*.tmp")))

    def test_first_directory_fsync_failure_preserves_competing_destination(
        self,
    ) -> None:
        module = _module()
        output = self.root / "report.json"
        competitor = b"competitor before failed fsync\n"
        original_fsync = module.os.fsync
        original_replace = module.os.replace
        directory_calls = 0

        def replace_then_fail_first_directory_fsync(fd: int) -> None:
            nonlocal directory_calls
            if stat.S_ISDIR(module.os.fstat(fd).st_mode):
                directory_calls += 1
                if directory_calls == 1:
                    candidate = self.root / "competitor.json"
                    candidate.write_bytes(competitor)
                    original_replace(candidate, output)
                    raise OSError(errno.EIO, "first directory fsync failed")
            original_fsync(fd)

        with (
            mock.patch.object(
                module.os,
                "fsync",
                side_effect=replace_then_fail_first_directory_fsync,
            ),
            self.assertRaises(module.ToolError) as raised,
        ):
            module._publish_json_no_overwrite(output, {"valid": False})

        self.assertEqual(competitor, output.read_bytes())
        self.assertEqual(2, directory_calls)
        self.assertIn("first directory fsync failed", raised.exception.detail)
        self.assertIn("competing destination", raised.exception.detail)
        self.assertIn(
            "destination visible/preserved; durability or ownership uncertain",
            raised.exception.detail,
        )
        self.assertIn(
            "temporary name removed; cleanup directory fsync completed",
            raised.exception.detail,
        )
        self.assertEqual([], list(self.root.glob(".report.json.*.tmp")))

    def test_rollback_stat_to_unlink_window_never_deletes_competitor(
        self,
    ) -> None:
        module = _module()
        output = self.root / "report.json"
        competitor = b"competitor after owned rollback stat\n"
        original_fsync = module.os.fsync
        original_replace = module.os.replace
        original_stat = module.os.stat
        directory_calls = 0
        destination_stats = 0
        competitor_installed = False

        def fail_first_directory_fsync(fd: int) -> None:
            nonlocal directory_calls
            if stat.S_ISDIR(module.os.fstat(fd).st_mode):
                directory_calls += 1
                if directory_calls == 1:
                    raise OSError(errno.EIO, "first directory fsync failed")
            original_fsync(fd)

        def replace_after_owned_rollback_stat(
            path: Any, *args: Any, **kwargs: Any
        ) -> os.stat_result:
            nonlocal competitor_installed, destination_stats
            if path != output.name or kwargs.get("dir_fd") is None:
                return original_stat(path, *args, **kwargs)
            destination_stats += 1
            current = original_stat(path, *args, **kwargs)
            if destination_stats == 3:
                candidate = self.root / "competitor.json"
                candidate.write_bytes(competitor)
                original_replace(candidate, output)
                competitor_installed = True
            return current

        with (
            mock.patch.object(
                module.os, "fsync", side_effect=fail_first_directory_fsync
            ),
            mock.patch.object(
                module.os,
                "stat",
                side_effect=replace_after_owned_rollback_stat,
            ),
            self.assertRaises(module.ToolError) as raised,
        ):
            module._publish_json_no_overwrite(output, {"valid": False})

        self.assertTrue(competitor_installed)
        self.assertTrue(output.exists(), raised.exception.detail)
        self.assertEqual(competitor, output.read_bytes())
        self.assertEqual([], list(self.root.glob(".report.json.*.tmp")))
        self.assertIn(
            "destination visible/preserved; durability or ownership uncertain",
            raised.exception.detail,
        )

    def test_replacement_after_first_directory_fsync_is_detected_and_preserved(
        self,
    ) -> None:
        module = _module()
        output = self.root / "report.json"
        competitor = b"competitor after successful fsync\n"
        original_fsync = module.os.fsync
        original_replace = module.os.replace
        directory_calls = 0

        def replace_after_first_directory_fsync(fd: int) -> None:
            nonlocal directory_calls
            original_fsync(fd)
            if stat.S_ISDIR(module.os.fstat(fd).st_mode):
                directory_calls += 1
                if directory_calls == 1:
                    candidate = self.root / "competitor.json"
                    candidate.write_bytes(competitor)
                    original_replace(candidate, output)

        with (
            mock.patch.object(
                module.os,
                "fsync",
                side_effect=replace_after_first_directory_fsync,
            ),
            self.assertRaises(module.ToolError) as raised,
        ):
            module._publish_json_no_overwrite(output, {"valid": False})

        self.assertEqual(competitor, output.read_bytes())
        self.assertIn("competing destination", raised.exception.detail)
        self.assertIn(
            "destination visible/preserved; durability or ownership uncertain",
            raised.exception.detail,
        )
        self.assertIn(
            "temporary name removed; cleanup directory fsync completed",
            raised.exception.detail,
        )
        self.assertEqual([], list(self.root.glob(".report.json.*.tmp")))


if __name__ == "__main__":
    unittest.main()
