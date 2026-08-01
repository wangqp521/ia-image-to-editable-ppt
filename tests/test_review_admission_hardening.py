from __future__ import annotations

import contextlib
import copy
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lib import (
    atomic_write,
    no_replace_transactions as transactions,
    review_contracts as contracts,
)
from lib.background_contracts import validate_background_prebuild
from lib.error_codes import ToolError
from lib.hashing import canonical_json_sha256, file_sha256
from lib.spec_identity import content_spec_sha256, input_spec_sha256
from tests.test_review_admission import AdmissionFixture, _load_script, _same_path
from tests.test_background_contracts import background_spec
from tests.fixture_specs import make_asset_fallback_spec


def _picture_background_spec(root: Path) -> dict[str, Any]:
    return background_spec(root, mode="background_picture", kind="picture")


class AssetAdmissionFixture(AdmissionFixture):
    SPEC_FACTORY = staticmethod(make_asset_fallback_spec)

    @property
    def representation_asset(self) -> Path:
        for element in self.spec["elements"]:
            content = element.get("content")
            if isinstance(content, dict) and isinstance(content.get("asset"), dict):
                return Path(content["asset"]["path"])
        raise AssertionError("asset fallback fixture must contain a representation asset")

    def rebind_changed_spec_chain(self) -> None:
        content_hash = content_spec_sha256(self.spec)
        input_hash = input_spec_sha256(self.spec)
        self.build_report.update(
            {
                "schema_sha256": canonical_json_sha256(self.spec),
                "content_spec_sha256": content_hash,
                "input_spec_sha256": input_hash,
            }
        )
        self.text_report.update(
            {"spec_sha256": content_hash, "input_spec_sha256": input_hash}
        )
        self.text_report["inputs"].update(
            {"spec_sha256": content_hash, "input_spec_sha256": input_hash}
        )
        self.background_report.update(
            {"spec_sha256": content_hash, "input_spec_sha256": input_hash}
        )
        self._rebind_chain()


class OriginalSpecPathSemanticsTests(AssetAdmissionFixture):
    """The production gate must validate the paths bound by the original spec."""

    def test_issue_rejects_byte_identical_representation_asset_leaf_symlink(self) -> None:
        asset = self.representation_asset
        target = asset.with_name("byte-identical-target.png")
        target.write_bytes(asset.read_bytes())
        asset.unlink()
        asset.symlink_to(target)

        self.assert_issue_rejected()

    def test_issue_rejects_representation_asset_under_symlink_parent(self) -> None:
        asset = self.representation_asset
        target_parent = self.fixture / "real-asset-parent"
        target_parent.mkdir()
        target = target_parent / asset.name
        target.write_bytes(asset.read_bytes())
        alias_parent = self.fixture / "asset-parent-alias"
        alias_parent.symlink_to(target_parent, target_is_directory=True)
        for element in self.spec["elements"]:
            content = element.get("content")
            if isinstance(content, dict) and isinstance(content.get("asset"), dict):
                content["asset"]["path"] = str(alias_parent / asset.name)
        self.rebind_changed_spec_chain()

        self.assert_issue_rejected()

    def test_issue_does_not_replace_original_asset_suffix_semantics(self) -> None:
        asset = self.representation_asset
        target = self.fixture / "actual-png-content.png"
        target.write_bytes(asset.read_bytes())
        misleading_alias = self.fixture / "declared-as-jpeg.jpg"
        misleading_alias.symlink_to(target)
        for element in self.spec["elements"]:
            content = element.get("content")
            if isinstance(content, dict) and isinstance(content.get("asset"), dict):
                content["asset"]["path"] = str(misleading_alias)
        self.rebind_changed_spec_chain()

        self.assert_issue_rejected()


class OriginalSpecProductionGateTests(AssetAdmissionFixture):
    SPEC_FACTORY = staticmethod(_picture_background_spec)

    def test_issue_rejects_original_background_provenance_string_mismatch(self) -> None:
        background = self.spec["modules"]["background"]["items"][0]
        asset_path = self.representation_asset
        background["source_provenance"]["source_path"] = (
            f"{asset_path.parent}/./{asset_path.name}"
        )
        self.rebind_changed_spec_chain()

        issue_codes = {
            issue.code for issue in validate_background_prebuild(self.spec)
        }
        self.assertIn("BACKGROUND_PROVENANCE_INVALID", issue_codes)
        self.assert_issue_rejected()


