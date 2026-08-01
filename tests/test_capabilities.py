"""Capability registry contracts for the schema v2 compiler."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"


class CapabilityTests(unittest.TestCase):
    def _api(self):
        """Import the target inside a test body so missing code is a failure."""
        scripts_root = str(SCRIPTS_ROOT)
        if scripts_root not in sys.path:
            sys.path.insert(0, scripts_root)
        try:
            capabilities = importlib.import_module("lib.capabilities")
            errors = importlib.import_module("lib.error_codes")
            hashing = importlib.import_module("lib.hashing")
        except ModuleNotFoundError:
            self.fail("capability module is not implemented")
        return capabilities, errors.ToolError, hashing.canonical_json_sha256

    def test_canonical_shape_types_reject_rect_alias(self) -> None:
        capabilities, tool_error, _ = self._api()

        with self.assertRaises(tool_error) as raised:
            capabilities.require_supported_value(
                "shape_type", "rect", "elements[0].style.shape_type"
            )

        self.assertEqual(raised.exception.code, "UNSUPPORTED_CAPABILITY")
        self.assertEqual(raised.exception.capability, "shape.rect")

    def test_non_string_values_return_stable_tool_errors(self) -> None:
        capabilities, tool_error, _ = self._api()

        for value in ([], {}):
            with self.subTest(value=value), self.assertRaises(tool_error) as raised:
                capabilities.require_supported_value(
                    "shape_type", value, "elements[0].style.shape_type"
                )

            self.assertEqual(raised.exception.code, "UNSUPPORTED_CAPABILITY")
            self.assertEqual(raised.exception.path, "elements[0].style.shape_type")

    def test_line_arrow_values_use_the_shared_capability_registry(self) -> None:
        capabilities, tool_error, _ = self._api()

        try:
            capabilities.require_supported_value(
                "line_arrow", "triangle", "elements[0].style.head_arrow"
            )
        except tool_error as exc:
            self.fail(f"line arrow capability is not implemented: {exc}")
        with self.assertRaises(tool_error) as raised:
            capabilities.require_supported_value(
                "line_arrow", "not-a-drawingml-arrow", "elements[0].style.head_arrow"
            )

        self.assertEqual(raised.exception.code, "UNSUPPORTED_CAPABILITY")

    def test_manifest_is_deterministic_and_contains_v1_kinds(self) -> None:
        capabilities, _, canonical_json_sha256 = self._api()

        first = capabilities.capability_manifest()
        second = capabilities.capability_manifest()

        self.assertEqual(first, second)
        self.assertEqual(
            set(first["buildable_kinds"]),
            {"text", "shape", "line", "table", "matrix", "status", "picture", "icon"},
        )
        self.assertEqual(
            capabilities.capability_manifest_sha256(), canonical_json_sha256(first)
        )

    def test_right_arrow_capability_is_aligned_with_shape_renderer(self) -> None:
        capabilities, _, _ = self._api()
        builder = importlib.import_module("pptx_builder")

        canonical_shapes = frozenset(
            capabilities.capability_manifest()["canonical_values"]["shape_type"]
        )

        self.assertEqual(
            builder.SHAPE_RENDERER.supported_values["shape_type"],
            canonical_shapes,
        )
        self.assertIn("rightArrow", canonical_shapes)
        self.assertIn("shape.rightArrow", builder.SHAPE_RENDERER.capability_ids)

    def test_manifest_declares_complete_text_run_capabilities(self) -> None:
        capabilities, _, _ = self._api()

        manifest = capabilities.capability_manifest()

        self.assertIn("atomic_capabilities", manifest)
        self.assertTrue(
            {
                "text.run.font",
                "text.run.font_size",
                "text.run.bold",
                "text.run.color",
                "text.run.italic",
                "text.run.underline",
                "text.run.strike",
                "text.run.baseline",
                "text.run.letter_spacing",
                "text.paragraph.native_bullet",
                "text.frame.no_autofit",
                "text.frame.margins",
                "text.frame.vertical_alignment",
                "text.frame.wrap",
            }.issubset(set(manifest["atomic_capabilities"]))
        )

    def test_manifest_declares_attempt_8_pre_review_workflow_capabilities(self) -> None:
        capabilities, _, _ = self._api()

        manifest = capabilities.capability_manifest()

        self.assertEqual(
            manifest["workflow_capabilities"],
            [
                "workflow.background_contract.v1",
                "workflow.rendered_text_geometry.v1",
                "workflow.review_admission.v1",
            ],
        )

    def test_visual_gate_schema_exposes_optional_attempt_8_artifact_identities(self) -> None:
        capabilities, _, _ = self._api()
        schema_contracts = importlib.import_module("lib.schema_contracts")

        visual_gate = schema_contracts.json_schema_document()["$defs"]["VisualGate"]
        new_fields = {
            "background_contract",
            "rendered_text_geometry",
            "review_admission",
            "review_invocation",
            "review_response_validation",
        }

        self.assertTrue(new_fields.issubset(visual_gate["properties"]))
        self.assertTrue(new_fields.isdisjoint(visual_gate["required"]))
        self.assertFalse(visual_gate["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
