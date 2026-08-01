"""Fail-closed element and multipart contracts for schema v2."""

from __future__ import annotations

import copy
import importlib
import math
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixture_specs import make_minimal_spec


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"


def make_shape_element(shape_type: str = "rectangle") -> dict:
    adjustments = [0.25] if shape_type == "roundRect" else []
    return {
        "element_id": "card",
        "kind": "shape",
        "source_bbox": [10, 10, 80, 40],
        "slide_bbox": [100, 100, 800, 400],
        "layer": 1,
        "editable": True,
        "confidence": "high",
        "style": {
            "shape_type": shape_type,
            "adjustments": adjustments,
            "fill": "#FFFFFF",
        },
        "content": {},
    }


def make_repeated_status_element() -> dict:
    return {
        "element_id": "status",
        "kind": "status",
        "source_bbox": [10, 10, 80, 20],
        "slide_bbox": [100, 100, 800, 200],
        "layer": 1,
        "editable": True,
        "confidence": "high",
        "style": {},
        "content": {
            "part_defaults": {"style": {"fill": "#00AA00"}},
            "repeat_sequence": [
                {"part_id": "segment-0", "slide_bbox": [100, 100, 400, 200]},
                {"part_id": "segment-1", "slide_bbox": [500, 100, 400, 200]},
            ],
        },
    }


def make_valid_table_element(element_id: str = "table") -> dict:
    return {
        "element_id": element_id,
        "kind": "table",
        "source_bbox": [10, 10, 80, 40],
        "slide_bbox": [100, 100, 900, 400],
        "layer": 1,
        "editable": True,
        "confidence": "high",
        "style": {"rotation": 0},
        "content": {
            "rows": [400],
            "columns": [900],
            "cells": [
                {
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "text": "cell",
                    "fill": "#FFFFFF",
                    "margins": {"left": 0, "right": 0, "top": 0, "bottom": 0},
                    "alignment": "center",
                    "vertical_alignment": "middle",
                    "font": {
                        "name": "Arial",
                        "size": 12,
                        "weight": 400,
                        "color": "#000000",
                        "italic": False,
                    },
                    "borders": {},
                }
            ],
        },
    }


def complete_multipart_text_style() -> dict:
    return {
        "font_name": "Arial",
        "font_size": 12,
        "font_weight": 400,
        "color": "#FFFFFF",
        "italic": False,
        "alignment": "center",
        "vertical_alignment": "middle",
        "margins": {"left": 0, "right": 0, "top": 0, "bottom": 0},
        "wrap": True,
    }