class CompleteEvidenceSnapshotTests(AssetAdmissionFixture):
    """Every stable nested snapshot must stay current through publication."""

    def _drift_targets(self) -> list[tuple[str, Path]]:
        return [
            ("soffice", self.soffice),
            ("pdftoppm", self.pdftoppm),
            ("pdffonts", self.pdffonts),
            ("pdftotext", self.pdftotext),
            ("fontconfig", self.fontconfig),
            ("representation_asset", self.representation_asset),
        ]

    @staticmethod
    def _persistently_drift(path: Path) -> None:
        path.write_bytes(path.read_bytes() + b"\nreview-evidence-drift\n")

    def test_issue_rejects_all_nested_evidence_drift_after_stable_validation(self) -> None:
        real_prompt = contracts.reviewer_prompt
        for index, (label, path) in enumerate(self._drift_targets()):
            with self.subTest(evidence=label):
                self._restore_fixture()
                output = self.root / f"issue-drift-{index}"

                def drift_then_prompt(admission: dict[str, Any]) -> str:
                    self._persistently_drift(path)
                    return real_prompt(admission)

                with mock.patch.object(
                    contracts, "reviewer_prompt", side_effect=drift_then_prompt
                ):
                    with self.assertRaises(ToolError) as raised:
                        self.issue(output=output)
                self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)
                self.assertFalse(output.exists())

    def test_invocation_rejects_all_nested_evidence_drift_before_publication(self) -> None:
        real_scan = contracts._scan_invocations
        for index, (label, path) in enumerate(self._drift_targets()):
            with self.subTest(evidence=label):
                self._restore_fixture()
                output = self.root / f"prepublish-admission-{index}"
                invocation_dir = self.root / f"prepublish-invocations-{index}"
                self.issue(output=output)

                def scan_then_drift(*args: Any, **kwargs: Any) -> None:
                    real_scan(*args, **kwargs)
                    self._persistently_drift(path)

                with mock.patch.object(
                    contracts, "_scan_invocations", side_effect=scan_then_drift
                ):
                    with self.assertRaises(ToolError) as raised:
                        contracts.record_invocation(
                            output / "review-admission.json", invocation_dir
                        )
                self.assertEqual("REVIEW_ADMISSION_STALE", raised.exception.code)
                self.assertFalse(
                    (invocation_dir / "page-001-round-1-invocation.json").exists()
                )

    def test_invocation_rolls_back_when_nested_evidence_drifts_after_publication(self) -> None:
        real_publish = contracts.publish_json_no_overwrite
        for index, (label, path) in enumerate(self._drift_targets()):
            with self.subTest(evidence=label):
                self._restore_fixture()
                output = self.root / f"postpublish-admission-{index}"
                invocation_dir = self.root / f"postpublish-invocations-{index}"
                self.issue(output=output)

                def publish_then_drift(destination: Path, payload: Any):
                    receipt = real_publish(destination, payload)
                    self._persistently_drift(path)
                    return receipt

                with mock.patch.object(
                    contracts,
                    "publish_json_no_overwrite",
                    side_effect=publish_then_drift,
                ):
                    with self.assertRaises(ToolError) as raised:
                        contracts.record_invocation(
                            output / "review-admission.json", invocation_dir
                        )
                self.assertEqual("REVIEW_ADMISSION_STALE", raised.exception.code)
                self.assertFalse(
                    (invocation_dir / "page-001-round-1-invocation.json").exists()
                )

    def test_issue_rolls_back_when_nested_evidence_drifts_after_publication(self) -> None:
        real_publish = contracts._publish_admission_directory
        targets = (
            ("pdftoppm", self.pdftoppm),
            ("fontconfig", self.fontconfig),
            ("representation_asset", self.representation_asset),
        )
        for index, (label, path) in enumerate(targets):
            with self.subTest(evidence=label):
                self._restore_fixture()
                output = self.root / f"issue-postpublish-drift-{index}"

                def publish_then_drift(
                    destination: Path, admission: dict[str, Any], prompt: str
                ):
                    receipt = real_publish(destination, admission, prompt)
                    self._persistently_drift(path)
                    return receipt

                with mock.patch.object(
                    contracts,
                    "_publish_admission_directory",
                    side_effect=publish_then_drift,
                ):
                    with self.assertRaises(ToolError) as raised:
                        self.issue(output=output)
                self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)
                self.assertFalse(output.exists())

    def test_issue_rollback_preserves_competitor_replacing_published_directory(
        self,
    ) -> None:
        competitor = self.root / "postpublish-competitor"
        competitor.mkdir()
        competitor_payloads = {
            "review-admission.json": b"competitor admission\n",
            "reviewer-prompt.txt": b"competitor prompt\n",
        }
        for name, payload in competitor_payloads.items():
            (competitor / name).write_bytes(payload)
        saved_owned = self.root / "saved-owned-postpublish-admission"
        real_publish = contracts._publish_admission_directory

        def publish_replace_and_drift(
            destination: Path, admission: dict[str, Any], prompt: str
        ):
            receipt = real_publish(destination, admission, prompt)
            os.rename(self.output, saved_owned)
            os.rename(competitor, self.output)
            self._persistently_drift(self.pdftoppm)
            return receipt

        with mock.patch.object(
            contracts,
            "_publish_admission_directory",
            side_effect=publish_replace_and_drift,
        ):
            with self.assertRaises(ToolError) as raised:
                self.issue()

        self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)
        self.assertTrue(saved_owned.is_dir())
        self.assertEqual(
            competitor_payloads,
            {
                name: (self.output / name).read_bytes()
                for name in competitor_payloads
            },
        )

    def test_issue_rejects_fixed_directory_replaced_after_publisher_return(
        self,
    ) -> None:
        """Removing the caller's final receipt check must make this test fail."""
        competitor = self.root / "receipt-competitor"
        competitor.mkdir()
        competitor_payloads = {
            "review-admission.json": b"{}\n",
            "reviewer-prompt.txt": b"competitor prompt\n",
        }
        for name, payload in competitor_payloads.items():
            (competitor / name).write_bytes(payload)
        saved_owned = self.root / "saved-owned-receipt-admission"
        real_publish = contracts._publish_admission_directory

        def publish_then_replace(
            destination: Path, admission: dict[str, Any], prompt: str
        ):
            receipt = real_publish(destination, admission, prompt)
            os.rename(destination, saved_owned)
            os.rename(competitor, destination)
            return receipt

        with mock.patch.object(
            contracts,
            "_publish_admission_directory",
            side_effect=publish_then_replace,
        ):
            with self.assertRaises(ToolError) as raised:
                self.issue()

        self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)
        self.assertTrue(saved_owned.is_dir())
        self.assertEqual(
            competitor_payloads,
            {
                name: (self.output / name).read_bytes()
                for name in competitor_payloads
            },
        )

    def test_issue_rejects_extra_member_added_during_final_receipt_check(
        self,
    ) -> None:
        """A final receipt must not issue a directory with an added member."""
        real_verify = contracts.verify_directory_receipt
        real_listdir = os.listdir
        state = {"verifying": False, "added": False}

        def listdir_then_add(descriptor: int):
            names = real_listdir(descriptor)
            if state["verifying"] and not state["added"]:
                extra = os.open(
                    "extra.txt",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=descriptor,
                )
                try:
                    os.write(extra, b"extra member\n")
                    os.fsync(extra)
                finally:
                    os.close(extra)
                state["added"] = True
            return names

        def verify_with_extra_member(receipt: Any) -> None:
            state["verifying"] = True
            try:
                real_verify(receipt)
            finally:
                state["verifying"] = False

        with mock.patch.object(
            contracts,
            "verify_directory_receipt",
            side_effect=verify_with_extra_member,
        ), mock.patch.object(
            transactions.os,
            "listdir",
            side_effect=listdir_then_add,
        ):
            with self.assertRaises(ToolError) as raised:
                self.issue()
        self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)
        self.assertTrue(state["added"])
        self.assertFalse(self.output.exists())


