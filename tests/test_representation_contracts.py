from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image
from tests.fixture_specs import make_asset_fallback_spec, make_minimal_spec


class RepresentationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _validate_representation_plan(self):
        try:
            from lib.representation_contracts import validate_representation_plan
        except ModuleNotFoundError as exc:
            if exc.name == "lib.representation_contracts":
                self.fail("representation contracts are not implemented")
            raise
        return validate_representation_plan

    def test_required_fact_cannot_be_not_applicable(self) -> None:
        validate_representation_plan = self._validate_representation_plan()
        spec = make_minimal_spec(self.root)
        item = spec["modules"]["representation_plan"]["items"][0]
        item.update(
            {
                "required": True,
                "coverage_status": "not_applicable",
                "selected_mode": None,
            }
        )

        issues = validate_representation_plan(spec)

        self.assertEqual(issues[0].code, "REPRESENTATION_INCOMPLETE")

    def test_full_editability_forbids_asset(self) -> None:
        validate_representation_plan = self._validate_representation_plan()
        spec = make_minimal_spec(self.root)
        item = spec["modules"]["representation_plan"]["items"][0]
        item.update({"selected_mode": "asset", "required_editability": "full"})

        issues = validate_representation_plan(spec)

        self.assertEqual(issues[0].code, "REPRESENTATION_FALLBACK_FORBIDDEN")

    def test_null_mode_only_allows_optional_not_applicable(self) -> None:
        validate_representation_plan = self._validate_representation_plan()
        spec = make_minimal_spec(self.root)
        item = spec["modules"]["representation_plan"]["items"][0]
        item.update(
            {
                "required": False,
                "coverage_status": "not_applicable",
                "selected_mode": None,
                "bound_element_ids": [],
                "evidence": [str(self.root / "representation-evidence.json")],
            }
        )

        self.assertEqual(validate_representation_plan(spec), [])

    def test_empty_evidence_is_incomplete(self) -> None:
        validate_representation_plan = self._validate_representation_plan()
        spec = make_minimal_spec(self.root)
        item = spec["modules"]["representation_plan"]["items"][0]
        item["evidence"] = []

        issues = validate_representation_plan(spec)

        self.assertEqual(issues[0].code, "REPRESENTATION_INCOMPLETE")

    def test_representation_plan_rejects_unknown_container_field(self) -> None:
        validate_representation_plan = self._validate_representation_plan()
        spec = make_minimal_spec(self.root)
        spec["modules"]["representation_plan"]["unexpected"] = True

        issues = validate_representation_plan(spec)

        self.assertTrue(issues, "unknown representation_plan fields must fail closed")
        self.assertEqual(issues[0].code, "REPRESENTATION_INCOMPLETE")
        self.assertEqual(issues[0].path, "modules.representation_plan")

    def test_unhashable_selected_mode_is_reported_not_raised(self) -> None:
        validate_representation_plan = self._validate_representation_plan()
        spec = make_minimal_spec(self.root)
        spec["modules"]["representation_plan"]["items"][0]["selected_mode"] = []

        issues = validate_representation_plan(spec)

        self.assertEqual(issues[0].code, "REPRESENTATION_INCOMPLETE")

    def test_mode_map_and_summary_reflect_bound_fact_modes(self) -> None:
        try:
            from lib.representation_contracts import (
                element_mode_map,
                representation_summary,
            )
        except ModuleNotFoundError as exc:
            if exc.name == "lib.representation_contracts":
                self.fail("representation contracts are not implemented")
            raise
        spec = make_minimal_spec(self.root)

        self.assertEqual(element_mode_map(spec), {"element-001": "native"})
        self.assertEqual(
            representation_summary(spec),
            {"asset": 0, "composite": 0, "native": 1, "not_applicable": 0},
        )

        from lib.background_contracts import resolved_element_mode_map

        self.assertEqual(
            resolved_element_mode_map(spec),
            {"background-base": "native", "element-001": "native"},
        )

    def test_labels_only_asset_requires_editable_text_binding(self) -> None:
        validate_representation_plan = self._validate_representation_plan()
        spec = make_asset_fallback_spec(
            self.root, required_editability="labels_only"
        )
        fact = spec["modules"]["representation_plan"]["items"][-1]
        fact["bound_element_ids"] = ["artwork-picture"]

        issues = validate_representation_plan(spec)

        self.assertTrue(issues, "labels_only asset without text must fail closed")
        self.assertEqual(issues[0].code, "REPRESENTATION_INCOMPLETE")

    def test_asset_picture_bbox_must_equal_source_fact_bbox(self) -> None:
        validate_representation_plan = self._validate_representation_plan()
        spec = make_asset_fallback_spec(
            self.root, required_editability="none"
        )
        spec["elements"][-1]["source_bbox"][2] += 1

        issues = validate_representation_plan(spec)

        self.assertTrue(issues, "asset bbox drift must fail closed")
        self.assertEqual(issues[0].code, "REPRESENTATION_INCOMPLETE")

    def test_asset_fact_maps_only_editable_labels_to_native_mode(self) -> None:
        from lib.representation_contracts import element_mode_map

        spec = make_asset_fallback_spec(
            self.root, required_editability="labels_only"
        )

        self.assertEqual(validate := self._validate_representation_plan()(spec), [])
        self.assertEqual(
            element_mode_map(spec),
            {"artwork-picture": "asset", "element-001": "native"},
        )

    def test_none_asset_allows_no_text_binding(self) -> None:
        spec = make_asset_fallback_spec(
            self.root, required_editability="none"
        )

        self.assertEqual(self._validate_representation_plan()(spec), [])

    def test_asset_forbids_geometry_editability(self) -> None:
        for editability in ("full", "labels_and_geometry"):
            with self.subTest(editability=editability):
                spec = make_asset_fallback_spec(
                    self.root / editability,
                    required_editability=editability,
                )

                issues = self._validate_representation_plan()(spec)

                self.assertTrue(issues)
                self.assertEqual(
                    issues[0].code, "REPRESENTATION_FALLBACK_FORBIDDEN"
                )

    def test_asset_requires_exactly_one_picture_or_icon(self) -> None:
        spec = make_asset_fallback_spec(
            self.root, required_editability="none"
        )
        picture = copy.deepcopy(spec["elements"][-1])
        picture["element_id"] = "artwork-picture-2"
        picture["source_bbox"] = [400, 140, 240, 180]
        picture["slide_bbox"] = [3048000, 1066800, 1828800, 1371600]
        spec["elements"].append(picture)
        spec["regions"][0]["element_ids"].append("artwork-picture-2")
        spec["reading_order"].append("artwork-picture-2")
        fact = spec["modules"]["representation_plan"]["items"][-1]
        fact["bound_element_ids"].append("artwork-picture-2")

        issues = self._validate_representation_plan()(spec)

        self.assertTrue(issues, "multiple asset pictures must fail closed")
        self.assertEqual(issues[0].code, "REPRESENTATION_INCOMPLETE")

    def test_asset_requires_a_picture_or_icon_binding(self) -> None:
        spec = make_asset_fallback_spec(
            self.root, required_editability="labels_only"
        )
        fact = spec["modules"]["representation_plan"]["items"][-1]
        fact["bound_element_ids"] = ["element-001"]

        issues = self._validate_representation_plan()(spec)

        self.assertTrue(issues, "asset fact without a picture must fail closed")
        self.assertEqual(issues[0].code, "REPRESENTATION_INCOMPLETE")

    def test_asset_unknown_and_duplicate_bindings_fail_closed(self) -> None:
        for name, bindings in (
            ("unknown", ["artwork-picture", "missing-label"]),
            ("duplicate", ["artwork-picture", "artwork-picture"]),
        ):
            with self.subTest(name=name):
                root = self.root / name
                spec = make_asset_fallback_spec(
                    root, required_editability="none"
                )
                fact = spec["modules"]["representation_plan"]["items"][-1]
                fact["bound_element_ids"] = bindings

                issues = self._validate_representation_plan()(spec)

                self.assertTrue(issues)
                self.assertEqual(issues[0].code, "REPRESENTATION_INCOMPLETE")
                self.assertEqual(
                    issues[0].path,
                    "modules.representation_plan.items[1].bound_element_ids",
                )

    def test_asset_rejects_non_label_non_picture_binding(self) -> None:
        spec = make_asset_fallback_spec(
            self.root, required_editability="none"
        )
        shape = copy.deepcopy(spec["elements"][0])
        shape.update(
            {
                "element_id": "unexpected-shape",
                "kind": "shape",
                "style": {},
                "content": {},
            }
        )
        spec["elements"].append(shape)
        fact = spec["modules"]["representation_plan"]["items"][-1]
        fact["bound_element_ids"].append("unexpected-shape")

        issues = self._validate_representation_plan()(spec)

        self.assertTrue(issues, "wrong-kind asset binding must fail closed")
        self.assertEqual(issues[0].code, "REPRESENTATION_INCOMPLETE")

    def test_labels_only_asset_rejects_noneditable_text(self) -> None:
        spec = make_asset_fallback_spec(
            self.root, required_editability="labels_only"
        )
        spec["elements"][0]["editable"] = False

        issues = self._validate_representation_plan()(spec)

        self.assertTrue(issues, "noneditable label must fail closed")
        self.assertEqual(issues[0].code, "REPRESENTATION_INCOMPLETE")

    def test_asset_rejects_invalid_media_identity(self) -> None:
        for field, value, expected_code in (
            ("asset_sha256", "0" * 64, "ASSET_HASH_MISMATCH"),
            ("pixel_size", [15, 16], "UNSUPPORTED_CAPABILITY"),
            (
                "path",
                str((self.root / "missing.png").resolve()),
                "UNSUPPORTED_CAPABILITY",
            ),
        ):
            with self.subTest(field=field):
                root = self.root / field
                spec = make_asset_fallback_spec(
                    root, required_editability="none"
                )
                spec["elements"][-1]["content"]["asset"][field] = value

                issues = self._validate_representation_plan()(spec)

                self.assertTrue(issues, "invalid asset identity must fail closed")
                self.assertEqual(issues[0].code, expected_code)

    def test_asset_hash_io_failure_returns_stable_issue(self) -> None:
        from lib import representation_contracts

        for name, failure in (
            ("os-error", OSError("asset disappeared")),
            ("value-error", ValueError("embedded null character")),
        ):
            with self.subTest(name=name):
                root = self.root / name
                spec = make_asset_fallback_spec(
                    root, required_editability="none"
                )
                raised = None
                issues = []
                with mock.patch.object(
                    representation_contracts,
                    "file_sha256",
                    side_effect=failure,
                ):
                    try:
                        issues = (
                            representation_contracts.validate_representation_plan(
                                spec
                            )
                        )
                    except Exception as exc:  # assertion owns public behavior
                        raised = exc

                self.assertIsNone(
                    raised, "asset hash I/O must return a contract issue"
                )
                self.assertTrue(issues)
                self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")

    def test_asset_decoder_failures_return_stable_issue(self) -> None:
        from lib import representation_contracts

        for name, failure in (
            ("value", ValueError("invalid dimensions")),
            ("bomb", Image.DecompressionBombError("too large")),
        ):
            with self.subTest(name=name):
                root = self.root / name
                spec = make_asset_fallback_spec(
                    root, required_editability="none"
                )
                raised = None
                issues = []
                with mock.patch.object(
                    representation_contracts.Image,
                    "open",
                    side_effect=failure,
                ):
                    try:
                        issues = (
                            representation_contracts.validate_representation_plan(
                                spec
                            )
                        )
                    except Exception as exc:  # assertion owns public behavior
                        raised = exc

                self.assertIsNone(
                    raised, "asset decoder failures must return a contract issue"
                )
                self.assertTrue(issues)
                self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")

    def test_asset_rejects_full_page_fallback(self) -> None:
        spec = make_asset_fallback_spec(
            self.root, required_editability="none"
        )
        page_bbox = list(spec["canvas"]["page_frame_bbox"])
        fact = spec["modules"]["representation_plan"]["items"][-1]
        fact["source_bbox"] = list(page_bbox)
        spec["elements"][-1]["source_bbox"] = list(page_bbox)

        issues = self._validate_representation_plan()(spec)

        self.assertTrue(issues, "full-page asset fallback must fail closed")
        self.assertEqual(
            issues[0].code, "REPRESENTATION_FALLBACK_FORBIDDEN"
        )

    def test_asset_path_must_be_literal_absolute_without_user_expansion(self) -> None:
        spec = make_asset_fallback_spec(
            self.root, required_editability="none"
        )
        asset = spec["elements"][-1]["content"]["asset"]
        absolute = Path(asset["path"])
        asset["path"] = "~/artwork.png"

        with mock.patch.object(Path, "expanduser", return_value=absolute):
            issues = self._validate_representation_plan()(spec)

        self.assertTrue(issues, "tilde path must not be expanded")
        self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
        self.assertEqual(
            issues[0].path, "elements.artwork-picture.content.asset.path"
        )
        self.assertEqual(issues[0].capability, "picture.asset.local_hash")

    def test_unknown_user_path_returns_stable_issue(self) -> None:
        spec = make_asset_fallback_spec(
            self.root, required_editability="none"
        )
        spec["elements"][-1]["content"]["asset"][
            "path"
        ] = "~ia-user-that-does-not-exist/asset.png"
        raised = None
        issues = []
        try:
            issues = self._validate_representation_plan()(spec)
        except Exception as exc:  # assertion below owns public behavior
            raised = exc

        self.assertIsNone(raised, "unknown-user path must not leak RuntimeError")
        self.assertTrue(issues)
        self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
        self.assertEqual(
            issues[0].path, "elements.artwork-picture.content.asset.path"
        )

    def test_asset_path_rejects_embedded_nul_without_native_exception(self) -> None:
        for name, raw_path in (
            ("leading", "\x00/private/tmp/bad.png"),
            ("middle", "/private/tmp/bad\x00name.png"),
            ("trailing", "/private/tmp/bad.png\x00"),
            ("multiple", "/private/\x00tmp/bad\x00name.png"),
        ):
            with self.subTest(name=name):
                root = self.root / name
                spec = make_asset_fallback_spec(
                    root, required_editability="none"
                )
                spec["elements"][-1]["content"]["asset"]["path"] = raw_path
                raised = None
                issues = []
                try:
                    issues = self._validate_representation_plan()(spec)
                except Exception as exc:  # assertion below owns public behavior
                    raised = exc

                self.assertIsNone(
                    raised, "embedded NUL must not leak a native exception"
                )
                self.assertTrue(issues)
                self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
                self.assertEqual(
                    issues[0].path,
                    "elements.artwork-picture.content.asset.path",
                )
                self.assertEqual(
                    issues[0].capability, "picture.asset.local_hash"
                )
                self.assertEqual(
                    issues[0].detail,
                    "asset path must not contain NUL characters",
                )

    def test_asset_path_metadata_failures_return_stable_issue(self) -> None:
        from lib import representation_contracts

        for method, failure in (
            ("is_symlink", OSError("stat failed")),
            ("is_symlink", ValueError("invalid path")),
            ("is_file", OSError("stat failed")),
            ("is_file", ValueError("invalid path")),
            ("resolve", RuntimeError("resolution failed")),
            ("resolve", ValueError("invalid path")),
        ):
            with self.subTest(
                method=method, failure_type=type(failure).__name__
            ):
                root = self.root / method
                spec = make_asset_fallback_spec(
                    root, required_editability="none"
                )
                raised = None
                issues = []
                with mock.patch.object(Path, method, side_effect=failure):
                    try:
                        issues = (
                            representation_contracts.validate_representation_plan(
                                spec
                            )
                        )
                    except Exception as exc:  # assertion owns public behavior
                        raised = exc

                self.assertIsNone(
                    raised, "path metadata failures must return a contract issue"
                )
                self.assertTrue(issues)
                self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
                self.assertEqual(
                    issues[0].path,
                    "elements.artwork-picture.content.asset.path",
                )
                self.assertEqual(
                    issues[0].capability, "picture.asset.local_hash"
                )

    def test_asset_boundaries_preserve_existing_tool_errors(self) -> None:
        from lib import representation_contracts
        from lib.error_codes import ToolError

        sentinel = ToolError(
            "SENTINEL",
            "sentinel.path",
            "sentinel detail",
            "sentinel.capability",
        )
        load_image = mock.MagicMock()
        load_image.__enter__.return_value.format = "PNG"
        load_image.__enter__.return_value.load.side_effect = sentinel
        cases = (
            (
                "path-construction",
                mock.patch.object(
                    representation_contracts.Path,
                    "__new__",
                    side_effect=sentinel,
                ),
            ),
            (
                "path-metadata",
                mock.patch.object(Path, "is_symlink", side_effect=sentinel),
            ),
            (
                "hash",
                mock.patch.object(
                    representation_contracts,
                    "file_sha256",
                    side_effect=sentinel,
                ),
            ),
            (
                "image-open",
                mock.patch.object(
                    representation_contracts.Image,
                    "open",
                    side_effect=sentinel,
                ),
            ),
            (
                "image-load",
                mock.patch.object(
                    representation_contracts.Image,
                    "open",
                    return_value=load_image,
                ),
            ),
        )
        for name, patcher in cases:
            with self.subTest(name=name):
                root = self.root / f"sentinel-{name}"
                spec = make_asset_fallback_spec(
                    root, required_editability="none"
                )
                with patcher:
                    issues = (
                        representation_contracts.validate_representation_plan(
                            spec
                        )
                    )

                self.assertTrue(issues)
                self.assertEqual(issues[0].as_dict(), sentinel.as_dict())

    def test_asset_near_full_page_threshold_is_fail_closed(self) -> None:
        cases = (
            ("exact", [0, 0, 1600, 900], True),
            ("one-pixel-inset", [0, 0, 1599, 899], True),
            ("margin-boundary", [16, 9, 1568, 882], True),
            ("nonzero-near-full", [8, 4, 1584, 892], True),
            ("outside-margin-threshold", [17, 9, 1566, 882], False),
            ("local-artwork", [120, 140, 240, 180], False),
        )
        for name, bbox, forbidden in cases:
            with self.subTest(name=name):
                root = self.root / name
                spec = make_asset_fallback_spec(
                    root, required_editability="none"
                )
                fact = spec["modules"]["representation_plan"]["items"][-1]
                fact["source_bbox"] = list(bbox)
                spec["elements"][-1]["source_bbox"] = list(bbox)

                issues = self._validate_representation_plan()(spec)

                if forbidden:
                    self.assertTrue(issues, f"{name} must be forbidden")
                    self.assertEqual(
                        issues[0].code, "REPRESENTATION_FALLBACK_FORBIDDEN"
                    )
                else:
                    self.assertEqual(issues, [])

    def test_near_full_page_predicate_rejects_invalid_geometry(self) -> None:
        from lib import geometry
        from lib.error_codes import ToolError

        predicate = getattr(geometry, "is_near_full_page_bbox", None)
        self.assertTrue(
            callable(predicate), "shared near-full-page predicate is required"
        )
        assert callable(predicate)
        for bbox in (
            [0, 0, 0, 900],
            [0, 0, True, 900],
            [0, 0, float("nan"), 900],
        ):
            with self.subTest(bbox=bbox):
                with self.assertRaises(ToolError) as raised:
                    predicate(bbox, [0, 0, 1600, 900])
                self.assertEqual(raised.exception.code, "INVALID_BBOX")

    def test_asset_rejects_disguised_media_container(self) -> None:
        for name, extension, encoded_format in (
            ("gif-as-png", ".png", "GIF"),
            ("png-as-jpg", ".jpg", "PNG"),
        ):
            with self.subTest(name=name):
                root = self.root / name
                spec = make_asset_fallback_spec(
                    root, required_editability="none"
                )
                fake = root / f"disguised{extension}"
                Image.new("RGB", (16, 16), "#336699").save(
                    fake, format=encoded_format
                )
                asset = spec["elements"][-1]["content"]["asset"]
                asset["path"] = str(fake.resolve())
                asset["asset_sha256"] = hashlib.sha256(
                    fake.read_bytes()
                ).hexdigest()

                issues = self._validate_representation_plan()(spec)

                self.assertTrue(issues, "container/extension mismatch must fail")
                self.assertEqual(issues[0].code, "UNSUPPORTED_CAPABILITY")
                self.assertEqual(
                    issues[0].path,
                    "elements.artwork-picture.content.asset.path",
                )
                self.assertEqual(
                    issues[0].capability, "picture.asset.local_hash"
                )

    def test_asset_accepts_matching_jpeg_container(self) -> None:
        spec = make_asset_fallback_spec(
            self.root, required_editability="none"
        )
        jpeg = self.root / "artwork.jpeg"
        Image.new("RGB", (16, 16), "#336699").save(jpeg, format="JPEG")
        asset = spec["elements"][-1]["content"]["asset"]
        asset["path"] = str(jpeg.resolve())
        asset["asset_sha256"] = hashlib.sha256(jpeg.read_bytes()).hexdigest()

        self.assertEqual(self._validate_representation_plan()(spec), [])
