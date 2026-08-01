"""Behavior contracts for deterministic build reports and pair publication."""

from __future__ import annotations

import contextlib
import copy
import errno
import hashlib
import importlib
import importlib.metadata
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Callable
from unittest import mock
from xml.etree import ElementTree as ET

from PIL import Image

from tests.fixture_specs import make_minimal_spec, write_valid_fixture
from tests.test_build_pptx_from_spec import (
    _append_primitive,
    make_icon_spec,
    make_merged_table_spec,
    make_native_list_spec,
    make_picture_spec,
    make_shape_spec,
    make_status_spec,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from lib.capabilities import capability_manifest_sha256
from lib.error_codes import ToolError
from lib.hashing import canonical_json_sha256, file_sha256
from lib.representation_contracts import representation_summary
from lib.spec_identity import content_spec_sha256, input_spec_sha256


def _load_script(filename: str, module_name: str):
    path = SCRIPTS_ROOT / filename
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


BUILDER = _load_script("build_pptx_from_spec.py", "test_build_report_builder")
ATOMIC_WRITE = importlib.import_module("lib.atomic_write")
VALIDATOR = _load_script("validate_pptx.py", "test_build_report_validator")

PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
PRESENTATION = f"{{{PRESENTATION_NS}}}"


def _rewrite_zip_entry(path: Path, entry_name: str, payload: bytes) -> None:
    replacement = path.with_name(f".{path.name}.tampered")
    try:
        with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(
            replacement, "w"
        ) as destination:
            names = source.namelist()
            if names.count(entry_name) != 1:
                raise AssertionError(
                    f"expected one target ZIP entry {entry_name!r}, got {names.count(entry_name)}"
                )
            for info in source.infolist():
                destination.writestr(
                    info,
                    payload if info.filename == entry_name else source.read(info),
                )
        os.replace(replacement, path)
    finally:
        replacement.unlink(missing_ok=True)


def remove_named_shape_from_pptx(path: Path, shape_name: str) -> None:
    """Remove exactly one named slide object from a test-copy PPTX."""
    slide_entry = "ppt/slides/slide1.xml"
    with zipfile.ZipFile(path, "r") as archive:
        root = ET.fromstring(archive.read(slide_entry))
    tree = root.find(f"{PRESENTATION}cSld/{PRESENTATION}spTree")
    if tree is None:
        raise AssertionError("test fixture slide lacks spTree")
    identity_paths = {
        f"{PRESENTATION}sp": f"{PRESENTATION}nvSpPr/{PRESENTATION}cNvPr",
        f"{PRESENTATION}cxnSp": f"{PRESENTATION}nvCxnSpPr/{PRESENTATION}cNvPr",
        f"{PRESENTATION}pic": f"{PRESENTATION}nvPicPr/{PRESENTATION}cNvPr",
        f"{PRESENTATION}graphicFrame": (
            f"{PRESENTATION}nvGraphicFramePr/{PRESENTATION}cNvPr"
        ),
    }
    matches = []
    for child in list(tree):
        identity_path = identity_paths.get(child.tag)
        identity = child.find(identity_path) if identity_path is not None else None
        if identity is not None and identity.get("name") == shape_name:
            matches.append(child)
    if len(matches) != 1:
        raise AssertionError(
            f"expected one named slide object {shape_name!r}, got {len(matches)}"
        )
    tree.remove(matches[0])
    _rewrite_zip_entry(
        path,
        slide_entry,
        ET.tostring(root, encoding="utf-8", xml_declaration=True),
    )


def mutate_named_shape_xml(
    path: Path, shape_name: str, mutation: Callable[[ET.Element], None]
) -> None:
    """Apply one controlled XML mutation to exactly one named test object."""
    slide_entry = "ppt/slides/slide1.xml"
    with zipfile.ZipFile(path, "r") as archive:
        root = ET.fromstring(archive.read(slide_entry))
    tree = root.find(f"{PRESENTATION}cSld/{PRESENTATION}spTree")
    if tree is None:
        raise AssertionError("test fixture slide lacks spTree")
    identity_paths = {
        f"{PRESENTATION}sp": f"{PRESENTATION}nvSpPr/{PRESENTATION}cNvPr",
        f"{PRESENTATION}cxnSp": f"{PRESENTATION}nvCxnSpPr/{PRESENTATION}cNvPr",
        f"{PRESENTATION}pic": f"{PRESENTATION}nvPicPr/{PRESENTATION}cNvPr",
        f"{PRESENTATION}graphicFrame": (
            f"{PRESENTATION}nvGraphicFramePr/{PRESENTATION}cNvPr"
        ),
    }
    matches = []
    for child in list(tree):
        identity_path = identity_paths.get(child.tag)
        identity = child.find(identity_path) if identity_path is not None else None
        if identity is not None and identity.get("name") == shape_name:
            matches.append(child)
    if len(matches) != 1:
        raise AssertionError(
            f"expected one named slide object {shape_name!r}, got {len(matches)}"
        )
    mutation(matches[0])
    _rewrite_zip_entry(
        path,
        slide_entry,
        ET.tostring(root, encoding="utf-8", xml_declaration=True),
    )


def replace_embedded_media(path: Path, payload: bytes) -> None:
    """Replace the sole embedded media entry in a test-copy PPTX."""
    with zipfile.ZipFile(path, "r") as archive:
        media_entries = [
            name for name in archive.namelist() if name.startswith("ppt/media/")
        ]
    if len(media_entries) != 1:
        raise AssertionError(
            f"expected exactly one embedded media entry, got {len(media_entries)}"
        )
    _rewrite_zip_entry(path, media_entries[0], payload)


def replace_picture_media_with_decoy(path: Path, payload: bytes) -> None:
    """Replace the linked media while retaining its original bytes as a decoy."""
    replacement = path.with_name(f".{path.name}.decoy")
    try:
        with zipfile.ZipFile(path, "r") as source:
            media_entries = [
                name for name in source.namelist() if name.startswith("ppt/media/")
            ]
            if len(media_entries) != 1:
                raise AssertionError(
                    f"expected exactly one embedded media entry, got {len(media_entries)}"
                )
            target = media_entries[0]
            original = source.read(target)
            with zipfile.ZipFile(replacement, "w") as destination:
                for info in source.infolist():
                    destination.writestr(
                        info, payload if info.filename == target else source.read(info)
                    )
                destination.writestr("ppt/media/decoy.png", original)
        os.replace(replacement, path)
    finally:
        replacement.unlink(missing_ok=True)


def _build_validation_fixture(
    root: Path, spec: dict
) -> tuple[Path, dict, dict]:
    pptx, _report_path, report = _compile(root, spec)
    return pptx, spec, report


def _make_labels_only_picture_base_spec(
    root: Path, *, include_independent_fallback: bool = False
) -> dict:
    """Build a compiler-valid native-label and asset-picture fixture."""
    spec = make_picture_spec(root)
    if include_independent_fallback:
        photo = next(item for item in spec["elements"] if item["element_id"] == "photo")
        _append_primitive(
            spec,
            element_id="photo-2",
            kind="picture",
            style=copy.deepcopy(photo["style"]),
            content=copy.deepcopy(photo["content"]),
            source_bbox=[400, 120, 240, 240],
            slide_bbox=[3048000, 914400, 1828800, 1828800],
            selected_mode="asset",
        )
    return spec


def _build_labels_only_validation_fixture(
    root: Path, *, include_independent_fallback: bool = False
) -> tuple[Path, dict, dict]:
    """Rebind a compiled fixture to a validator-level labels-only contract."""
    pptx, spec, report = _build_validation_fixture(
        root,
        _make_labels_only_picture_base_spec(
            root, include_independent_fallback=include_independent_fallback
        ),
    )
    facts = spec["modules"]["representation_plan"]["items"]
    facts.pop(0)
    labels_only = next(item for item in facts if item["source_fact_id"] == "fact-photo")
    labels_only["required_editability"] = "labels_only"
    labels_only["bound_element_ids"] = ["photo", "element-001"]
    report["schema_sha256"] = canonical_json_sha256(spec)
    report["content_spec_sha256"] = content_spec_sha256(spec)
    report["input_spec_sha256"] = input_spec_sha256(spec)
    report["representation_summary"] = representation_summary(spec)
    report["asset_fallbacks"] = [
        copy.deepcopy(item)
        for item in sorted(facts, key=lambda value: value["source_fact_id"])
        if item["selected_mode"] == "asset"
    ]
    report["elements"]["element-001"]["selected_mode"] = "native"
    return pptx, spec, report


def _validate_with_report(
    pptx: Path, spec: dict, report: dict
) -> tuple[Exception | None, dict | None]:
    try:
        return None, VALIDATOR.validate_pptx(pptx, 1, spec, report)
    except Exception as exc:  # the behavior assertion owns call compatibility
        return exc, None


def _required_callable(module, name: str):
    value = getattr(module, name, None)
    if not callable(value):
        raise AssertionError(f"required behavior is not implemented: {name}")
    return value


def _compile(root: Path, spec: dict) -> tuple[Path, Path, dict]:
    spec_path, prebuild_path = write_valid_fixture(root, spec)
    output = root / "page.pptx"
    report_path = root / "build-report.json"
    report = BUILDER.compile_single_page(
        spec_path, prebuild_path, output, report_path
    )
    return output, report_path, report


def _background_picture_spec(root: Path) -> dict:
    spec = make_minimal_spec(root)
    asset_path = root / "clean-background.png"
    Image.new("RGB", (1600, 900), (235, 240, 248)).save(asset_path)
    asset_sha256 = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    background = next(
        item
        for item in spec["elements"]
        if item["element_id"] == "background-base"
    )
    background.update(
        {
            "kind": "picture",
            "editable": False,
            "style": {"rotation": 0, "opacity": 1},
            "content": {
                "asset": {
                    "path": str(asset_path.resolve()),
                    "asset_sha256": asset_sha256,
                    "pixel_size": [1600, 900],
                },
                "mode": "none",
                "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            },
        }
    )
    fact = spec["modules"]["background"]["items"][0]
    fact.update(
        {
            "selected_mode": "background_picture",
            "source_provenance": {
                "kind": "clean_background_asset",
                "source_path": str(asset_path.resolve()),
                "source_sha256": asset_sha256,
            },
        }
    )
    return spec


class BuildReportContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _invoke_main(
        self,
        spec_path: Path,
        prebuild_path: Path,
        output: Path,
        report: Path,
    ) -> tuple[Exception | None, int | None, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        error: Exception | None = None
        return_code: int | None = None
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                return_code = BUILDER.main(
                    [
                        "--spec",
                        str(spec_path),
                        "--prebuild-report",
                        str(prebuild_path),
                        "--output",
                        str(output),
                        "--build-report",
                        str(report),
                    ]
                )
            except Exception as exc:
                error = exc
        return error, return_code, stdout.getvalue(), stderr.getvalue()

    def _assert_stable_cli_failure(
        self,
        spec_path: Path,
        prebuild_path: Path,
        output: Path,
        report: Path,
    ) -> None:
        error, return_code, stdout, stderr = self._invoke_main(
            spec_path, prebuild_path, output, report
        )
        self.assertIsNone(error)
        self.assertEqual(return_code, 2)
        self.assertEqual(stderr, "")
        failure = json.loads(stdout)
        self.assertEqual(failure["valid"], False)
        self.assertEqual(failure["errors"][0]["code"], "BUILD_OUTPUT_INCOMPLETE")
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_report_binds_normalized_pptx_and_complete_runtime_contract(self) -> None:
        spec = make_native_list_spec(self.root)
        output, report_path, returned = _compile(
            self.root, spec
        )

        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report, returned)
        self.assertEqual(
            set(report),
            {
                "valid",
                "schema_version",
                "schema_sha256",
                "content_spec_sha256",
                "input_spec_sha256",
                "compiler_sha256",
                "capability_manifest_sha256",
                "pptx_sha256",
                "environment",
                "elements",
                "representation_summary",
                "asset_fallbacks",
                "background_summary",
                "background_pictures",
                "normalization",
                "warnings",
                "unsupported",
            },
        )
        self.assertEqual(report["pptx_sha256"], file_sha256(output))
        self.assertEqual(
            report["capability_manifest_sha256"], capability_manifest_sha256()
        )
        self.assertEqual(report["normalization"]["applied"], True)
        self.assertEqual(
            set(report["environment"]), {"python", "python-pptx", "Pillow"}
        )
        self.assertNotIn("timestamp", json.dumps(report, ensure_ascii=False).lower())
        self.assertEqual(
            report["representation_summary"],
            {"asset": 0, "composite": 0, "native": 1, "not_applicable": 0},
        )
        self.assertEqual(report["asset_fallbacks"], [])
        self.assertEqual(
            report["content_spec_sha256"], content_spec_sha256(spec)
        )
        self.assertEqual(report["input_spec_sha256"], report["schema_sha256"])
        self.assertEqual(
            report["background_summary"],
            {"native": 1, "background_picture": 0},
        )
        self.assertEqual(report["background_pictures"], [])

    def test_background_picture_report_is_separate_from_asset_fallbacks(self) -> None:
        spec = _background_picture_spec(self.root)

        _output, _report_path, report = _compile(self.root, spec)

        self.assertEqual(report["asset_fallbacks"], [])
        self.assertEqual(
            report["background_summary"],
            {"native": 0, "background_picture": 1},
        )
        expected = {
            **spec["modules"]["background"]["items"][0],
            "media_sha256": next(
                item["media_sha256"]
                for item in report["elements"]["background-base"]["objects"]
            ),
        }
        self.assertEqual(report["background_pictures"], [expected])

    def test_validator_reconciles_real_background_picture_independently(self) -> None:
        pptx, spec, original = _build_validation_fixture(
            self.root, _background_picture_spec(self.root)
        )
        asset_path = Path(
            next(
                item
                for item in spec["elements"]
                if item["element_id"] == "background-base"
            )["content"]["asset"]["path"]
        )
        independent_asset_sha256 = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        with zipfile.ZipFile(pptx) as archive:
            media_names = [
                name for name in archive.namelist() if name.startswith("ppt/media/")
            ]
            self.assertEqual(len(media_names), 1)
            independent_pptx_media_sha256 = hashlib.sha256(
                archive.read(media_names[0])
            ).hexdigest()
        self.assertEqual(independent_asset_sha256, independent_pptx_media_sha256)
        self.assertEqual(
            original["background_pictures"][0]["media_sha256"],
            independent_asset_sha256,
        )
        self.assertTrue(VALIDATOR.validate_pptx(pptx, 1, spec, original)["valid"])

        schema_fact = copy.deepcopy(spec)
        schema_fact["modules"]["background"]["items"][0]["reason"] = (
            "tampered schema fact"
        )
        schema_fact_report = copy.deepcopy(original)
        schema_fact_report["schema_sha256"] = canonical_json_sha256(schema_fact)
        schema_fact_report["content_spec_sha256"] = content_spec_sha256(schema_fact)
        schema_fact_report["input_spec_sha256"] = input_spec_sha256(schema_fact)
        schema_fact_result = VALIDATOR.validate_pptx(
            pptx, 1, schema_fact, schema_fact_report
        )
        self.assertIn("BUILD_REPORT_MISMATCH", schema_fact_result["errors"])
        self.assertTrue(
            any(
                warning.startswith("build_report.background_pictures:")
                for warning in schema_fact_result["warnings"]
            ),
            schema_fact_result,
        )

        cases: list[tuple[str, dict, str, str]] = []
        summary = copy.deepcopy(original)
        summary["background_summary"] = {
            "native": 1,
            "background_picture": 0,
        }
        cases.append(
            (
                "summary",
                summary,
                "BUILD_REPORT_MISMATCH",
                "build_report.background_summary",
            )
        )
        fact = copy.deepcopy(original)
        fact["background_pictures"][0]["reason"] = "tampered fact"
        cases.append(
            (
                "fact",
                fact,
                "BUILD_REPORT_MISMATCH",
                "build_report.background_pictures",
            )
        )
        background_hash = copy.deepcopy(original)
        background_hash["background_pictures"][0]["media_sha256"] = "0" * 64
        cases.append(
            (
                "background-media",
                background_hash,
                "BUILD_REPORT_MISMATCH",
                "build_report.background_pictures",
            )
        )
        element_hash = copy.deepcopy(original)
        element_hash["elements"]["background-base"]["objects"][0][
            "media_sha256"
        ] = "0" * 64
        cases.append(
            (
                "element-media",
                element_hash,
                "ASSET_HASH_MISMATCH",
                "build_report.elements.background-base.objects[0].media_sha256",
            )
        )
        for name, report, expected_code, expected_path in cases:
            with self.subTest(name=name):
                result = VALIDATOR.validate_pptx(pptx, 1, spec, report)
                self.assertIn(expected_code, result["errors"])
                self.assertTrue(
                    any(
                        warning.startswith(f"{expected_path}:")
                        for warning in result["warnings"]
                    ),
                    result,
                )

        tampered_pptx = self.root / "tampered-background.pptx"
        tampered_pptx.write_bytes(pptx.read_bytes())
        replacement = io.BytesIO()
        Image.new("RGB", (1600, 900), (180, 40, 80)).save(
            replacement, format="PNG"
        )
        replace_embedded_media(tampered_pptx, replacement.getvalue())
        tampered_report = copy.deepcopy(original)
        tampered_report["pptx_sha256"] = file_sha256(tampered_pptx)

        tampered_result = VALIDATOR.validate_pptx(
            tampered_pptx, 1, spec, tampered_report
        )

        self.assertIn("ASSET_HASH_MISMATCH", tampered_result["errors"])
        self.assertTrue(
            any(
                warning.startswith(
                    "build_report.elements.background-base.objects[0].media_sha256:"
                )
                for warning in tampered_result["warnings"]
            ),
            tampered_result,
        )

    def test_validator_rejects_build_report_pptx_hash_mismatch(self) -> None:
        pptx, spec, report = _build_validation_fixture(
            self.root, make_native_list_spec(self.root)
        )
        report["pptx_sha256"] = "0" * 64

        error, result = _validate_with_report(pptx, spec, report)

        self.assertIsNone(error)
        assert result is not None
        self.assertIn("BUILD_REPORT_MISMATCH", result["errors"])

    def test_validator_rejects_missing_registered_multipart_part(self) -> None:
        pptx, spec, report = _build_validation_fixture(
            self.root, make_status_spec(self.root, repeat=False)
        )
        remove_named_shape_from_pptx(pptx, "ia:status:segment-1")

        error, result = _validate_with_report(pptx, spec, report)

        self.assertIsNone(error)
        assert result is not None
        self.assertIn("BUILD_OUTPUT_INCOMPLETE", result["errors"])

    def test_validator_recomputes_embedded_media_hash(self) -> None:
        pptx, spec, report = _build_validation_fixture(
            self.root, make_picture_spec(self.root)
        )
        replace_embedded_media(pptx, b"different")

        error, result = _validate_with_report(pptx, spec, report)

        self.assertIsNone(error)
        assert result is not None
        self.assertIn("ASSET_HASH_MISMATCH", result["errors"])

    def test_validator_binds_media_hash_to_the_named_picture_relationship(self) -> None:
        pptx, spec, report = _build_validation_fixture(
            self.root, make_picture_spec(self.root)
        )
        buffer = io.BytesIO()
        Image.new("RGB", (40, 20), (220, 40, 80)).save(buffer, format="PNG")
        replace_picture_media_with_decoy(pptx, buffer.getvalue())
        report["pptx_sha256"] = file_sha256(pptx)

        result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertIn("ASSET_HASH_MISMATCH", result["errors"])

    def test_validator_accepts_complete_status_contract_with_counters(self) -> None:
        pptx, spec, report = _build_validation_fixture(
            self.root, make_status_spec(self.root, repeat=True)
        )

        result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertEqual(result["errors"], [])
        self.assertTrue(result["valid"])
        self.assertEqual(result["build_report_objects_checked"], 4)
        self.assertEqual(result["multipart_contracts_checked"], 1)
        self.assertEqual(result["representation_facts_checked"], 2)
        self.assertEqual(result["asset_fallbacks_checked"], 0)

    def test_validator_accepts_complete_asset_contract_with_counters(self) -> None:
        pptx, spec, report = _build_validation_fixture(
            self.root, make_picture_spec(self.root)
        )

        result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertEqual(result["errors"], [])
        self.assertTrue(result["valid"])
        self.assertEqual(result["build_report_objects_checked"], 3)
        self.assertEqual(result["multipart_contracts_checked"], 0)
        self.assertEqual(result["representation_facts_checked"], 2)
        self.assertEqual(result["asset_fallbacks_checked"], 1)

    def test_validator_accepts_table_and_icon_report_bindings(self) -> None:
        for name, factory in (
            ("table", make_merged_table_spec),
            ("icon", make_icon_spec),
        ):
            with self.subTest(name=name):
                root = self.root / name
                pptx, spec, report = _build_validation_fixture(root, factory(root))

                result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

                self.assertEqual(result["errors"], [])
                self.assertTrue(result["valid"])

    def test_validator_accepts_non_text_shapes_line_and_explicit_empty_text(self) -> None:
        factories = [
            (shape_type, lambda root, shape_type=shape_type: make_shape_spec(root, shape_type))
            for shape_type in ("rectangle", "ellipse", "chevron")
        ]

        def line_spec(root: Path) -> dict:
            spec = make_minimal_spec(root)
            _append_primitive(
                spec,
                element_id="arrow",
                kind="line",
                style={
                    "line": {
                        "color": "#112233",
                        "width": 12700,
                        "dash": "solid",
                        "opacity": 1.0,
                    },
                    "head_arrow": "none",
                    "tail_arrow": "triangle",
                    "rotation": 0,
                },
                content={},
            )
            return spec

        factories.append(("line", line_spec))
        for name, factory in factories:
            with self.subTest(name=name):
                root = self.root / name
                pptx, spec, report = _build_validation_fixture(root, factory(root))

                result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

                self.assertEqual(result["errors"], [])
                self.assertTrue(result["valid"])
                record = next(
                    item
                    for item in result["structure_objects"]
                    if item["object_name"] != "ia:element-001"
                )
                self.assertIsNone(record["text_summary"])

        empty_root = self.root / "explicit-empty"
        empty_spec = make_status_spec(empty_root, repeat=False)
        empty_spec["elements"][-1]["content"]["parts"][0]["content"]["text"] = ""
        pptx, spec, report = _build_validation_fixture(empty_root, empty_spec)

        result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertTrue(result["valid"])
        empty_record = next(
            item
            for item in result["structure_objects"]
            if item["object_name"] == "ia:status:segment-0"
        )
        self.assertEqual(empty_record["text_summary"], "")

    def test_validator_rejects_text_injected_into_schema_non_text_shape(self) -> None:
        drawing = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        for name, injected_text in (("content", "injected"), ("empty", "")):
            with self.subTest(name=name):
                root = self.root / f"injected-{name}"
                pptx, spec, report = _build_validation_fixture(
                    root, make_shape_spec(root, "rectangle")
                )

                def inject_text(shape: ET.Element) -> None:
                    tx_body = ET.SubElement(shape, f"{PRESENTATION}txBody")
                    ET.SubElement(tx_body, f"{drawing}bodyPr")
                    ET.SubElement(tx_body, f"{drawing}lstStyle")
                    paragraph = ET.SubElement(tx_body, f"{drawing}p")
                    run = ET.SubElement(paragraph, f"{drawing}r")
                    text = ET.SubElement(run, f"{drawing}t")
                    text.text = injected_text or None

                mutate_named_shape_xml(pptx, "ia:card", inject_text)
                report["elements"]["card"]["objects"][0][
                    "text_summary"
                ] = injected_text
                report["pptx_sha256"] = file_sha256(pptx)

                result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

                self.assertIn("BUILD_REPORT_MISMATCH", result["errors"])

    def test_validator_counters_only_count_successful_contracts(self) -> None:
        bbox_root = self.root / "bbox"
        pptx, spec, report = _build_validation_fixture(
            bbox_root, make_status_spec(bbox_root, repeat=False)
        )
        report["elements"]["status"]["objects"][0]["bbox"][0] += 1

        bbox_result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertEqual(bbox_result["build_report_objects_checked"], 3)
        self.assertEqual(bbox_result["multipart_contracts_checked"], 0)
        self.assertEqual(bbox_result["representation_facts_checked"], 1)
        self.assertEqual(bbox_result["asset_fallbacks_checked"], 0)

        mode_root = self.root / "mode"
        pptx, spec, report = _build_validation_fixture(
            mode_root, make_status_spec(mode_root, repeat=False)
        )
        report["elements"]["status"]["selected_mode"] = "native"

        mode_result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertEqual(mode_result["build_report_objects_checked"], 2)
        self.assertEqual(mode_result["multipart_contracts_checked"], 0)
        self.assertEqual(mode_result["representation_facts_checked"], 1)

        fallback_root = self.root / "fallback"
        pptx, spec, report = _build_validation_fixture(
            fallback_root, make_picture_spec(fallback_root)
        )
        report["asset_fallbacks"][0]["source_bbox"][0] += 1

        fallback_result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertEqual(fallback_result["build_report_objects_checked"], 3)
        self.assertEqual(fallback_result["multipart_contracts_checked"], 0)
        self.assertEqual(fallback_result["representation_facts_checked"], 2)
        self.assertEqual(fallback_result["asset_fallbacks_checked"], 0)

    def test_labels_only_fallback_requires_verified_label_evidence(self) -> None:
        valid_root = self.root / "labels-valid"
        pptx, spec, report = _build_labels_only_validation_fixture(valid_root)

        valid_result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertTrue(valid_result["valid"])
        self.assertEqual(valid_result["representation_facts_checked"], 1)
        self.assertEqual(valid_result["asset_fallbacks_checked"], 1)

        drawing = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

        def change_bbox(shape: ET.Element) -> None:
            offset = shape.find(f"{PRESENTATION}spPr/{drawing}xfrm/{drawing}off")
            if offset is None:
                raise AssertionError("test label lacks position")
            offset.set("x", str(int(offset.get("x", "0")) + 1))

        def change_text(shape: ET.Element) -> None:
            text = shape.find(f".//{drawing}t")
            if text is None:
                raise AssertionError("test label lacks text")
            text.text = "已篡改"

        def change_font(shape: ET.Element) -> None:
            fonts = shape.findall(f".//{drawing}rPr/{drawing}latin")
            if not fonts:
                raise AssertionError("test label lacks explicit font")
            for font in fonts:
                font.set("typeface", "Courier New")

        def report_type(report: dict) -> None:
            element = report["elements"]["element-001"]
            element["object_type"] = "graphicFrame"
            element["objects"][0]["object_type"] = "graphicFrame"

        def report_bbox(report: dict) -> None:
            report["elements"]["element-001"]["objects"][0]["bbox"][0] += 1

        def report_text(report: dict) -> None:
            report["elements"]["element-001"]["objects"][0]["text_summary"] = "已篡改"

        def report_font(report: dict) -> None:
            report["elements"]["element-001"]["objects"][0][
                "font_declarations"
            ] = ["Courier New"]

        cases = (
            ("report-type", report_type, None),
            ("report-bbox", report_bbox, None),
            ("report-text", report_text, None),
            ("report-font", report_font, None),
            ("pptx-bbox", None, change_bbox),
            ("pptx-text", None, change_text),
            ("pptx-font", None, change_font),
        )
        for name, report_mutation, pptx_mutation in cases:
            with self.subTest(name=name):
                root = self.root / name
                pptx, spec, report = _build_labels_only_validation_fixture(root)
                if report_mutation is not None:
                    report_mutation(report)
                if pptx_mutation is not None:
                    mutate_named_shape_xml(pptx, "ia:element-001", pptx_mutation)
                    report["pptx_sha256"] = file_sha256(pptx)

                result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

                self.assertFalse(result["valid"])
                self.assertEqual(result["representation_facts_checked"], 0)
                self.assertEqual(result["asset_fallbacks_checked"], 0)

        missing_root = self.root / "pptx-missing"
        pptx, spec, report = _build_labels_only_validation_fixture(missing_root)
        remove_named_shape_from_pptx(pptx, "ia:element-001")
        report["pptx_sha256"] = file_sha256(pptx)

        missing_result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertFalse(missing_result["valid"])
        self.assertEqual(missing_result["representation_facts_checked"], 0)
        self.assertEqual(missing_result["asset_fallbacks_checked"], 0)

        binding_root = self.root / "unknown-binding"
        pptx, spec, report = _build_labels_only_validation_fixture(binding_root)
        report["asset_fallbacks"][0]["bound_element_ids"][-1] = "missing-label"

        binding_result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertFalse(binding_result["valid"])
        self.assertEqual(binding_result["asset_fallbacks_checked"], 0)

    def test_labels_only_failure_preserves_independent_fallback_counter(self) -> None:
        root = self.root / "independent-fallback"
        pptx, spec, report = _build_labels_only_validation_fixture(
            root,
            include_independent_fallback=True,
        )
        remove_named_shape_from_pptx(pptx, "ia:element-001")
        report["pptx_sha256"] = file_sha256(pptx)

        result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertFalse(result["valid"])
        self.assertEqual(result["representation_facts_checked"], 1)
        self.assertEqual(result["asset_fallbacks_checked"], 1)

    def test_validator_rejects_stale_report_identities(self) -> None:
        pptx, spec, original = _build_validation_fixture(
            self.root, make_native_list_spec(self.root)
        )
        for field in (
            "schema_sha256",
            "content_spec_sha256",
            "input_spec_sha256",
            "compiler_sha256",
            "capability_manifest_sha256",
        ):
            with self.subTest(field=field):
                report = copy.deepcopy(original)
                report[field] = "0" * 64

                result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

                self.assertIn("BUILD_REPORT_MISMATCH", result["errors"])
                self.assertTrue(any(field in item for item in result["warnings"]))

    def test_validator_rejects_duplicate_or_tampered_report_objects(self) -> None:
        pptx, spec, original = _build_validation_fixture(
            self.root, make_status_spec(self.root, repeat=False)
        )
        cases = []
        duplicate = copy.deepcopy(original)
        duplicate["elements"]["status"]["objects"].append(
            copy.deepcopy(duplicate["elements"]["status"]["objects"][0])
        )
        cases.append(("duplicate", duplicate, "BUILD_REPORT_MISMATCH"))
        wrong_type = copy.deepcopy(original)
        wrong_type["elements"]["status"]["objects"][0]["object_type"] = "pic"
        cases.append(("type", wrong_type, "BUILD_REPORT_INVALID"))
        wrong_summary_type = copy.deepcopy(original)
        wrong_summary_type["elements"]["status"]["object_type"] = "pic"
        cases.append(("summary-type", wrong_summary_type, "BUILD_REPORT_MISMATCH"))
        wrong_bbox = copy.deepcopy(original)
        wrong_bbox["elements"]["status"]["objects"][0]["bbox"][0] += 1
        cases.append(("bbox", wrong_bbox, "BUILD_REPORT_MISMATCH"))
        wrong_font = copy.deepcopy(original)
        wrong_font["elements"]["status"]["objects"][0]["font_declarations"] = [
            "Courier New"
        ]
        cases.append(("font", wrong_font, "BUILD_REPORT_MISMATCH"))
        for name, report, expected_code in cases:
            with self.subTest(name=name):
                result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

                self.assertIn(expected_code, result["errors"])

    def test_validator_recomputes_object_rotation_text_and_fonts_from_ooxml(self) -> None:
        drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
        drawing = f"{{{drawing_ns}}}"

        def change_rotation(shape: ET.Element) -> None:
            transform = shape.find(f"{PRESENTATION}spPr/{drawing}xfrm")
            if transform is None:
                raise AssertionError("test object lacks transform")
            transform.set("rot", "1800000")

        def change_text(shape: ET.Element) -> None:
            text = shape.find(f".//{drawing}t")
            if text is None:
                raise AssertionError("test object lacks text")
            text.text = "已篡改"

        def change_font(shape: ET.Element) -> None:
            fonts = shape.findall(f".//{drawing}rPr/{drawing}latin")
            if not fonts:
                raise AssertionError("test object lacks explicit font")
            for font in fonts:
                font.set("typeface", "Courier New")

        for name, mutation in (
            ("rotation", change_rotation),
            ("text", change_text),
            ("font", change_font),
        ):
            with self.subTest(name=name):
                root = self.root / name
                pptx, spec, report = _build_validation_fixture(
                    root, make_status_spec(root, repeat=False)
                )
                mutate_named_shape_xml(pptx, "ia:status:segment-0", mutation)
                report["pptx_sha256"] = file_sha256(pptx)

                result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

                self.assertIn("BUILD_OUTPUT_INCOMPLETE", result["errors"])

    def test_validator_recomputes_table_text_summary_from_ooxml(self) -> None:
        drawing = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        pptx, spec, report = _build_validation_fixture(
            self.root, make_merged_table_spec(self.root)
        )

        def change_cell_text(frame: ET.Element) -> None:
            text = frame.find(f".//{drawing}t")
            if text is None:
                raise AssertionError("test table lacks cell text")
            text.text = "表格已篡改"

        mutate_named_shape_xml(pptx, "ia:table", change_cell_text)
        report["pptx_sha256"] = file_sha256(pptx)

        result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertIn("BUILD_OUTPUT_INCOMPLETE", result["errors"])

    def test_validator_rejects_missing_asset_fallback_binding(self) -> None:
        pptx, spec, report = _build_validation_fixture(
            self.root, make_picture_spec(self.root)
        )
        report["asset_fallbacks"] = []

        result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

        self.assertIn("BUILD_REPORT_MISMATCH", result["errors"])

    def test_validator_loads_build_report_path_and_cli_option(self) -> None:
        pptx, spec, report = _build_validation_fixture(
            self.root, make_native_list_spec(self.root)
        )
        spec_path = self.root / "validator-spec.json"
        report_path = self.root / "validator-build-report.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        report_path.write_text(json.dumps(report), encoding="utf-8")

        direct = VALIDATOR.validate_pptx(pptx, 1, spec_path, report_path)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            return_code = VALIDATOR.main(
                [
                    str(pptx),
                    "--expected-slides",
                    "1",
                    "--spec",
                    str(spec_path),
                    "--build-report",
                    str(report_path),
                    "--summary",
                ]
            )

        self.assertTrue(direct["valid"])
        self.assertEqual(return_code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["valid"])

    def test_validator_rejects_invalid_build_report_inputs(self) -> None:
        pptx, spec, _report = _build_validation_fixture(
            self.root, make_native_list_spec(self.root)
        )
        cases = {
            "missing": self.root / "missing-report.json",
            "malformed": self.root / "malformed-report.json",
            "wrong-root": self.root / "wrong-root-report.json",
        }
        cases["malformed"].write_text("{", encoding="utf-8")
        cases["wrong-root"].write_text("[]", encoding="utf-8")
        for name, report_path in cases.items():
            with self.subTest(name=name):
                result = VALIDATOR.validate_pptx(pptx, 1, spec, report_path)

                self.assertIn("BUILD_REPORT_INVALID", result["errors"])

        error, result = _validate_with_report(pptx, spec, "bad\x00report.json")
        self.assertIsNone(error)
        assert result is not None
        self.assertIn("BUILD_REPORT_INVALID", result["errors"])

    def test_validator_rejects_malformed_nested_build_report_contracts(self) -> None:
        pptx, spec, original = _build_validation_fixture(
            self.root, make_native_list_spec(self.root)
        )
        cases = []
        extra = copy.deepcopy(original)
        extra["unexpected"] = True
        cases.append(("unknown-top-level", extra))
        invalid_elements = copy.deepcopy(original)
        invalid_elements["elements"] = []
        cases.append(("elements-type", invalid_elements))
        invalid_objects = copy.deepcopy(original)
        invalid_objects["elements"]["element-001"]["objects"] = [None]
        cases.append(("object-type", invalid_objects))
        invalid_bbox = copy.deepcopy(original)
        invalid_bbox["elements"]["element-001"]["objects"][0]["bbox"] = "bad"
        cases.append(("bbox-type", invalid_bbox))
        invalid_hash = copy.deepcopy(original)
        invalid_hash["compiler_sha256"] = "bad"
        cases.append(("hash-format", invalid_hash))
        invalid_environment = copy.deepcopy(original)
        invalid_environment["environment"] = {"arbitrary": "identity"}
        cases.append(("environment-contract", invalid_environment))
        invalid_normalization = copy.deepcopy(original)
        invalid_normalization["normalization"]["unexpected"] = True
        cases.append(("normalization-contract", invalid_normalization))
        for name, report in cases:
            with self.subTest(name=name):
                error, result = _validate_with_report(pptx, spec, report)

                self.assertIsNone(error)
                assert result is not None
                self.assertIn("BUILD_REPORT_INVALID", result["errors"])

    def test_validator_rejects_picture_n_a_fields_and_fallback_shape(self) -> None:
        pptx, spec, original = _build_validation_fixture(
            self.root, make_picture_spec(self.root)
        )
        fake_text = copy.deepcopy(original)
        fake_text["elements"]["photo"]["objects"][0]["text_summary"] = "fake"
        malformed_fallback = copy.deepcopy(original)
        malformed_fallback["asset_fallbacks"][0]["bound_element_ids"] = "photo"
        for name, report in (
            ("picture-text", fake_text),
            ("fallback-nested", malformed_fallback),
        ):
            with self.subTest(name=name):
                result = VALIDATOR.validate_pptx(pptx, 1, spec, report)

                self.assertIn("BUILD_REPORT_INVALID", result["errors"])

    def test_validator_rejects_malformed_representation_spec_without_exception(self) -> None:
        pptx, spec, report = _build_validation_fixture(
            self.root, make_native_list_spec(self.root)
        )
        spec["modules"]["representation_plan"]["items"].append(None)
        report["schema_sha256"] = canonical_json_sha256(spec)

        error, result = _validate_with_report(pptx, spec, report)

        self.assertIsNone(error)
        assert result is not None
        self.assertIn("RECONSTRUCTION_SPEC_INVALID", result["errors"])

    def test_validator_rejects_invalid_reconstruction_spec_path_without_exception(self) -> None:
        pptx, _spec, report = _build_validation_fixture(
            self.root, make_native_list_spec(self.root)
        )
        try:
            result = VALIDATOR.validate_pptx(pptx, 1, "bad\x00spec.json", report)
            error = None
        except Exception as exc:
            result = None
            error = exc

        self.assertIsNone(error)
        assert result is not None
        self.assertIn("RECONSTRUCTION_SPEC_INVALID", result["errors"])

    def test_zip_tamper_helpers_fail_on_ambiguous_targets(self) -> None:
        native_pptx, _spec, _report = _build_validation_fixture(
            self.root / "native", make_native_list_spec(self.root / "native")
        )
        with self.assertRaises(AssertionError):
            remove_named_shape_from_pptx(native_pptx, "ia:not-present")
        with self.assertRaises(AssertionError):
            replace_embedded_media(native_pptx, b"unused")

        picture_root = self.root / "picture"
        picture_pptx, _spec, _report = _build_validation_fixture(
            picture_root, make_picture_spec(picture_root)
        )
        replacement = picture_pptx.with_name(".two-media.pptx")
        try:
            with zipfile.ZipFile(picture_pptx, "r") as source, zipfile.ZipFile(
                replacement, "w"
            ) as destination:
                for info in source.infolist():
                    destination.writestr(info, source.read(info))
                destination.writestr("ppt/media/second.png", b"second")
            os.replace(replacement, picture_pptx)
        finally:
            replacement.unlink(missing_ok=True)
        with self.assertRaises(AssertionError):
            replace_embedded_media(picture_pptx, b"unused")

    def test_compiler_identity_is_sorted_content_only_and_canonical(self) -> None:
        compiler_identity = _required_callable(BUILDER, "compiler_identity")
        compiler_sha256 = _required_callable(BUILDER, "compiler_sha256")

        first = compiler_identity()
        second = compiler_identity()
        self.assertEqual(first, second)
        self.assertEqual(list(first), sorted(first))
        self.assertEqual(
            set(first),
            {
                "build_pptx_from_spec.py",
                *{
                    path.relative_to(SCRIPTS_ROOT).as_posix()
                    for path in (SCRIPTS_ROOT / "lib").glob("*.py")
                    if path.is_file()
                },
                *{
                    path.relative_to(SCRIPTS_ROOT).as_posix()
                    for path in (SCRIPTS_ROOT / "pptx_builder").glob("*.py")
                    if path.is_file()
                },
            },
        )
        for relative_path, digest in first.items():
            expected = hashlib.sha256(
                (SCRIPTS_ROOT / relative_path).read_bytes()
            ).hexdigest()
            self.assertEqual(digest, expected)
        self.assertEqual(compiler_sha256(), canonical_json_sha256(first))

    def test_asset_fallbacks_come_only_from_asset_representation_facts(self) -> None:
        spec = make_picture_spec(self.root)

        _, _, report = _compile(self.root, spec)

        self.assertIn("representation_summary", report)
        self.assertIn("asset_fallbacks", report)
        self.assertEqual(
            report["representation_summary"],
            {"asset": 1, "composite": 0, "native": 1, "not_applicable": 0},
        )
        self.assertEqual(len(report["asset_fallbacks"]), 1)
        fallback = report["asset_fallbacks"][0]
        self.assertEqual(fallback["source_fact_id"], "fact-photo")
        self.assertEqual(fallback["selected_mode"], "asset")
        self.assertEqual(fallback["bound_element_ids"], ["photo"])

    def test_valid_not_applicable_fact_uses_shared_representation_summary(self) -> None:
        spec = make_native_list_spec(self.root)
        evidence = spec["modules"]["representation_plan"]["items"][0][
            "evidence"
        ]
        spec["modules"]["representation_plan"]["items"].append(
            {
                "source_fact_id": "fact-decoration-not-present",
                "semantic_role": "decoration",
                "source_bbox": [1, 1, 1, 1],
                "required": False,
                "selected_mode": None,
                "required_editability": "none",
                "fallback_policy": "forbid",
                "bound_element_ids": [],
                "reason": "the inspected source contains no decoration",
                "coverage_status": "not_applicable",
                "evidence": list(evidence),
            }
        )

        error = None
        result = None
        try:
            result = _compile(self.root, spec)
        except Exception as exc:  # behavior assertion below owns success
            error = exc

        self.assertIsNone(error)
        assert result is not None
        output, _, report = result
        self.assertTrue(output.is_file())
        self.assertEqual(
            report["representation_summary"],
            {"asset": 0, "composite": 0, "native": 1, "not_applicable": 1},
        )

    def test_normalizer_failure_is_stable_and_publishes_neither_output(self) -> None:
        spec = make_native_list_spec(self.root)
        spec_path, prebuild_path = write_valid_fixture(self.root, spec)
        output = self.root / "page.pptx"
        report_path = self.root / "build-report.json"

        error = None
        with mock.patch.object(
            BUILDER,
            "normalize_pptx",
            side_effect=BUILDER.NormalizeError(
                "NORMALIZE_INPUT_INVALID", "input.pptx", "boom"
            ),
        ):
            try:
                BUILDER.compile_single_page(
                    spec_path, prebuild_path, output, report_path
                )
            except Exception as exc:  # behavior assertion below owns the type
                error = exc

        self.assertIsInstance(error, ToolError)
        assert isinstance(error, ToolError)
        self.assertEqual(error.code, "NORMALIZE_INPUT_INVALID")
        self.assertEqual(error.path, "input.pptx")
        self.assertFalse(output.exists())
        self.assertFalse(report_path.exists())

    def test_normalizer_expected_runtime_failures_are_cli_json_only(self) -> None:
        for name, failure in (
            ("io", OSError("cannot read normalized input")),
            ("format", ValueError("malformed normalized package")),
        ):
            with self.subTest(name=name):
                root = self.root / name
                spec_path, prebuild_path = write_valid_fixture(
                    root, make_native_list_spec(root)
                )
                output = root / "page.pptx"
                report = root / "build-report.json"
                with mock.patch.object(
                    BUILDER, "normalize_pptx", side_effect=failure
                ):
                    self._assert_stable_cli_failure(
                        spec_path, prebuild_path, output, report
                    )

    def test_normalizer_malformed_result_is_cli_json_only(self) -> None:
        spec_path, prebuild_path = write_valid_fixture(
            self.root, make_native_list_spec(self.root)
        )
        output = self.root / "page.pptx"
        report = self.root / "build-report.json"

        with mock.patch.object(
            BUILDER, "normalize_pptx", return_value={"valid": True}
        ):
            self._assert_stable_cli_failure(
                spec_path, prebuild_path, output, report
            )

    def test_normalizer_tool_error_provenance_reaches_cli_unchanged(self) -> None:
        spec_path, prebuild_path = write_valid_fixture(
            self.root, make_native_list_spec(self.root)
        )
        output = self.root / "page.pptx"
        report = self.root / "build-report.json"
        original = ToolError(
            "NORMALIZER_STABLE_FAILURE",
            "modules.typography.items[0]",
            "normalizer rejected a stable contract",
            "text.paragraph.native_bullet",
        )

        with mock.patch.object(BUILDER, "normalize_pptx", side_effect=original):
            error, return_code, stdout, stderr = self._invoke_main(
                spec_path, prebuild_path, output, report
            )

        self.assertIsNone(error)
        self.assertEqual(return_code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {"valid": False, "errors": [original.as_dict()]},
        )
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_output_ancestor_relationships_fail_before_any_path_creation(self) -> None:
        for name in (
            "pptx-parent",
            "report-parent",
            "lexical-dotdot",
            "symlink-ancestor",
        ):
            with self.subTest(name=name):
                root = self.root / "ancestor-cases" / name
                spec_path, prebuild_path = write_valid_fixture(
                    root, make_native_list_spec(root)
                )
                real = root / "real"
                real.mkdir()
                alias = root / "alias"
                alias.symlink_to(real, target_is_directory=True)
                if name == "pptx-parent":
                    output = root / "page.pptx"
                    report = output / "build-report.json"
                    forbidden_path = output
                elif name == "report-parent":
                    report = root / "build-report.json"
                    output = report / "page.pptx"
                    forbidden_path = report
                elif name == "lexical-dotdot":
                    output = root / "unused" / ".." / "page.pptx"
                    report = root / "page.pptx" / "build-report.json"
                    forbidden_path = root / "page.pptx"
                else:
                    output = alias / "page.pptx"
                    report = real / "page.pptx" / "build-report.json"
                    forbidden_path = real / "page.pptx"
                with self.assertRaises(ToolError) as raised:
                    BUILDER.compile_single_page(
                        spec_path, prebuild_path, output, report
                    )
                self.assertEqual(raised.exception.code, "BUILD_OUTPUT_INCOMPLETE")
                self.assertFalse(forbidden_path.exists())
                self.assertTrue(alias.is_symlink())

    def test_report_construction_failures_are_cli_json_only(self) -> None:
        cases = (
            ("compiler-identity", "compiler_identity", OSError("identity read")),
            ("compiler-hash", "compiler_sha256", OSError("compiler hash")),
            (
                "manifest-hash",
                "capability_manifest_sha256",
                ValueError("manifest data"),
            ),
            (
                "environment",
                "_environment",
                importlib.metadata.PackageNotFoundError("Pillow"),
            ),
            ("summary", "representation_summary", KeyError("mode")),
            ("fallback", "_asset_fallbacks", TypeError("fallback data")),
        )
        for name, target, failure in cases:
            with self.subTest(name=name):
                root = self.root / f"report-{name}"
                spec_path, prebuild_path = write_valid_fixture(
                    root, make_native_list_spec(root)
                )
                output = root / "page.pptx"
                report = root / "build-report.json"
                with mock.patch.object(BUILDER, target, side_effect=failure):
                    self._assert_stable_cli_failure(
                        spec_path, prebuild_path, output, report
                    )

    def test_report_construction_does_not_swallow_process_control_exceptions(self) -> None:
        for exception_type in (KeyboardInterrupt, SystemExit, MemoryError):
            with self.subTest(exception_type=exception_type.__name__):
                root = self.root / exception_type.__name__
                spec_path, prebuild_path = write_valid_fixture(
                    root, make_native_list_spec(root)
                )
                output = root / "page.pptx"
                report = root / "build-report.json"
                with mock.patch.object(
                    BUILDER, "_environment", side_effect=exception_type()
                ):
                    with self.assertRaises(exception_type):
                        BUILDER.compile_single_page(
                            spec_path, prebuild_path, output, report
                        )
                self.assertFalse(output.exists())
                self.assertFalse(report.exists())

    def test_existing_target_is_rejected_without_overwriting_or_half_pair(self) -> None:
        publish = _required_callable(ATOMIC_WRITE, "publish_pair_no_overwrite")
        candidates = self.root / "transaction"
        candidates.mkdir()
        pptx_candidate = candidates / "candidate.pptx"
        report_candidate = candidates / "candidate.json"
        pptx_candidate.write_bytes(b"new-pptx")
        report_candidate.write_bytes(b"new-report")
        output = self.root / "page.pptx"
        report = self.root / "build-report.json"
        output.write_bytes(b"user-pptx")

        with self.assertRaises(ToolError) as raised:
            publish(pptx_candidate, report_candidate, output, report)

        self.assertEqual(raised.exception.code, "BUILD_OUTPUT_INCOMPLETE")
        self.assertEqual(output.read_bytes(), b"user-pptx")
        self.assertFalse(report.exists())

    def test_report_publication_failure_rolls_back_new_pptx(self) -> None:
        publish = _required_callable(ATOMIC_WRITE, "publish_pair_no_overwrite")
        candidates = self.root / "transaction"
        candidates.mkdir()
        pptx_candidate = candidates / "candidate.pptx"
        report_candidate = candidates / "candidate.json"
        pptx_candidate.write_bytes(b"new-pptx")
        report_candidate.write_bytes(b"new-report")
        output = self.root / "page.pptx"
        report = self.root / "build-report.json"
        real_link = os.link

        def fail_report(source, destination, *args, **kwargs):
            if Path(destination) == report:
                raise OSError("injected report publication failure")
            return real_link(source, destination, *args, **kwargs)

        with mock.patch.object(ATOMIC_WRITE.os, "link", side_effect=fail_report):
            with self.assertRaises(ToolError) as raised:
                publish(pptx_candidate, report_candidate, output, report)

        self.assertEqual(raised.exception.code, "BUILD_OUTPUT_INCOMPLETE")
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_directory_fsync_failure_after_either_link_rolls_back_the_pair(self) -> None:
        publish = _required_callable(ATOMIC_WRITE, "publish_pair_no_overwrite")
        for failure_call in (1, 2):
            with self.subTest(failure_call=failure_call):
                root = self.root / str(failure_call)
                candidates = root / "transaction"
                candidates.mkdir(parents=True)
                pptx_candidate = candidates / "candidate.pptx"
                report_candidate = candidates / "candidate.json"
                pptx_candidate.write_bytes(b"pptx")
                report_candidate.write_bytes(b"report")
                output = root / "page.pptx"
                report = root / "build-report.json"
                calls = 0

                def fail_selected_directory_sync(path):
                    nonlocal calls
                    calls += 1
                    if calls == failure_call:
                        raise ToolError(
                            "BUILD_OUTPUT_INCOMPLETE",
                            str(path),
                            "injected directory fsync failure",
                        )

                with mock.patch.object(
                    ATOMIC_WRITE,
                    "_fsync_directory",
                    side_effect=fail_selected_directory_sync,
                ):
                    with self.assertRaises(ToolError):
                        publish(
                            pptx_candidate,
                            report_candidate,
                            output,
                            report,
                        )

                self.assertFalse(output.exists())
                self.assertFalse(report.exists())

    def test_directory_close_failures_preserve_stable_diagnostics_and_rollback(self) -> None:
        publish = _required_callable(ATOMIC_WRITE, "publish_pair_no_overwrite")
        real_close = os.close
        for name, fsync_failure, expected_detail in (
            ("close-only", None, "cannot close output directory"),
            (
                "fsync-and-close",
                OSError("fsync failed"),
                "cannot fsync output directory",
            ),
        ):
            with self.subTest(name=name):
                root = self.root / name
                candidates = root / "transaction"
                candidates.mkdir(parents=True)
                pptx_candidate = candidates / "candidate.pptx"
                report_candidate = candidates / "candidate.json"
                pptx_candidate.write_bytes(b"pptx")
                report_candidate.write_bytes(b"report")
                output = root / "page.pptx"
                report = root / "build-report.json"

                def close_then_fail(descriptor):
                    real_close(descriptor)
                    raise OSError("close failed")

                fsync_patch = (
                    mock.patch.object(ATOMIC_WRITE.os, "fsync", return_value=None)
                    if fsync_failure is None
                    else mock.patch.object(
                        ATOMIC_WRITE.os, "fsync", side_effect=fsync_failure
                    )
                )
                with mock.patch.object(ATOMIC_WRITE, "_fsync_file", return_value=None):
                    with fsync_patch, mock.patch.object(
                        ATOMIC_WRITE.os, "close", side_effect=close_then_fail
                    ):
                        error = None
                        try:
                            publish(
                                pptx_candidate,
                                report_candidate,
                                output,
                                report,
                            )
                        except Exception as exc:  # assertion below owns the type
                            error = exc

                self.assertIsInstance(error, ToolError)
                assert isinstance(error, ToolError)
                self.assertEqual(error.detail, expected_detail)
                self.assertFalse(output.exists())
                self.assertFalse(report.exists())

    def test_directory_fsync_process_control_exceptions_always_close_descriptor(self) -> None:
        fsync_directory = _required_callable(ATOMIC_WRITE, "_fsync_directory")
        descriptor = 314
        for exception_type in (KeyboardInterrupt, SystemExit, MemoryError):
            with self.subTest(exception_type=exception_type.__name__):
                failure = exception_type("stop")
                close_calls: list[int] = []
                with mock.patch.object(
                    ATOMIC_WRITE.os, "open", return_value=descriptor
                ), mock.patch.object(
                    ATOMIC_WRITE.os, "fsync", side_effect=failure
                ), mock.patch.object(
                    ATOMIC_WRITE.os,
                    "close",
                    side_effect=lambda value: close_calls.append(value),
                ):
                    raised = None
                    try:
                        fsync_directory(self.root)
                    except BaseException as exc:  # identity assertions own the type
                        raised = exc

                self.assertIs(raised, failure)
                self.assertEqual(close_calls, [descriptor])

    def test_directory_close_oserror_does_not_override_process_control_failure(self) -> None:
        fsync_directory = _required_callable(ATOMIC_WRITE, "_fsync_directory")
        descriptor = 271
        failure = KeyboardInterrupt("stop")
        close_calls: list[int] = []

        def close_then_fail(value: int) -> None:
            close_calls.append(value)
            raise OSError("close failed")

        with mock.patch.object(
            ATOMIC_WRITE.os, "open", return_value=descriptor
        ), mock.patch.object(
            ATOMIC_WRITE.os, "fsync", side_effect=failure
        ), mock.patch.object(
            ATOMIC_WRITE.os, "close", side_effect=close_then_fail
        ):
            raised = None
            try:
                fsync_directory(self.root)
            except BaseException as exc:  # identity assertions own the type
                raised = exc

        self.assertIs(raised, failure)
        self.assertEqual(close_calls, [descriptor])

    def test_post_link_exception_after_either_publication_rolls_back_the_pair(self) -> None:
        publish = _required_callable(ATOMIC_WRITE, "publish_pair_no_overwrite")
        for failure_call in (1, 2):
            with self.subTest(failure_call=failure_call):
                root = self.root / f"post-link-{failure_call}"
                candidates = root / "transaction"
                candidates.mkdir(parents=True)
                pptx_candidate = candidates / "candidate.pptx"
                report_candidate = candidates / "candidate.json"
                pptx_candidate.write_bytes(b"pptx")
                report_candidate.write_bytes(b"report")
                output = root / "page.pptx"
                report = root / "build-report.json"
                real_link = os.link
                calls = 0

                def link_then_fail(source, destination, *args, **kwargs):
                    nonlocal calls
                    calls += 1
                    result = real_link(source, destination, *args, **kwargs)
                    if calls == failure_call:
                        raise OSError("injected post-link failure")
                    return result

                with mock.patch.object(
                    ATOMIC_WRITE.os, "link", side_effect=link_then_fail
                ):
                    with self.assertRaises(ToolError):
                        publish(
                            pptx_candidate,
                            report_candidate,
                            output,
                            report,
                        )

                self.assertFalse(output.exists())
                self.assertFalse(report.exists())

    def test_cross_device_publication_failure_leaves_no_outputs(self) -> None:
        publish = _required_callable(ATOMIC_WRITE, "publish_pair_no_overwrite")
        candidates = self.root / "transaction"
        candidates.mkdir()
        pptx_candidate = candidates / "candidate.pptx"
        report_candidate = candidates / "candidate.json"
        pptx_candidate.write_bytes(b"pptx")
        report_candidate.write_bytes(b"report")
        output = self.root / "page.pptx"
        report = self.root / "build-report.json"

        with mock.patch.object(
            ATOMIC_WRITE.os,
            "link",
            side_effect=OSError(errno.EXDEV, "cross-device link"),
        ):
            with self.assertRaises(ToolError) as raised:
                publish(pptx_candidate, report_candidate, output, report)

        self.assertEqual(raised.exception.code, "BUILD_OUTPUT_INCOMPLETE")
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_report_candidate_write_failure_is_cli_json_and_publishes_nothing(self) -> None:
        spec_path, prebuild_path = write_valid_fixture(
            self.root, make_native_list_spec(self.root)
        )
        output = self.root / "page.pptx"
        report = self.root / "build-report.json"
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch.object(
            BUILDER, "atomic_write_json", side_effect=OSError("disk full")
        ):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                error = None
                try:
                    return_code = BUILDER.main(
                        [
                            "--spec",
                            str(spec_path),
                            "--prebuild-report",
                            str(prebuild_path),
                            "--output",
                            str(output),
                            "--build-report",
                            str(report),
                        ]
                    )
                except Exception as exc:  # behavior assertion below owns the type
                    error = exc

        self.assertIsNone(error)
        self.assertEqual(return_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        failure = json.loads(stdout.getvalue())
        self.assertEqual(failure["valid"], False)
        self.assertEqual(failure["errors"][0]["code"], "BUILD_OUTPUT_INCOMPLETE")
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_second_output_parent_failure_reports_the_real_parent(self) -> None:
        root = self.root / "mkdir-failure"
        spec_path, prebuild_path = write_valid_fixture(
            root, make_native_list_spec(root)
        )
        output_parent = root / "pptx-output"
        report_parent = root / "report-output"
        output = output_parent / "page.pptx"
        report = report_parent / "build-report.json"
        real_mkdir = Path.mkdir
        resolved_report_parent = report_parent.resolve()

        def fail_report_parent(path, *args, **kwargs):
            if path == resolved_report_parent:
                raise OSError("report parent unavailable")
            return real_mkdir(path, *args, **kwargs)

        with mock.patch.object(
            Path, "mkdir", autospec=True, side_effect=fail_report_parent
        ):
            error = None
            try:
                BUILDER.compile_single_page(
                    spec_path, prebuild_path, output, report
                )
            except Exception as exc:  # assertion below owns the type
                error = exc

        self.assertIsInstance(error, ToolError)
        assert isinstance(error, ToolError)
        self.assertEqual(error.path, str(report_parent.resolve()))
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_pair_publisher_rejects_aliases_and_separate_candidate_dirs(self) -> None:
        publish = _required_callable(ATOMIC_WRITE, "publish_pair_no_overwrite")
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        pptx_candidate = first / "candidate.pptx"
        report_candidate = second / "candidate.json"
        pptx_candidate.write_bytes(b"pptx")
        report_candidate.write_bytes(b"report")
        output = self.root / "page.pptx"
        report = self.root / "build-report.json"

        cases = (
            (pptx_candidate, pptx_candidate, output, report),
            (pptx_candidate, report_candidate, output, report),
            (pptx_candidate, first / "candidate.json", output, output),
        )
        (first / "candidate.json").write_bytes(b"report")
        for args in cases:
            with self.subTest(args=tuple(map(str, args))):
                with self.assertRaises(ToolError) as raised:
                    publish(*args)
                self.assertEqual(raised.exception.code, "BUILD_OUTPUT_INCOMPLETE")
                self.assertFalse(output.exists())
                self.assertFalse(report.exists())

    def test_pair_publisher_rejects_missing_output_parent_without_publication(self) -> None:
        publish = _required_callable(ATOMIC_WRITE, "publish_pair_no_overwrite")
        candidates = self.root / "transaction"
        candidates.mkdir()
        pptx_candidate = candidates / "candidate.pptx"
        report_candidate = candidates / "candidate.json"
        pptx_candidate.write_bytes(b"pptx")
        report_candidate.write_bytes(b"report")
        output = self.root / "missing" / "page.pptx"
        report = self.root / "missing" / "build-report.json"

        with self.assertRaises(ToolError) as raised:
            publish(pptx_candidate, report_candidate, output, report)

        self.assertEqual(raised.exception.code, "BUILD_OUTPUT_INCOMPLETE")
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_cli_failure_is_one_stable_json_object_and_publishes_no_report(self) -> None:
        spec_path, prebuild_path = write_valid_fixture(
            self.root, make_native_list_spec(self.root)
        )
        output = self.root / "page.pptx"
        report = self.root / "build-report.json"
        output.write_bytes(b"user-owned")

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "build_pptx_from_spec.py"),
                "--spec",
                str(spec_path),
                "--prebuild-report",
                str(prebuild_path),
                "--output",
                str(output),
                "--build-report",
                str(report),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "valid": False,
                "errors": [
                    {
                        "code": "BUILD_OUTPUT_INCOMPLETE",
                        "path": str(output.resolve()),
                        "detail": "output path already exists",
                    }
                ],
            },
        )
        self.assertEqual(output.read_bytes(), b"user-owned")
        self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