class ElementContractTests(unittest.TestCase):
    def _api(self):
        scripts_root = str(SCRIPTS_ROOT)
        if scripts_root not in sys.path:
            sys.path.insert(0, scripts_root)
        try:
            module = importlib.import_module("lib.element_contracts")
        except ModuleNotFoundError as exc:
            if exc.name == "lib.element_contracts":
                self.fail("element contracts are not implemented")
            raise
        return (
            module.validate_element_contract,
            module.expand_multipart_parts,
            module.expected_object_types,
        )

    def test_unknown_element_field_fails_closed(self) -> None:
        validate_element_contract, _, _ = self._api()
        element = make_shape_element()
        element["custom_python"] = "build.py"

        issues = validate_element_contract(element)

        self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
        self.assertEqual(issues[0].path, "elements.card")

    def test_shape_alias_is_not_a_supported_capability(self) -> None:
        validate_element_contract, _, _ = self._api()

        issues = validate_element_contract(make_shape_element("rect"))

        self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
        self.assertEqual(issues[0].path, "elements.card.style.shape_type")
        self.assertEqual(issues[0].capability, "shape.rect")

    def test_round_rect_requires_one_adjustment(self) -> None:
        validate_element_contract, _, _ = self._api()
        element = make_shape_element(shape_type="roundRect")
        element["style"]["adjustments"] = []

        issues = validate_element_contract(element)

        self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
        self.assertEqual(issues[0].path, "elements.card.style.adjustments")

    def test_repeat_expansion_is_stable_and_does_not_mutate_input(self) -> None:
        _, expand_multipart_parts, _ = self._api()
        element = make_repeated_status_element()
        before = copy.deepcopy(element)

        first = expand_multipart_parts(element)
        second = expand_multipart_parts(element)

        self.assertEqual(first, second)
        self.assertEqual(element, before)
        self.assertEqual([part["part_id"] for part in first], ["segment-0", "segment-1"])
        self.assertEqual(first[0]["style"], {"fill": "#00AA00"})

    def test_multipart_rejects_overlapping_parts_without_authorization(self) -> None:
        validate_element_contract, _, _ = self._api()
        element = make_repeated_status_element()
        element["content"] = {
            "part_defaults": {},
            "parts": [
                {"part_id": "left", "slide_bbox": [100, 100, 500, 200]},
                {"part_id": "right", "slide_bbox": [500, 100, 400, 200]},
            ],
        }

        issues = validate_element_contract(element)

        self.assertEqual(issues[0].code, "PART_CONTRACT_INVALID")
        self.assertEqual(issues[0].path, "elements.status.content.parts[1].slide_bbox")

    def test_multipart_union_must_equal_parent_bbox(self) -> None:
        validate_element_contract, _, _ = self._api()
        element = make_repeated_status_element()
        element["content"]["repeat_sequence"][1]["slide_bbox"] = [500, 100, 300, 200]

        issues = validate_element_contract(element)

        self.assertEqual(issues[0].code, "PART_CONTRACT_INVALID")
        self.assertEqual(issues[0].path, "elements.status.content")

    def test_multipart_part_text_style_is_explicitly_supported(self) -> None:
        validate_element_contract, expand_multipart_parts, _ = self._api()
        element = make_repeated_status_element()
        element["content"]["part_defaults"]["style"][
            "text_style"
        ] = complete_multipart_text_style()

        self.assertEqual(validate_element_contract(element), [])
        self.assertEqual(
            expand_multipart_parts(element)[0]["style"]["text_style"],
            complete_multipart_text_style(),
        )

    def test_multipart_parts_reject_noncanonical_capability_values(self) -> None:
        validate_element_contract, _, _ = self._api()
        cases = (
            (
                {"shape_type": "rect"},
                "elements.status.content.part_defaults.style.shape_type",
                "shape.rect",
            ),
            (
                {"line": {"dash": "squiggle"}},
                "elements.status.content.part_defaults.style.line.dash",
                "line_dash.squiggle",
            ),
        )
        for style, path, capability in cases:
            with self.subTest(style=style):
                element = make_repeated_status_element()
                element["content"]["part_defaults"]["style"].update(style)

                issues = validate_element_contract(element)

                self.assertTrue(issues, "multipart capability values must fail closed")
                self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
                self.assertEqual(issues[0].path, path)
                self.assertEqual(issues[0].capability, capability)

    def test_table_requires_exact_nonuniform_sizes_and_complete_cell_coverage(self) -> None:
        validate_element_contract, _, _ = self._api()
        element = {
            "element_id": "table",
            "kind": "table",
            "source_bbox": [10, 10, 80, 40],
            "slide_bbox": [100, 100, 900, 400],
            "layer": 1,
            "editable": True,
            "confidence": "high",
            "style": {"rotation": 0},
            "content": {
                "rows": [150, 250],
                "columns": [300, 600],
                "cells": [
                    {
                        "row": 0,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 2,
                        "text": "header",
                        "fill": "noFill",
                        "margins": {"left": 0, "right": 0, "top": 0, "bottom": 0},
                        "alignment": "center",
                        "vertical_alignment": "middle",
                        "font": {
                            "name": "Arial",
                            "size": 12,
                            "weight": 400,
                            "color": "#000000",
                            "italic": False,
                        },
                        "borders": {},
                    },
                    {
                        "row": 1,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "text": "left",
                        "fill": "noFill",
                        "margins": {"left": 0, "right": 0, "top": 0, "bottom": 0},
                        "alignment": "left",
                        "vertical_alignment": "top",
                        "font": {
                            "name": "Arial",
                            "size": 12,
                            "weight": 400,
                            "color": "#000000",
                            "italic": False,
                        },
                        "borders": {},
                    },
                    {
                        "row": 1,
                        "column": 1,
                        "row_span": 1,
                        "column_span": 1,
                        "text": "right",
                        "fill": "noFill",
                        "margins": {"left": 0, "right": 0, "top": 0, "bottom": 0},
                        "alignment": "right",
                        "vertical_alignment": "bottom",
                        "font": {
                            "name": "Arial",
                            "size": 12,
                            "weight": 400,
                            "color": "#000000",
                            "italic": False,
                        },
                        "borders": {},
                    },
                ],
            },
        }

        self.assertEqual(validate_element_contract(element), [])
        broken_size = copy.deepcopy(element)
        broken_size["content"]["columns"][1] = 599
        missing_cell = copy.deepcopy(element)
        missing_cell["content"]["cells"].pop()

        for candidate in (broken_size, missing_cell):
            with self.subTest(candidate=candidate):
                issues = validate_element_contract(candidate)
                self.assertTrue(issues, "invalid table contract must fail closed")
                self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")

        invalid_alignment = copy.deepcopy(element)
        invalid_alignment["content"]["cells"][0]["alignment"] = []
        try:
            issues = validate_element_contract(invalid_alignment)
        except TypeError as exc:
            self.fail(f"table alignment must fail closed: {exc}")
        self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
        self.assertEqual(
            issues[0].path,
            "elements.table.content.cells[0].alignment",
        )

    def test_prebuild_table_bbox_types_fail_closed_without_native_exceptions(self) -> None:
        scripts_root = str(SCRIPTS_ROOT)
        if scripts_root not in sys.path:
            sys.path.insert(0, scripts_root)
        validator = importlib.import_module("validate_reconstruction_spec")
        invalid_values = (None, [], {}, "invalid")

        for index, value in enumerate(invalid_values):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                spec = make_minimal_spec(Path(directory))
                spec["elements"][0] = make_valid_table_element("element-001")
                spec["elements"][0]["slide_bbox"] = value
                spec["modules"]["representation_plan"]["items"][0][
                    "semantic_role"
                ] = "table"
                try:
                    report = validator.validate_spec(spec, stage="prebuild")
                except (TypeError, IndexError, KeyError) as exc:
                    self.fail(f"bbox type leaked native exception: {exc}")

                self.assertFalse(report["valid"])
                self.assertIn(
                    {
                        "code": "INVALID_BBOX",
                        "path": "elements.element-001.slide_bbox",
                        "detail": "bbox must be [x, y, width, height]",
                    },
                    report["errors"],
                )

    def test_public_prebuild_rejects_nested_non_finite_numbers_before_hashing(self) -> None:
        scripts_root = str(SCRIPTS_ROOT)
        if scripts_root not in sys.path:
            sys.path.insert(0, scripts_root)
        validator = importlib.import_module("validate_reconstruction_spec")
        cases = (
            (
                "table-infinity",
                make_valid_table_element("element-001"),
                lambda element: element["content"]["cells"][0]["font"].update(
                    {"size": math.inf}
                ),
                "elements[0].content.cells[0].font.size",
            ),
            (
                "multipart-nan",
                make_repeated_status_element(),
                lambda element: element["content"]["part_defaults"]["style"].update(
                    {"text_style": {**complete_multipart_text_style(), "font_size": math.nan}}
                ),
                "elements[0].content.part_defaults.style.text_style.font_size",
            ),
        )
        for name, element, mutate, expected_path in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                spec = make_minimal_spec(Path(directory))
                spec["elements"][0] = copy.deepcopy(element)
                mutate(spec["elements"][0])
                error: Exception | None = None
                report = None

                try:
                    report = validator.validate_spec(spec, stage="prebuild")
                except Exception as exc:  # behavior assertion owns the exact type
                    error = exc

                self.assertIsNone(error, f"public prebuild leaked {error!r}")
                self.assertIsNotNone(report)
                assert report is not None
                self.assertFalse(report["valid"])
                self.assertIsNone(report["spec_sha256"])
                self.assertEqual(
                    report["errors"],
                    [
                        {
                            "code": "SPEC_NUMBER_NON_FINITE",
                            "path": expected_path,
                            "detail": "number must be finite",
                        }
                    ],
                )

    def test_public_prebuild_preserves_finite_spec_hash_and_boolean_semantics(self) -> None:
        scripts_root = str(SCRIPTS_ROOT)
        if scripts_root not in sys.path:
            sys.path.insert(0, scripts_root)
        validator = importlib.import_module("validate_reconstruction_spec")
        hashing = importlib.import_module("lib.hashing")
        with tempfile.TemporaryDirectory() as directory:
            spec = make_minimal_spec(Path(directory))
            expected_hash = hashing.canonical_json_sha256(spec)

            report = validator.validate_spec(spec, stage="prebuild")

        self.assertEqual(report["spec_sha256"], expected_hash)
        self.assertNotIn(
            "SPEC_NUMBER_NON_FINITE",
            {error["code"] for error in report["errors"]},
        )

    def test_non_finite_report_survives_an_unhashable_invalid_profile(self) -> None:
        scripts_root = str(SCRIPTS_ROOT)
        if scripts_root not in sys.path:
            sys.path.insert(0, scripts_root)
        validator = importlib.import_module("validate_reconstruction_spec")
        with tempfile.TemporaryDirectory() as directory:
            spec = make_minimal_spec(Path(directory))
            spec["verification_profile"] = []
            spec["canvas"]["source_size"][0] = math.inf
            error: Exception | None = None
            report = None

            try:
                report = validator.validate_spec(spec, stage="prebuild")
            except Exception as exc:  # behavior assertion owns the exact type
                error = exc

        self.assertIsNone(error, f"non-finite report leaked {error!r}")
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report["verification_profile"], "strict")
        self.assertEqual(
            report["errors"][0]["path"],
            "canvas.source_size[0]",
        )

    def test_public_prebuild_rejects_a_non_finite_float_subclass(self) -> None:
        scripts_root = str(SCRIPTS_ROOT)
        if scripts_root not in sys.path:
            sys.path.insert(0, scripts_root)
        validator = importlib.import_module("validate_reconstruction_spec")

        class SchemaFloat(float):
            pass

        with tempfile.TemporaryDirectory() as directory:
            spec = make_minimal_spec(Path(directory))
            spec["canvas"]["visual_size"][1] = SchemaFloat("inf")
            error: Exception | None = None
            report = None

            try:
                report = validator.validate_spec(spec, stage="prebuild")
            except Exception as exc:  # behavior assertion owns the exact type
                error = exc

        self.assertIsNone(error, f"float subclass leaked {error!r}")
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report["errors"][0]["code"], "SPEC_NUMBER_NON_FINITE")
        self.assertEqual(report["errors"][0]["path"], "canvas.visual_size[1]")

    def test_table_numeric_and_rotation_bounds_fail_at_exact_paths(self) -> None:
        validate_element_contract, _, _ = self._api()
        cases = (
            ("font-small", ("content", "cells", 0, "font", "size"), 0.5, "elements.table.content.cells[0].font.size"),
            ("font-large", ("content", "cells", 0, "font", "size"), 4000.01, "elements.table.content.cells[0].font.size"),
            ("font-infinite", ("content", "cells", 0, "font", "size"), math.inf, "elements.table.content.cells[0].font.size"),
            ("font-bool", ("content", "cells", 0, "font", "size"), True, "elements.table.content.cells[0].font.size"),
            ("margin-overflow", ("content", "cells", 0, "margins", "left"), 2_147_483_648, "elements.table.content.cells[0].margins.left"),
            ("rotation-full", ("style", "rotation"), 359.999999, "elements.table.style.rotation"),
            ("rotation-360", ("style", "rotation"), 360, "elements.table.style.rotation"),
            ("rotation-collapse", ("style", "rotation"), 0.000001, "elements.table.style.rotation"),
            ("rotation-infinite", ("style", "rotation"), math.inf, "elements.table.style.rotation"),
            ("rotation-bool", ("style", "rotation"), True, "elements.table.style.rotation"),
        )
        for name, path, value, expected_path in cases:
            with self.subTest(name=name):
                element = make_valid_table_element()
                target = element
                for component in path[:-1]:
                    target = target[component]
                target[path[-1]] = value

                issues = validate_element_contract(element)

                self.assertTrue(issues, f"{name} must fail closed")
                self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
                self.assertEqual(issues[0].path, expected_path)

    def test_declared_multipart_text_style_must_be_complete_at_source_path(self) -> None:
        validate_element_contract, _, _ = self._api()
        cases = (
            (
                "defaults-empty",
                lambda element: element["content"]["part_defaults"]["style"].update(
                    {"text_style": {}}
                ),
                "elements.status.content.part_defaults.style.text_style",
            ),
            (
                "explicit-incomplete",
                lambda element: element["content"]["repeat_sequence"][0].update(
                    {"style": {"text_style": {"font_name": "Arial"}}}
                ),
                "elements.status.content.repeat_sequence[0].style.text_style",
            ),
            (
                "explicit-incomplete-over-complete-defaults",
                lambda element: (
                    element["content"]["part_defaults"]["style"].update(
                        {"text_style": complete_multipart_text_style()}
                    ),
                    element["content"]["repeat_sequence"][0].update(
                        {"style": {"text_style": {"color": "#112233"}}}
                    ),
                ),
                "elements.status.content.repeat_sequence[0].style.text_style",
            ),
        )
        for name, mutate, expected_path in cases:
            with self.subTest(name=name):
                element = make_repeated_status_element()
                mutate(element)

                issues = validate_element_contract(element)

                self.assertTrue(issues, "declared text_style must not be ignored")
                self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
                self.assertEqual(issues[0].path, expected_path)

    def test_multipart_numeric_and_rotation_bounds_fail_at_source_paths(self) -> None:
        validate_element_contract, _, _ = self._api()
        cases = (
            ("font-small", "font_size", 0.5, "text_style.font_size"),
            ("font-large", "font_size", 4000.01, "text_style.font_size"),
            ("font-infinite", "font_size", math.inf, "text_style.font_size"),
            ("font-bool", "font_size", True, "text_style.font_size"),
            ("margin-overflow", "margin", 2_147_483_648, "text_style.margins.left"),
            ("rotation-full", "rotation", 359.999999, "rotation"),
            ("rotation-360", "rotation", 360, "rotation"),
            ("rotation-collapse", "rotation", 0.000001, "rotation"),
            ("rotation-infinite", "rotation", math.inf, "rotation"),
            ("rotation-bool", "rotation", True, "rotation"),
        )
        for name, field, value, suffix in cases:
            with self.subTest(name=name):
                element = make_repeated_status_element()
                style = complete_multipart_text_style()
                element["content"]["part_defaults"]["style"].update(
                    {"shape_type": "rectangle", "text_style": style}
                )
                if field == "margin":
                    style["margins"]["left"] = value
                elif field == "rotation":
                    element["content"]["part_defaults"]["style"]["rotation"] = value
                else:
                    style[field] = value

                issues = validate_element_contract(element)

                self.assertTrue(issues, f"{name} must fail closed")
                self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
                self.assertEqual(
                    issues[0].path,
                    f"elements.status.content.part_defaults.style.{suffix}",
                )

    def test_multipart_rejects_unknown_nested_text_style_field(self) -> None:
        validate_element_contract, _, _ = self._api()
        element = make_repeated_status_element()
        element["content"]["part_defaults"]["style"]["text_style"] = {
            "font_name": "Arial",
            "font_size": 12,
            "font_weight": 400,
            "color": "#FFFFFF",
            "italic": False,
            "alignment": "center",
            "vertical_alignment": "middle",
            "rogue": True,
        }

        issues = validate_element_contract(element)

        self.assertTrue(issues, "unknown multipart text_style must fail closed")
        self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
        self.assertIn("text_style", issues[0].path)

    def test_multipart_rejects_explicit_null_text(self) -> None:
        validate_element_contract, _, _ = self._api()
        element = make_repeated_status_element()
        element["content"]["repeat_sequence"][0]["content"] = {"text": None}

        issues = validate_element_contract(element)

        self.assertTrue(issues, "explicit null part text must fail closed")
        self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
        self.assertEqual(
            issues[0].path,
            "elements.status.content.repeat_sequence[0].content.text",
        )

    def test_expected_object_types_are_immutable_and_kind_specific(self) -> None:
        _, _, expected_object_types = self._api()

        self.assertEqual(expected_object_types("table"), frozenset({"graphicFrame"}))
        self.assertEqual(expected_object_types("line"), frozenset({"cxnSp"}))
        self.assertEqual(expected_object_types("unknown"), frozenset())

    def test_prebuild_collects_element_contract_issues(self) -> None:
        scripts_root = str(SCRIPTS_ROOT)
        if scripts_root not in sys.path:
            sys.path.insert(0, scripts_root)
        validator = importlib.import_module("validate_reconstruction_spec")
        with tempfile.TemporaryDirectory() as temporary_directory:
            spec = make_minimal_spec(Path(temporary_directory))
            spec["elements"][0]["custom_python"] = "build.py"

            report = validator.validate_spec(spec, stage="prebuild")

        self.assertFalse(report["valid"])
        self.assertIn(
            {"code": "UNSUPPORTED_CAPABILITY", "path": "elements.element-001", "detail": "unknown fields: custom_python"},
            report["errors"],
        )


if __name__ == "__main__":
    unittest.main()