class RuntimeRequestedAliasBindingTests(AdmissionFixture):
    """Each lexical requested path stays bound independently of its target."""

    def _bind_alias(self, kind: str, index: int) -> Callable[[], None]:
        if kind == "leaf":
            alias = self.fixture / f"pdftoppm-leaf-alias-{index}"
            alias.symlink_to(self.pdftoppm)

            def retarget() -> None:
                alias.unlink()
                alias.symlink_to(self.pdffonts)

            requested = alias
        else:
            alternate = self.fixture / f"alternate-tools-{index}"
            alternate.mkdir()
            alternate_tool = alternate / self.pdftoppm.name
            shutil.copy2(self.pdffonts, alternate_tool)
            alternate_tool.chmod(0o755)
            alias_parent = self.fixture / f"tools-parent-alias-{index}"
            alias_parent.symlink_to(self.pdftoppm.parent, target_is_directory=True)

            def retarget() -> None:
                alias_parent.unlink()
                alias_parent.symlink_to(alternate, target_is_directory=True)

            requested = alias_parent / self.pdftoppm.name
        self.runtime["executables"]["pdftoppm"]["requested"] = str(requested)
        self._rebind_chain()
        return retarget

    def test_issue_rejects_requested_alias_retarget_after_validation(self) -> None:
        """Resolving requested before deduplication must make both cases fail."""
        real_prompt = contracts.reviewer_prompt
        for index, kind in enumerate(("leaf", "parent")):
            with self.subTest(kind=kind):
                self._restore_fixture()
                retarget = self._bind_alias(kind, index)
                output = self.root / f"alias-issue-{kind}"

                def retarget_then_prompt(admission: dict[str, Any]) -> str:
                    retarget()
                    return real_prompt(admission)

                with mock.patch.object(
                    contracts,
                    "reviewer_prompt",
                    side_effect=retarget_then_prompt,
                ):
                    with self.assertRaises(ToolError) as raised:
                        self.issue(output=output)
                self.assertEqual(
                    "REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code
                )
                self.assertFalse(output.exists())

    def test_invocation_rejects_requested_alias_retarget_and_rolls_back(
        self,
    ) -> None:
        """Dropping invocation lexical checks must publish a forbidden record."""
        real_scan = contracts._scan_invocations
        for index, kind in enumerate(("leaf", "parent"), start=10):
            with self.subTest(kind=kind):
                self._restore_fixture()
                retarget = self._bind_alias(kind, index)
                output = self.root / f"alias-invocation-admission-{kind}"
                invocation_dir = self.root / f"alias-invocations-{kind}"
                self.issue(output=output)

                def scan_then_retarget(*args: Any, **kwargs: Any) -> None:
                    real_scan(*args, **kwargs)
                    retarget()

                with mock.patch.object(
                    contracts,
                    "_scan_invocations",
                    side_effect=scan_then_retarget,
                ):
                    with self.assertRaises(ToolError) as raised:
                        contracts.record_invocation(
                            output / "review-admission.json", invocation_dir
                        )
                self.assertEqual("REVIEW_ADMISSION_STALE", raised.exception.code)
                self.assertFalse(
                    (
                        invocation_dir
                        / "page-001-round-1-invocation.json"
                    ).exists()
                )


