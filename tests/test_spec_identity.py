"""Immutable content and review-state identities for schema v2 specs."""

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


class SpecIdentityTests(unittest.TestCase):
    def _api(self):
        scripts_root = str(SCRIPTS_ROOT)
        if scripts_root not in sys.path:
            sys.path.insert(0, scripts_root)
        try:
            return importlib.import_module("lib.spec_identity")
        except ModuleNotFoundError as exc:
            if exc.name == "lib.spec_identity":
                self.fail("shared spec identity module is not implemented")
            raise

    def test_delivery_evidence_does_not_change_content_identity(self) -> None:
        identity = self._api()
        with tempfile.TemporaryDirectory() as directory:
            spec = make_minimal_spec(Path(directory))
            first = identity.content_spec_sha256(spec)
            spec["delivery_status"] = "reviewed_passed"
            spec["runtime_preflight"] = {"path": "/tmp/runtime.json", "sha256": "a" * 64}
            spec["visual_gate"] = {"status": "passed", "evidence": ["overlay.png"], "tripwire": None}
            spec["editability_gate"] = {"status": "passed", "evidence": ["validator.json"]}
            spec["activated_modules"].append("high_risk")
            spec["modules"]["high_risk"] = {"items": [{"risk_id": "p1", "result": "passed"}]}
            self.assertEqual(first, identity.content_spec_sha256(spec))

    def test_renderable_change_invalidates_content_identity(self) -> None:
        identity = self._api()
        with tempfile.TemporaryDirectory() as directory:
            spec = make_minimal_spec(Path(directory))
            first = identity.content_spec_sha256(spec)
            spec["elements"][0]["content"]["text"] = "不同标题"
            self.assertNotEqual(first, identity.content_spec_sha256(spec))

    def test_projection_does_not_mutate_input(self) -> None:
        identity = self._api()
        with tempfile.TemporaryDirectory() as directory:
            spec = make_minimal_spec(Path(directory))
            before = copy.deepcopy(spec)
            identity.build_content_projection(spec)
            self.assertEqual(before, spec)

    def test_content_identity_ignores_only_declared_module_verification_fields(self) -> None:
        identity = self._api()
        with tempfile.TemporaryDirectory() as directory:
            spec = make_minimal_spec(Path(directory))
            spec["modules"]["icons"] = {
                "icons": [{"icon_id": "icon-001", "selectable_picture_verified": False}]
            }
            first = identity.content_spec_sha256(spec)
            spec["modules"]["typography"]["items"][0]["font_declaration_verified"] = True
            spec["modules"]["icons"]["icons"][0]["selectable_picture_verified"] = True
            self.assertEqual(first, identity.content_spec_sha256(spec))

    def test_content_projection_tolerates_non_object_module_members(self) -> None:
        identity = self._api()
        spec = {"modules": {"typography": [], "icons": []}}
        self.assertEqual(identity.build_content_projection(spec), spec)

    def test_content_projection_normalizes_tuple_collections_before_exclusion(self) -> None:
        identity = self._api()
        list_spec = {
            "activated_modules": ["page_layout", "high_risk"],
            "modules": {
                "high_risk": {"items": [{"risk_id": "p1", "result": "passed"}]},
                "typography": {
                    "items": [
                        {"font_declaration_verified": True, "content": "kept"}
                    ]
                },
                "icons": {
                    "icons": [
                        {"selectable_picture_verified": True, "icon_id": "icon-001"}
                    ]
                },
            },
        }
        tuple_spec = copy.deepcopy(list_spec)
        tuple_spec["activated_modules"] = tuple(tuple_spec["activated_modules"])
        tuple_spec["modules"]["typography"]["items"] = tuple(
            tuple_spec["modules"]["typography"]["items"]
        )
        tuple_spec["modules"]["icons"]["icons"] = tuple(
            tuple_spec["modules"]["icons"]["icons"]
        )
        list_before = copy.deepcopy(list_spec)
        tuple_before = copy.deepcopy(tuple_spec)

        self.assertEqual(
            identity.build_content_projection(list_spec),
            identity.build_content_projection(tuple_spec),
        )
        self.assertEqual(
            identity.content_spec_sha256(list_spec),
            identity.content_spec_sha256(tuple_spec),
        )
        self.assertEqual(list_before, list_spec)
        self.assertEqual(tuple_before, tuple_spec)

    def test_content_projection_removes_only_exact_verification_fields(self) -> None:
        identity = self._api()
        spec = {
            "modules": {
                "typography": {
                    "items": [
                        {
                            "font_declaration_verified": True,
                            "font_declaration_verified_note": "keep",
                        }
                    ]
                },
                "icons": {
                    "icons": [
                        {
                            "selectable_picture_verified": True,
                            "selectable_picture_verified_note": "keep",
                        }
                    ]
                },
            }
        }

        projected = identity.build_content_projection(spec)

        typography_item = projected["modules"]["typography"]["items"][0]
        icon_item = projected["modules"]["icons"]["icons"][0]
        self.assertNotIn("font_declaration_verified", typography_item)
        self.assertEqual(typography_item["font_declaration_verified_note"], "keep")
        self.assertNotIn("selectable_picture_verified", icon_item)
        self.assertEqual(icon_item["selectable_picture_verified_note"], "keep")

    def test_review_state_projection_removes_post_review_fields_only(self) -> None:
        identity = self._api()
        spec = {
            "delivery_status": "reviewed_passed",
            "visual_gate": {
                "status": "passed",
                "review_round": 2,
                "review": {"outcome": "passed"},
                "reviewer": {"id": "reviewer-001"},
                "review_admission": {"path": "/tmp/admission.json"},
                "review_invocation": {"path": "/tmp/invocation.json"},
                "review_response_validation": {"path": "/tmp/response.json"},
                "background_contract": {"path": "/tmp/bg", "sha256": "a" * 64},
                "evidence": ["overlay.png"],
            },
        }

        projected = identity.review_state_projection(spec)

        self.assertNotIn("delivery_status", projected)
        for field in identity.POST_REVIEW_VISUAL_FIELDS:
            self.assertNotIn(field, projected["visual_gate"])
        self.assertEqual(
            projected["visual_gate"]["background_contract"],
            {"path": "/tmp/bg", "sha256": "a" * 64},
        )
        self.assertEqual(projected["visual_gate"]["evidence"], ["overlay.png"])

    def test_review_state_keeps_high_risk_but_ignores_reviewer_result(self) -> None:
        identity = self._api()
        with tempfile.TemporaryDirectory() as directory:
            spec = make_minimal_spec(Path(directory))
            spec["activated_modules"].append("high_risk")
            spec["modules"]["high_risk"] = {"items": [{"risk_id": "p1", "result": "passed"}]}
            spec["visual_gate"].update({"background_contract": {"path": "/tmp/bg", "sha256": "e" * 64}})
            before = identity.review_state_sha256(spec)
            spec["delivery_status"] = "reviewed_passed"
            spec["visual_gate"].update({
                "status": "passed",
                "review_round": 2,
                "reviewer": {"admission_id": "a" * 64},
                "review_admission": {"path": "/tmp/admission.json", "sha256": "b" * 64},
                "review_invocation": {"path": "/tmp/invocation.json", "sha256": "c" * 64},
                "review_response_validation": {"path": "/tmp/response.json", "sha256": "d" * 64},
            })
            self.assertEqual(before, identity.review_state_sha256(spec))
            spec["modules"]["high_risk"]["items"][0]["result"] = "changes_required"
            self.assertNotEqual(before, identity.review_state_sha256(spec))

    def test_identity_rejects_non_object_non_finite_and_non_json_values(self) -> None:
        identity = self._api()
        for value in ([], {"number": math.nan}, {"value": object()}):
            for api in (
                identity.build_content_projection,
                identity.content_spec_sha256,
                identity.input_spec_sha256,
                identity.review_state_projection,
                identity.review_state_sha256,
            ):
                with self.subTest(value_type=type(value).__name__, api=api.__name__):
                    with self.assertRaises(identity.ToolError) as raised:
                        api(value)
                    self.assertEqual(raised.exception.code, "SPEC_IDENTITY_INVALID")

    def test_identity_apis_reject_non_utf8_json_strings(self) -> None:
        identity = self._api()
        for api in (
            identity.build_content_projection,
            identity.content_spec_sha256,
            identity.input_spec_sha256,
            identity.review_state_sha256,
        ):
            with self.subTest(api=api.__name__):
                with self.assertRaises(identity.ToolError) as raised:
                    api({"bad": "\ud800"})
                self.assertEqual(raised.exception.code, "SPEC_IDENTITY_INVALID")