@unittest.skipUnless(
    sys.platform == "darwin" and Path("/opt/homebrew/bin/pdffonts").is_file(),
    "requires macOS Homebrew pdffonts",
)
class HomebrewPdffontsStableRuntimeTests(AdmissionFixture):
    def test_validate_response_uses_locked_dylibs_without_ambient_loader_env(
        self,
    ) -> None:
        pdffonts = Path("/opt/homebrew/bin/pdffonts")
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("DYLD_") and not key.startswith("LD_")
        }
        completed = subprocess.run(
            [str(pdffonts), str(self.pdf)],
            check=False,
            capture_output=True,
            text=True,
            env=clean_env,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

        preflight = _load_script(
            "preflight_runtime.py",
            f"homebrew_preflight_runtime_{self._generation}",
        )
        args = preflight._parse_args(
            [
                "--soffice",
                str(self.soffice),
                "--pdftoppm",
                str(self.pdftoppm),
                "--pdffonts",
                str(pdffonts),
                "--pdftotext",
                str(self.pdftotext),
                "--fontconfig",
                str(self.fontconfig),
                "--python-module",
                "json",
                "--output",
                str(self.runtime_path),
            ]
        )
        self.pdffonts = pdffonts.resolve(strict=True)
        self.runtime = preflight.inspect_runtime(args)
        self.assertTrue(self.runtime["valid"], self.runtime)

        self.raw_font_report_path.write_text(completed.stdout, encoding="utf-8")
        renderer = _load_script(
            "render_preview.py",
            f"homebrew_render_preview_{self._generation}",
        )
        resolved_fonts = renderer._parse_pdffonts(completed.stdout)
        self.font_report_path.write_text(
            json.dumps(
                {"resolved_fonts": resolved_fonts},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.render_report["font_report"].update(
            {
                "sha256": file_sha256(self.font_report_path),
                "raw_sha256": file_sha256(self.raw_font_report_path),
                "resolved_fonts": resolved_fonts,
            }
        )
        self._rebind_chain()

        with mock.patch.dict(os.environ, clean_env, clear=True):
            admission = self.issue()
            invocation = contracts.record_invocation(
                self.admission_path, self.invocations
            )
            report = self.validate_response(
                self.admission_path,
                self.invocation_path,
                self.valid_response(admission),
            )

        self.assertEqual(admission["admission_id"], invocation["admission_id"])
        self.assertTrue(report["valid"], report)


class StableEvidenceViewTests(AdmissionFixture):
    """A validator must consume the exact bytes bound by the admission."""

    def _swap_only_original_path(
        self,
        path: Path,
        valid_bytes: bytes,
        real_call: Callable[..., Any],
    ) -> Callable[..., Any]:
        def wrapped(candidate: str | Path, *args: Any, **kwargs: Any) -> Any:
            candidate_path = Path(candidate).resolve(strict=False)
            if candidate_path != path.resolve(strict=False):
                return real_call(candidate, *args, **kwargs)
            invalid_bytes = path.read_bytes()
            path.write_bytes(valid_bytes)
            try:
                return real_call(candidate, *args, **kwargs)
            finally:
                path.write_bytes(invalid_bytes)

        return wrapped

    def _pil_swap_only_original_path(
        self, path: Path, valid_bytes: bytes, real_open: Callable[..., Any]
    ) -> Callable[..., Any]:
        def wrapped(candidate: Any, *args: Any, **kwargs: Any) -> Any:
            if not isinstance(candidate, (str, os.PathLike)) or not _same_path(
                candidate, path
            ):
                return real_open(candidate, *args, **kwargs)
            invalid_bytes = path.read_bytes()
            path.write_bytes(valid_bytes)
            try:
                opened = real_open(candidate, *args, **kwargs)
                opened.load()
                return opened
            finally:
                path.write_bytes(invalid_bytes)

        return wrapped

    def test_swap_validate_restore_pdf_is_rejected(self) -> None:
        valid_pdf = self.pdf.read_bytes()
        self.pdf.write_bytes(b"not-a-real-pdf\n")
        self.render_report["pdf"]["sha256"] = file_sha256(self.pdf)
        self._rebind_chain()
        renderer = __import__("render_preview")
        real_inspect = renderer._inspect_pdf

        with mock.patch.object(
            renderer,
            "_inspect_pdf",
            side_effect=self._swap_only_original_path(
                self.pdf, valid_pdf, real_inspect
            ),
        ):
            self.assert_issue_rejected()
        self.assertEqual(b"not-a-real-pdf\n", self.pdf.read_bytes())

    def test_swap_validate_restore_preview_and_source_are_rejected(self) -> None:
        for label, path, mutate in (
            (
                "preview",
                self.preview,
                lambda: Image.new("RGB", (1920, 1080), "white").save(self.preview),
            ),
            ("source", self.source, lambda: self.source.write_bytes(b"not-an-image\n")),
        ):
            with self.subTest(label=label):
                valid_bytes = path.read_bytes()
                mutate()
                if label == "preview":
                    self.render_report["preview"]["sha256"] = file_sha256(path)
                self._rebind_chain()
                real_open = Image.open
                with mock.patch.object(
                    Image,
                    "open",
                    side_effect=self._pil_swap_only_original_path(
                        path, valid_bytes, real_open
                    ),
                ):
                    self.assert_issue_rejected()
                self._restore_fixture()

    def test_swap_validate_restore_pptx_is_rejected(self) -> None:
        valid_pptx = self.pptx.read_bytes()
        self.pptx.write_bytes(b"not-a-pptx\n")
        digest = file_sha256(self.pptx)
        self.build_report["pptx_sha256"] = digest
        self.structure_report["pptx_sha256"] = digest
        self.render_report["pptx"]["sha256"] = digest
        self.text_report["pptx_sha256"] = digest
        self.text_report["inputs"]["pptx_sha256"] = digest
        self.background_report["pptx_sha256"] = digest
        self.visual_report["pptx_sha256"] = digest
        self._rebind_chain()
        validator = __import__("validate_pptx")
        real_validate = validator.validate_pptx

        with mock.patch.object(
            validator,
            "validate_pptx",
            side_effect=self._swap_only_original_path(
                self.pptx, valid_pptx, real_validate
            ),
        ):
            self.assert_issue_rejected()
        self.assertEqual(b"not-a-pptx\n", self.pptx.read_bytes())

    def test_production_pptx_and_image_validators_receive_private_paths(self) -> None:
        validator = __import__("validate_pptx")
        real_validate = validator.validate_pptx
        real_open = Image.open
        seen_pptx: list[Path] = []
        seen_images: list[Path] = []

        def record_pptx(path: Path, *args: Any, **kwargs: Any) -> Any:
            seen_pptx.append(Path(path).resolve(strict=False))
            return real_validate(path, *args, **kwargs)

        def record_image(path: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(path, (str, os.PathLike)):
                seen_images.append(Path(path).resolve(strict=False))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(
            validator, "validate_pptx", side_effect=record_pptx
        ), mock.patch.object(Image, "open", side_effect=record_image):
            self.issue()
        self.assertTrue(seen_pptx)
        self.assertNotIn(self.pptx.resolve(), seen_pptx)
        self.assertNotIn(self.source.resolve(), seen_images)
        self.assertNotIn(self.preview.resolve(), seen_images)

    def test_swap_validate_restore_runtime_tool_is_rejected(self) -> None:
        valid_tool = self.pdftoppm.read_bytes()
        self.pdftoppm.write_text(
            "#!/usr/bin/env python3\nraise SystemExit(9)\n", encoding="utf-8"
        )
        self.pdftoppm.chmod(0o755)
        preflight = __import__("preflight_runtime")
        renderer = __import__("render_preview")
        real_inspect = preflight.inspect_runtime
        real_load = renderer._load_runtime

        def swap_for_call(real_call: Callable[..., Any]) -> Callable[..., Any]:
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                invalid = self.pdftoppm.read_bytes()
                self.pdftoppm.write_bytes(valid_tool)
                self.pdftoppm.chmod(0o755)
                try:
                    return real_call(*args, **kwargs)
                finally:
                    self.pdftoppm.write_bytes(invalid)
                    self.pdftoppm.chmod(0o755)

            return wrapped

        with mock.patch.object(
            preflight, "inspect_runtime", side_effect=swap_for_call(real_inspect)
        ), mock.patch.object(
            renderer, "_load_runtime", side_effect=swap_for_call(real_load)
        ):
            self.assert_issue_rejected()

    def test_runtime_tool_drift_around_current_preflight_is_rejected(self) -> None:
        preflight = __import__("preflight_runtime")
        real_inspect = preflight.inspect_runtime
        for timing, expected_current_valid in (("before", False), ("after", True)):
            with self.subTest(timing=timing):
                observations: list[dict[str, Any]] = []

                def drift_tool() -> None:
                    replacement = self.soffice.with_name(
                        f"replacement-soffice-{timing}"
                    )
                    replacement.write_text(
                        "#!/usr/bin/env python3\n"
                        "print('LibreOffice 99.0')\n",
                        encoding="utf-8",
                    )
                    replacement.chmod(0o755)
                    if timing == "before":
                        self.soffice.write_bytes(replacement.read_bytes())
                        self.soffice.chmod(0o755)
                    else:
                        os.replace(replacement, self.soffice)

                def inspect_with_drift(*args: Any, **kwargs: Any) -> Any:
                    if timing == "before":
                        drift_tool()
                    current = real_inspect(*args, **kwargs)
                    observations.append(current)
                    if timing == "after":
                        drift_tool()
                    return current

                with mock.patch.object(
                    preflight, "inspect_runtime", side_effect=inspect_with_drift
                ):
                    self.assert_issue_rejected()

                self.assertEqual(1, len(observations))
                self.assertIs(
                    expected_current_valid,
                    observations[0]["valid"],
                )
                self._restore_fixture()

    def test_expected_runtime_snapshot_survives_mutable_runtime_drift(self) -> None:
        preflight = __import__("preflight_runtime")
        real_inspect = preflight.inspect_runtime
        for mutation in ("content", "path-replacement"):
            with self.subTest(mutation=mutation):
                observations: list[tuple[Path, dict[str, Any]]] = []

                def drift_runtime() -> None:
                    if mutation == "content":
                        self.runtime_path.write_text(
                            '{"forged": true}\n', encoding="utf-8"
                        )
                        return
                    replacement = self.runtime_path.with_name(
                        "replacement-runtime.json"
                    )
                    replacement.write_bytes(self.runtime_path.read_bytes())
                    os.replace(replacement, self.runtime_path)

                def inspect_after_runtime_drift(
                    arguments: Any,
                ) -> dict[str, Any]:
                    drift_runtime()
                    current = real_inspect(arguments)
                    observations.append((Path(arguments.expected_runtime), current))
                    return current

                with mock.patch.object(
                    preflight,
                    "inspect_runtime",
                    side_effect=inspect_after_runtime_drift,
                ):
                    self.assert_issue_rejected()

                self.assertEqual(1, len(observations))
                expected_runtime, current = observations[0]
                self.assertNotEqual(
                    self.runtime_path.resolve(strict=False),
                    expected_runtime.resolve(strict=False),
                )
                self.assertTrue(current["valid"], current)
                self.assertEqual(self.runtime, current)
                self._restore_fixture()

    def test_private_snapshot_paths_never_enter_admission_or_prompt(self) -> None:
        admission = self.issue()
        serialized = json.dumps(admission, ensure_ascii=False)
        prompt = self.prompt_path.read_text(encoding="utf-8")
        self.assertNotIn("review-evidence", serialized)
        self.assertNotIn("review-evidence", prompt)


class AdmissionPublicationOwnershipTests(AdmissionFixture):
    def test_staging_identity_is_captured_before_path_can_be_replaced(self) -> None:
        real_fsync = contracts._fsync_directory
        real_stat = contracts.os.stat
        real_rename = contracts.os.rename
        saved_owned = self.root / "saved-owned-staging"
        state = {"replaced": False, "extra": False}

        def replace_after_creation(path: Path) -> None:
            if _same_path(path, self.root) and not state["replaced"]:
                staging = next(self.root.glob(".admission.*.rollback"))
                real_rename(staging, saved_owned)
                staging.mkdir()
                state["replaced"] = True
            real_fsync(path)

        def add_attacker_member(path: str | Path, *args: Any, **kwargs: Any):
            candidate = Path(path)
            if (
                state["replaced"]
                and candidate.parent == self.root
                and candidate.name.startswith(".admission.")
                and candidate.name.endswith(".rollback")
                and not state["extra"]
            ):
                (candidate / "extra.txt").write_text("attacker\n", encoding="utf-8")
                state["extra"] = True
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(
            contracts, "_fsync_directory", side_effect=replace_after_creation
        ), mock.patch.object(contracts.os, "stat", side_effect=add_attacker_member):
            with self.assertRaises(ToolError) as raised:
                self.issue()
        self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)
        if self.output.exists():
            self.assertNotIn("extra.txt", {item.name for item in self.output.iterdir()})

    def test_six_cooperative_issuers_leave_no_loser_staging(self) -> None:
        self.output = self.root / "concurrent-clean-admission"
        self._write_payloads()

        def issue_once(_: int) -> str:
            try:
                contracts.issue_admission(self.inputs(), self.output)
                return "success"
            except ToolError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(issue_once, range(6)))
        self.assertEqual(1, results.count("success"))
        self.assertEqual(5, results.count("REVIEW_ADMISSION_ALREADY_EXISTS"))
        self.assertEqual([], list(self.root.glob(".concurrent-clean-admission.*")))

    def test_repeated_real_failures_stop_before_a_fourth_tombstone(self) -> None:
        """Removing the recovery count gate must grow four full tombstones."""
        self._write_payloads()
        details: list[str] = []
        real_rename = getattr(
            contracts,
            "_rename_directory_no_replace",
            contracts.os.rename,
        )

        def fail_publish(source: str | Path, destination: str | Path) -> None:
            if Path(destination).name.startswith("failed-admission-"):
                raise OSError("injected publication failure")
            real_rename(source, destination)

        with mock.patch.object(
            contracts,
            "_rename_directory_no_replace",
            side_effect=fail_publish,
        ):
            for index in range(4):
                with self.assertRaises(ToolError) as raised:
                    contracts.issue_admission(
                        self.inputs(), self.root / f"failed-admission-{index}"
                    )
                details.append(raised.exception.detail)

        tombstones = sorted(self.root.glob(".failed-admission-*.rollback"))
        self.assertEqual(3, len(tombstones))
        identities = {
            (path.stat().st_dev, path.stat().st_ino) for path in tombstones
        }
        self.assertEqual(3, len(identities))
        for detail in details[:3]:
            self.assertIn("retained_tombstone=", detail)
            self.assertIn("phase=", detail)
            self.assertIn("identity=", detail)
        self.assertIn("recovery tombstone limit", details[3])


class InvocationReceiptTests(AdmissionFixture):
    def test_fixed_invocation_replacement_after_publisher_return_is_rejected(self) -> None:
        self.issue()
        real_publish = contracts.publish_json_no_overwrite
        saved_owned = self.root / "saved-owned-invocation"

        def publish_then_replace(path: Path, payload: Any):
            receipt = real_publish(path, payload)
            os.rename(path, saved_owned)
            path.write_text('{"competitor": true}\n', encoding="utf-8")
            return receipt

        with mock.patch.object(
            contracts,
            "publish_json_no_overwrite",
            side_effect=publish_then_replace,
        ):
            with self.assertRaises(ToolError) as raised:
                contracts.record_invocation(self.admission_path, self.invocations)
        self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
        self.assertEqual(
            {"competitor": True},
            json.loads(self.invocation_path.read_text(encoding="utf-8")),
        )

    def test_final_receipt_rejects_same_inode_rewrite_after_eof(self) -> None:
        """A final receipt must reject a same-inode rewrite after EOF."""
        self.issue()
        real_verify = contracts.verify_file_receipt
        real_read = os.read
        state = {"verify_calls": 0, "final": False, "mutated": False}
        competitor = b'{"competitor":true}\n'

        def read_then_rewrite(descriptor: int, count: int) -> bytes:
            chunk = real_read(descriptor, count)
            if (
                state["final"]
                and not state["mutated"]
                and chunk == b""
                and self.invocation_path.exists()
            ):
                opened = os.fstat(descriptor)
                fixed = os.stat(self.invocation_path, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) == (fixed.st_dev, fixed.st_ino):
                    writer = os.open(self.invocation_path, os.O_WRONLY | os.O_TRUNC)
                    try:
                        os.write(writer, competitor)
                        os.fsync(writer)
                    finally:
                        os.close(writer)
                    state["mutated"] = True
            return chunk

        def verify_with_final_race(receipt: Any) -> None:
            state["verify_calls"] += 1
            state["final"] = state["verify_calls"] == 2
            try:
                real_verify(receipt)
            finally:
                state["final"] = False

        with mock.patch.object(
            contracts,
            "verify_file_receipt",
            side_effect=verify_with_final_race,
        ), mock.patch.object(
            transactions.os,
            "read",
            side_effect=read_then_rewrite,
        ):
            with self.assertRaises(ToolError) as raised:
                contracts.record_invocation(self.admission_path, self.invocations)
        self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
        self.assertTrue(state["mutated"])
        self.assertFalse(self.invocation_path.exists())


class NoReplaceTransactionHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _assert_candidate_replacement_is_preserved(
        self,
        action: Callable[[], Any],
        *,
        expect_error: bool,
    ) -> None:
        competitor = self.root / "candidate-competitor"
        saved_owned = self.root / "saved-owned-candidate"
        competitor.write_text("competitor\n", encoding="utf-8")
        real_unlink = Path.unlink
        real_rename = os.rename
        replaced = False

        def replace_before_unlink(
            path: Path, *args: Any, **kwargs: Any
        ) -> None:
            nonlocal replaced
            if (
                path.parent == self.root
                and path.name.startswith(".record.json.")
                and not path.name.endswith(".rollback")
                and not replaced
            ):
                replaced = True
                real_rename(path, saved_owned)
                real_rename(competitor, path)
            real_unlink(path, *args, **kwargs)

        with mock.patch.object(
            Path,
            "unlink",
            autospec=True,
            side_effect=replace_before_unlink,
        ):
            if expect_error:
                with self.assertRaises(ToolError):
                    action()
            else:
                action()
        contents = [
            path.read_text(encoding="utf-8")
            for path in self.root.iterdir()
            if path.is_file()
        ]
        self.assertIn("competitor\n", contents)

    def test_success_never_unlinks_a_replaced_candidate_path(self) -> None:
        destination = self.root / "record.json"
        self._assert_candidate_replacement_is_preserved(
            lambda: atomic_write.publish_json_no_overwrite(destination, {"value": 1}),
            expect_error=False,
        )
        self.assertEqual({"value": 1}, json.loads(destination.read_text()))

    def test_fileexists_never_unlinks_a_replaced_candidate_path(self) -> None:
        destination = self.root / "record.json"
        destination.write_text("existing\n", encoding="utf-8")
        self._assert_candidate_replacement_is_preserved(
            lambda: atomic_write.publish_json_no_overwrite(destination, {"value": 1}),
            expect_error=True,
        )
        self.assertEqual("existing\n", destination.read_text(encoding="utf-8"))

    def test_prepublish_failure_never_unlinks_a_replaced_candidate_path(self) -> None:
        destination = self.root / "record.json"
        publisher_name = (
            "_rename_no_replace"
            if hasattr(atomic_write, "_rename_no_replace")
            else "_publish_no_overwrite"
        )
        with mock.patch.object(
            atomic_write,
            publisher_name,
            side_effect=ToolError(
                "BUILD_OUTPUT_INCOMPLETE", str(destination), "injected failure"
            ),
        ):
            self._assert_candidate_replacement_is_preserved(
                lambda: atomic_write.publish_json_no_overwrite(
                    destination, {"value": 1}
                ),
                expect_error=True,
            )
        self.assertFalse(destination.exists())

    def test_fsync_failure_keeps_only_one_name_for_the_owned_inode(self) -> None:
        destination = self.root / "record.json"
        real_fsync = atomic_write._fsync_directory
        calls = 0

        def fail_once(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ToolError(
                    "BUILD_OUTPUT_INCOMPLETE", str(path), "injected fsync failure"
                )
            real_fsync(path)

        with mock.patch.object(atomic_write, "_fsync_directory", side_effect=fail_once):
            with self.assertRaises(ToolError) as raised:
                atomic_write.publish_json_no_overwrite(destination, {"value": 1})
        tombstones = list(self.root.glob(".record.json.*.rollback"))
        self.assertEqual(1, len(tombstones), raised.exception.detail)
        self.assertIn("retained_tombstone=", raised.exception.detail)
        self.assertIn("phase=", raised.exception.detail)
        self.assertIn("identity=", raised.exception.detail)

    def test_directory_lock_closes_descriptor_when_flock_fails(self) -> None:
        before = len(os.listdir("/dev/fd"))
        lock = contracts._DirectoryLock(self.root)
        with mock.patch.object(
            contracts.fcntl, "flock", side_effect=OSError("injected flock failure")
        ):
            for _ in range(25):
                with self.assertRaises(OSError):
                    lock.__enter__()
                self.assertIsNone(lock.descriptor)
        self.assertEqual(before, len(os.listdir("/dev/fd")))

    def _bounded_second_acquisition(self, first: transactions.DirectoryLock) -> bool:
        def acquire() -> bool:
            with transactions.DirectoryLock(self.root):
                return True

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(acquire)
        completed = False
        try:
            try:
                completed = future.result(timeout=0.4)
            except TimeoutError:
                completed = False
        finally:
            if first.descriptor is not None:
                os.close(first.descriptor)
                first.descriptor = None
            future.result(timeout=2)
            pool.shutdown(wait=True)
        return completed

    def test_directory_lock_post_flock_fstat_failure_releases_fd_and_lock(
        self,
    ) -> None:
        """Moving enter bookkeeping outside its cleanup guard must block here."""
        before = len(os.listdir("/dev/fd"))
        lock = transactions.DirectoryLock(self.root)
        real_fstat = os.fstat
        failed = False

        def fail_bookkeeping(descriptor: int):
            nonlocal failed
            if descriptor == lock.descriptor and not failed:
                failed = True
                raise OSError("injected post-flock fstat failure")
            return real_fstat(descriptor)

        with mock.patch.object(
            transactions.os, "fstat", side_effect=fail_bookkeeping
        ):
            with self.assertRaises(OSError):
                lock.__enter__()
        self.assertTrue(self._bounded_second_acquisition(lock))
        self.assertIsNone(lock.descriptor)
        self.assertEqual(before, len(os.listdir("/dev/fd")))

    def test_directory_lock_exit_bookkeeping_failure_still_releases_lock(
        self,
    ) -> None:
        """A pre-finally exit fstat must leak the acquired lock and descriptor."""
        before = len(os.listdir("/dev/fd"))
        lock = transactions.DirectoryLock(self.root)
        lock.__enter__()
        descriptor = lock.descriptor
        real_fstat = os.fstat
        failed = False

        def fail_bookkeeping(candidate: int):
            nonlocal failed
            if candidate == descriptor and not failed:
                failed = True
                raise OSError("injected exit bookkeeping failure")
            return real_fstat(candidate)

        with mock.patch.object(
            transactions.os, "fstat", side_effect=fail_bookkeeping
        ):
            try:
                lock.__exit__(None, None, None)
            except OSError:
                pass
        self.assertTrue(self._bounded_second_acquisition(lock))
        self.assertIsNone(lock.descriptor)
        self.assertEqual(before, len(os.listdir("/dev/fd")))

    def test_open_verified_file_post_open_fstat_failure_closes_every_fd(
        self,
    ) -> None:
        """Removing the post-open close guard must leak one fd per attempt."""
        path = self.root / "receipt.json"
        path.write_text("{}\n", encoding="utf-8")
        before = len(os.listdir("/dev/fd"))
        opened: list[int] = []
        real_open = os.open

        def record_open(*args: Any, **kwargs: Any) -> int:
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        try:
            with mock.patch.object(
                transactions.os, "open", side_effect=record_open
            ), mock.patch.object(
                transactions.os,
                "fstat",
                side_effect=OSError("injected receipt fstat failure"),
            ):
                for _ in range(12):
                    with self.assertRaises(OSError):
                        transactions._open_verified_file(path)
            after = len(os.listdir("/dev/fd"))
        finally:
            for descriptor in opened:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self.assertEqual(before, after)

    def test_no_replace_primitive_rejects_nul_before_ctypes(self) -> None:
        with self.assertRaises(ValueError):
            contracts._rename_directory_no_replace(
                Path("bad\0source"), self.root / "destination"
            )


class RecoveryManifestTests(unittest.TestCase):
    """Retained file and directory payloads are bounded and recoverable."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _failed_file(self, name: str = "record.json") -> tuple[Path, bytes]:
        destination = self.root / name
        payload = {"value": name}
        encoded = transactions.encode_json(payload)
        with mock.patch.object(
            atomic_write, "_rename_no_replace", side_effect=OSError("injected")
        ):
            with self.assertRaises(ToolError):
                atomic_write.publish_json_no_overwrite(destination, payload)
        return destination, encoded

    def _failed_directory(self, name: str = "admission") -> tuple[Path, dict[str, bytes]]:
        destination = self.root / name
        payloads = {
            "review-admission.json": b"{}\n",
            "reviewer-prompt.txt": b"prompt\n",
        }
        with self.assertRaises(transactions.TransactionFailure):
            transactions.publish_directory_no_replace(
                destination,
                payloads,
                fsync_directory=atomic_write._fsync_directory,
                rename=lambda _source, _destination: (_ for _ in ()).throw(
                    OSError("injected")
                ),
            )
        return destination, payloads

    def test_file_and_directory_tombstones_have_persistent_exact_manifests(
        self,
    ) -> None:
        """Keeping audit data only in exception text must fail enumeration."""
        file_destination, file_bytes = self._failed_file()
        directory_destination, directory_payloads = self._failed_directory()
        enumerator = getattr(transactions, "enumerate_recovery_manifests", None)
        self.assertTrue(callable(enumerator), "persistent recovery API is missing")
        manifests = enumerator(self.root)
        self.assertEqual(2, len(manifests))
        by_kind = {manifest.kind: manifest for manifest in manifests}
        file_manifest = by_kind["file"]
        self.assertEqual(1, file_manifest.schema_version)
        self.assertEqual(file_destination, file_manifest.fixed_destination)
        self.assertEqual("none_observed", file_manifest.competitor_state)
        self.assertEqual(len(file_bytes), file_manifest.payload_size)
        self.assertEqual(
            hashlib.sha256(file_bytes).hexdigest(),
            file_manifest.payload_sha256,
        )
        self.assertEqual(file_manifest.owned_identity, file_manifest.tombstone_identity)
        directory_manifest = by_kind["directory"]
        self.assertEqual(directory_destination, directory_manifest.fixed_destination)
        self.assertEqual(
            set(directory_payloads),
            {member.name for member in directory_manifest.members},
        )
        self.assertEqual(
            manifests,
            transactions.enumerate_recovery_manifests(self.root),
        )

    def test_recovery_capacity_limit_fails_before_creating_candidate(self) -> None:
        """Removing the 64 MiB recovery budget must publish this oversized JSON."""
        destination = self.root / "oversized.json"
        payload = {"payload": "x" * (64 * 1024 * 1024)}
        with self.assertRaises(ToolError) as raised:
            atomic_write.publish_json_no_overwrite(destination, payload)
        self.assertIn("recovery tombstone capacity", raised.exception.detail)
        self.assertFalse(destination.exists())
        self.assertEqual([], list(self.root.glob("*.rollback")))

    def test_unsupported_recovery_metadata_is_cached_fail_closed(self) -> None:
        """Bypassing capability preflight must create or publish full candidates."""
        unsupported = OSError(errno.ENOTSUP, "injected unsupported xattr")
        with mock.patch.object(
            transactions,
            "_set_recovery_xattr",
            side_effect=unsupported,
        ):
            for index in range(2):
                destination = self.root / f"unsupported-{index}.json"
                with self.assertRaises(ToolError) as raised:
                    atomic_write.publish_json_no_overwrite(
                        destination, {"payload": "x" * 1024 * 1024}
                    )
                self.assertIn("recovery metadata", raised.exception.detail)
                self.assertFalse(destination.exists())
        self.assertEqual([], list(self.root.glob("*.rollback")))


if __name__ == "__main__":
    unittest.main()
