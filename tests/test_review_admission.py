from __future__ import annotations

import contextlib
import copy
import errno
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lib import atomic_write, review_contracts as contracts
from lib.atomic_write import publish_json_no_overwrite
from lib.error_codes import ToolError
from lib.hashing import canonical_json_sha256, file_sha256
from lib.spec_identity import (
    content_spec_sha256,
    input_spec_sha256,
    review_state_sha256,
)
from tests.fixture_specs import make_minimal_spec
from tests.test_build_pptx_from_spec import compile_fixture
from tests.test_create_rendered_text_geometry import bbox_xml
from tests.test_render_preview import write_minimal_pdf


CLI_PATH = SCRIPTS / "review_admission.py"


def _load_script(filename: str, module_name: str):
    path = SCRIPTS / filename
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise AssertionError(f"cannot load production script: {path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


def _load_cli():
    return _load_script("review_admission.py", "task7_review_admission_cli")


def _identity(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": file_sha256(path)}


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)


class AdmissionFixture(unittest.TestCase):
    SPEC_FACTORY = staticmethod(make_minimal_spec)

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.output = self.root / "admission"
        self.invocations = self.root / "invocations"
        self._generation = 0
        self._rejection_attempt = 0
        self._response_attempt = 0
        self._round_two_attempt = 0
        self._make_fixture()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    @property
    def admission_path(self) -> Path:
        return self.output / "review-admission.json"

    @property
    def prompt_path(self) -> Path:
        return self.output / "reviewer-prompt.txt"

    @property
    def invocation_path(self) -> Path:
        return self.invocations / "page-001-round-1-invocation.json"

    @property
    def response_validation_path(self) -> Path:
        return self.root / "review-response-validation.json"

    def _write_executable(self, root: Path, name: str, source: str) -> Path:
        path = root / name
        path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _make_fixture(
        self,
        profile: str = "strict",
        *,
        page_id: str = "page-001",
        pptx_bytes: bytes | None = None,
        preview_bytes: bytes | None = None,
    ) -> None:
        self._generation += 1
        fixture = self.root / f"fixture-{self._generation}"
        fixture.mkdir()
        self.fixture = fixture
        self.spec = self.SPEC_FACTORY(fixture / "source")
        self.spec["page_id"] = page_id
        self.spec["verification_profile"] = profile
        self.pptx, self.build_report = compile_fixture(fixture / "compile", self.spec)
        if pptx_bytes is None:
            with zipfile.ZipFile(self.pptx, "a") as package:
                package.comment = f"candidate-{self._generation}".encode("ascii")
        else:
            self.pptx.write_bytes(pptx_bytes)
        self.build_report["pptx_sha256"] = file_sha256(self.pptx)
        self.spec_path = fixture / "compile" / "page-reconstruction.json"
        self.build_path = fixture / "compile" / "build-report.json"
        self.source = Path(self.spec["clean_visual_reference"]["path"])
        # All downstream producers bind raw-file hashes.  Put the two compiler
        # payloads in the same canonical test serialization that issue() uses
        # before producing any dependent report.
        _write_json(self.spec_path, self.spec)
        _write_json(self.build_path, self.build_report)

        validator = _load_script(
            "validate_pptx.py", f"task7_validate_pptx_{self._generation}"
        )
        self.structure_report = validator.validate_pptx(
            self.pptx,
            expected_slides=1,
            reconstruction_spec=self.spec,
            build_report=self.build_report,
        )
        self.assertTrue(self.structure_report["valid"], self.structure_report)
        # validate_pptx may normalize report members in place; persist the
        # post-validation object before later reports bind its raw bytes.
        _write_json(self.build_path, self.build_report)
        self.structure_path = fixture / "structure-report.json"
        _write_json(self.structure_path, self.structure_report)

        tools = fixture / "tools"
        tools.mkdir()
        pdf_fixture = fixture / "fixture.pdf"
        write_minimal_pdf(pdf_fixture)
        preview_fixture = fixture / "fixture.png"
        if preview_bytes is None:
            preview_image = Image.new("RGB", (1920, 1080), "white")
            offset = self._generation
            ImageDraw.Draw(preview_image).rectangle(
                (120 + offset, 100, 760 + offset, 360), fill="black"
            )
            preview_image.save(preview_fixture)
        else:
            preview_fixture.write_bytes(preview_bytes)
        self.soffice = self._write_executable(
            tools,
            "soffice",
            "import shutil, sys\n"
            "from pathlib import Path\n"
            "if '--version' in sys.argv or '-v' in sys.argv:\n"
            "    print('LibreOffice 26.2.3.2')\n"
            "    raise SystemExit(0)\n"
            f"fixture = Path({str(pdf_fixture)!r})\n"
            "outdir = Path(sys.argv[sys.argv.index('--outdir') + 1])\n"
            "source = Path(sys.argv[-1])\n"
            "outdir.mkdir(parents=True, exist_ok=True)\n"
            "shutil.copy2(fixture, outdir / (source.stem + '.pdf'))\n",
        )
        self.pdftoppm = self._write_executable(
            tools,
            "pdftoppm",
            "import shutil, sys\n"
            "from pathlib import Path\n"
            "if '--version' in sys.argv or '-v' in sys.argv:\n"
            "    print('pdftoppm version 26.07.0')\n"
            "    raise SystemExit(0)\n"
            f"fixture = Path({str(preview_fixture)!r})\n"
            "prefix = Path(sys.argv[-1])\n"
            "shutil.copy2(fixture, prefix.with_suffix('.png'))\n",
        )
        self.pdffonts = self._write_executable(
            tools,
            "pdffonts",
            "import sys\n"
            "if '--version' in sys.argv or '-v' in sys.argv:\n"
            "    print('pdffonts version 26.07.0')\n"
            "    raise SystemExit(0)\n"
            "print('name type encoding emb sub uni object ID')\n"
            "print('----------------------------------------')\n"
            "print('AAAAAA+NotoSansCJKsc-Regular Type1 Builtin yes yes yes 1 0')\n",
        )
        xml_path = fixture / "bbox.xml"
        xml_path.write_text(
            bbox_xml([("测试标题", 20, 20, 70, 32)]), encoding="utf-8"
        )
        self.pdftotext = self._write_executable(
            tools,
            "pdftotext",
            "import sys\n"
            "from pathlib import Path\n"
            "if '--version' in sys.argv or '-v' in sys.argv:\n"
            "    print('pdftotext version 26.07.0')\n"
            "    raise SystemExit(0)\n"
            f"sys.stdout.write(Path({str(xml_path)!r}).read_text(encoding='utf-8'))\n",
        )
        self.fontconfig = fixture / "fontconfig.xml"
        self.fontconfig.write_text("<fontconfig/>\n", encoding="utf-8")
        self.runtime_path = fixture / "runtime-preflight.json"
        preflight = _load_script(
            "preflight_runtime.py", f"task7_preflight_runtime_{self._generation}"
        )
        preflight_args = preflight._parse_args(
            [
                "--soffice",
                str(self.soffice),
                "--pdftoppm",
                str(self.pdftoppm),
                "--pdffonts",
                str(self.pdffonts),
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
        self.runtime = preflight.inspect_runtime(preflight_args)
        self.assertTrue(self.runtime["valid"], self.runtime)
        _write_json(self.runtime_path, self.runtime)

        renderer = _load_script(
            "render_preview.py", f"task7_render_preview_{self._generation}"
        )
        self.render_dir = fixture / "render"
        self.render_report = renderer.render_preview(
            self.pptx, self.render_dir, self.runtime_path
        )
        self.render_path = self.render_dir / "render-report.json"
        _write_json(self.render_path, self.render_report)
        self.pdf = Path(self.render_report["pdf"]["path"])
        self.preview = Path(self.render_report["preview"]["path"])
        self.font_report_path = Path(self.render_report["font_report"]["path"])
        self.raw_font_report_path = Path(
            self.render_report["font_report"]["raw_path"]
        )

        geometry = _load_script(
            "create_rendered_text_geometry.py",
            f"task7_text_geometry_{self._generation}",
        )
        self.text_path = fixture / "rendered-text-geometry.json"
        self.text_report = geometry.create_rendered_text_geometry(
            self.spec_path,
            self.pptx,
            self.build_path,
            self.render_path,
            self.runtime_path,
            self.text_path,
        )
        self.assertTrue(self.text_report["valid"], self.text_report)

        background = _load_script(
            "validate_background_contract.py",
            f"task7_background_{self._generation}",
        )
        self.background_report = background.validate_background_postbuild(
            self.spec_path, self.pptx, self.build_path, self.structure_path
        )
        self.assertTrue(self.background_report["valid"], self.background_report)
        self.background_path = fixture / "background-contract.json"
        _write_json(self.background_path, self.background_report)

        visual = _load_script(
            "create_visual_diff.py", f"task7_visual_diff_{self._generation}"
        )
        self.visual_dir = fixture / "visual"
        self.visual_report = visual.build_visual_diff_from_render_report(
            self.source,
            self.render_path,
            self.visual_dir,
            regions=self.spec["regions"],
            profile=profile,
        )
        self.visual_path = self.visual_dir / "visual-diff.json"
        self.overlay = Path(self.visual_report["evidence"]["overlay"]["path"])
        self.diff = Path(self.visual_report["evidence"]["diff"]["path"])
        self.region_evidence = (
            Path(self.visual_report["regions"][0]["evidence"])
            if self.visual_report["regions"]
            else self.preview
        )

        self.content_hash = content_spec_sha256(self.spec)
        self.full_spec_hash = input_spec_sha256(self.spec)
        self.review_state_hash = review_state_sha256(self.spec)
        self.pptx_hash = file_sha256(self.pptx)
        self.source_hash = file_sha256(self.source)
        self.preview_hash = file_sha256(self.preview)
        self._capture_baseline()

    def _payload_map(self) -> dict[str, tuple[str, Path]]:
        return {
            "spec": ("spec", self.spec_path),
            "build_report": ("build_report", self.build_path),
            "structure_report": ("structure_report", self.structure_path),
            "render_report": ("render_report", self.render_path),
            "runtime": ("runtime", self.runtime_path),
            "text_report": ("text_report", self.text_path),
            "background_report": ("background_report", self.background_path),
            "visual_report": ("visual_report", self.visual_path),
        }

    def _capture_baseline(self) -> None:
        self._baseline_payloads = {
            name: copy.deepcopy(getattr(self, attribute))
            for name, (attribute, _path) in self._payload_map().items()
        }
        self._baseline_bytes = {
            path: path.read_bytes()
            for path in self.fixture.rglob("*")
            if path.is_file()
        }

    def _restore_fixture(self) -> None:
        for path, payload in self._baseline_bytes.items():
            path.write_bytes(payload)
        for name, (attribute, _path) in self._payload_map().items():
            setattr(self, attribute, copy.deepcopy(self._baseline_payloads[name]))

    def _write_payloads(self) -> None:
        for _name, (attribute, path) in self._payload_map().items():
            _write_json(path, getattr(self, attribute))

    def _rebind_chain(self) -> None:
        """Keep cross-file identities current after a deliberate forged mutation."""
        _write_json(self.spec_path, self.spec)
        _write_json(self.build_path, self.build_report)
        _write_json(self.structure_path, self.structure_report)
        _write_json(self.render_path, self.render_report)
        _write_json(self.runtime_path, self.runtime)
        spec_file_hash = file_sha256(self.spec_path)
        build_file_hash = file_sha256(self.build_path)
        render_file_hash = file_sha256(self.render_path)
        runtime_file_hash = file_sha256(self.runtime_path)
        self.text_report.update(
            {
                "spec_file_sha256": spec_file_hash,
                "pptx_sha256": file_sha256(self.pptx),
                "build_report_sha256": build_file_hash,
                "render_report_sha256": render_file_hash,
                "runtime_sha256": runtime_file_hash,
                "pdf_sha256": file_sha256(self.pdf),
            }
        )
        self.text_report["inputs"].update(
            {
                "spec_file_sha256": spec_file_hash,
                "pptx_sha256": file_sha256(self.pptx),
                "build_report_sha256": build_file_hash,
                "render_report_sha256": render_file_hash,
                "runtime_sha256": runtime_file_hash,
                "pdf_sha256": file_sha256(self.pdf),
            }
        )
        _write_json(self.text_path, self.text_report)
        self.background_report.update(
            {
                "pptx_sha256": file_sha256(self.pptx),
                "build_report_sha256": canonical_json_sha256(self.build_report),
                "build_report_file_sha256": build_file_hash,
                "structure_report_sha256": canonical_json_sha256(
                    self.structure_report
                ),
                "structure_report_file_sha256": file_sha256(self.structure_path),
            }
        )
        _write_json(self.background_path, self.background_report)
        self.visual_report["reference"] = _identity(self.source)
        self.visual_report["preview"] = _identity(self.preview)
        self.visual_report["pptx_sha256"] = file_sha256(self.pptx)
        self.visual_report["pdf_sha256"] = file_sha256(self.pdf)
        self.visual_report["render_report"] = _identity(self.render_path)
        self.visual_report["renderer"] = copy.deepcopy(
            self.render_report["renderer"]
        )
        _write_json(self.visual_path, self.visual_report)

    def inputs(self, *, round_number: int = 1) -> contracts.AdmissionInputs:
        return contracts.AdmissionInputs(
            spec=self.spec_path,
            pptx=self.pptx,
            build_report=self.build_path,
            structure_report=self.structure_path,
            render_report=self.render_path,
            text_geometry=self.text_path,
            background_report=self.background_path,
            visual_diff=self.visual_path,
            review_round=round_number,
        )

    def issue(self, *, round_number: int = 1, output: Path | None = None):
        self._write_payloads()
        return contracts.issue_admission(
            self.inputs(round_number=round_number), output or self.output
        )

    def valid_response(self, admission: dict[str, object]) -> dict[str, object]:
        return {
            "admission_id": admission["admission_id"],
            "page_id": admission["page_id"],
            "review_round": admission["review_round"],
            "source_sha256": admission["source_sha256"],
            "preview_sha256": admission["preview_sha256"],
            "decision": "passed",
            "coverage": {
                "canvas_and_regions": "checked",
                "objects_and_geometry": "checked",
                "text_and_typography": "checked",
                "tables_and_matrices": "not_applicable",
                "graphics_connectors_charts": "not_applicable",
                "pictures_crop_layers": "not_applicable",
                "high_risk_regions": "not_applicable",
            },
            "findings": [],
            "p2_disclosures": [],
        }

    def invoked_round_one(self):
        admission = self.issue()
        invocation = contracts.record_invocation(self.admission_path, self.invocations)
        return admission, invocation, self.admission_path, self.invocation_path

    def validate_response(
        self,
        admission_path: Path,
        invocation_path: Path,
        response: dict[str, object],
        *,
        output: Path | None = None,
    ) -> dict[str, Any]:
        self._response_attempt += 1
        response_path = self.root / f"review-response-{self._response_attempt}.json"
        _write_json(response_path, response)
        before = response_path.read_bytes()
        report = contracts.validate_response(
            admission_path,
            invocation_path,
            response_path,
            output or self.root / f"review-response-validation-{self._response_attempt}.json",
        )
        self.assertEqual(before, response_path.read_bytes())
        return report

    def invoke_again(self, _admission: dict[str, object]) -> dict[str, Any]:
        return contracts.record_invocation(self.admission_path, self.invocations)

    def _validated_prior_response(
        self,
        *,
        decision: str,
        severities: list[str],
        category: str,
    ) -> dict[str, Any]:
        admission, invocation, admission_path, invocation_path = self.invoked_round_one()
        response = self.valid_response(admission)
        response["decision"] = decision
        response["findings"] = [
            {
                "severity": severity,
                "category": category,
                "location": f"region-{index}",
                "source_fact": f"source fact {index}",
                "observed_difference": f"difference {index}",
                "evidence": [str(self.overlay.resolve())],
            }
            for index, severity in enumerate(severities)
        ]
        response_path = self.root / f"prior-review-response-{self._generation}.json"
        validation_path = self.root / f"prior-review-response-validation-{self._generation}.json"
        _write_json(response_path, response)
        report = contracts.validate_response(
            admission_path,
            invocation_path,
            response_path,
            validation_path,
        )
        self.assertTrue(report["valid"], report)
        prior = {
            "admission": admission,
            "invocation": invocation,
            "response": response,
            "validation": report,
            "admission_path": admission_path,
            "invocation_path": invocation_path,
            "response_path": response_path,
            "validation_path": validation_path,
        }
        self._make_fixture()
        self.output = self.root / "admission-round-2"
        self.invocations = self.root / "invocations-round-2"
        return prior

    def validated_changes_required_response(
        self,
        *,
        severities: list[str] | None = None,
        category: str = "objects_and_geometry",
    ) -> dict[str, Any]:
        return self._validated_prior_response(
            decision="changes_required",
            severities=severities or ["P1"],
            category=category,
        )

    def _current_verification(self) -> dict[str, dict[str, str]]:
        evidence = _identity(self.overlay)
        return {
            name: {"status": "passed", **evidence}
            for name in ("dense_text", "numbers_and_units", "wrap_sensitive")
        }

    def add_high_risk_mapping(
        self,
        prior: dict[str, Any],
        *,
        finding_index: int = 0,
        source: str | None = None,
        severity: str | None = None,
        category: str | None = None,
        result: str = "passed",
        evidence: list[str] | None = None,
        verification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        finding = prior["response"]["findings"][finding_index]
        item = {
            "risk_id": f"reviewer-finding-{finding_index}",
            "source": source
            or f"reviewer:{prior['admission']['admission_id']}:finding:{finding_index}",
            "scope": "whole_page",
            "category": category or finding["category"],
            "expected": finding["source_fact"],
            "strategy": "repair_and_reverify",
            "result": result,
            "evidence": [str(self.overlay.resolve())] if evidence is None else evidence,
            "confidence": "high",
            "severity": severity or finding["severity"],
            "verification": (
                self._current_verification()
                if verification is None
                and (category or finding["category"]) == "text_and_typography"
                else ({} if verification is None else verification)
            ),
        }
        activated = self.spec.setdefault("activated_modules", [])
        if "high_risk" not in activated:
            activated.append("high_risk")
        self.spec.setdefault("modules", {}).setdefault("high_risk", {}).setdefault(
            "items", []
        ).append(item)
        current_input_hash = input_spec_sha256(self.spec)
        self.build_report["schema_sha256"] = canonical_json_sha256(self.spec)
        self.build_report["input_spec_sha256"] = current_input_hash
        self.text_report["input_spec_sha256"] = current_input_hash
        self.text_report["inputs"]["input_spec_sha256"] = current_input_hash
        self.background_report["input_spec_sha256"] = current_input_hash
        self._rebind_chain()
        return item

    def issue_round_two(self, prior: dict[str, Any]) -> dict[str, Any]:
        self._round_two_attempt += 1
        output = self.root / f"admission-round-2-{self._round_two_attempt}"
        self.output = output
        self._write_payloads()
        return contracts.issue_admission(
            contracts.AdmissionInputs(
                spec=self.spec_path,
                pptx=self.pptx,
                build_report=self.build_path,
                structure_report=self.structure_path,
                render_report=self.render_path,
                text_geometry=self.text_path,
                background_report=self.background_path,
                visual_diff=self.visual_path,
                review_round=2,
                prior_admission=prior["admission_path"],
                prior_invocation=prior["invocation_path"],
                prior_response_validation=prior["validation_path"],
            ),
            output,
        )

    def assert_issue_rejected(self, code: str = "REVIEW_ADMISSION_NOT_ISSUED") -> None:
        self._rejection_attempt += 1
        self.output = self.root / f"rejected-admission-{self._rejection_attempt}"
        with self.assertRaises(ToolError) as raised:
            self.issue()
        self.assertEqual(code, raised.exception.code)
        self.assertFalse(self.output.exists())


class ReviewAdmissionIssueTests(AdmissionFixture):
    def test_round_one_binds_every_current_gate_without_visual_conclusion(self) -> None:
        admission = self.issue()
        self.assertEqual("page-001", admission["page_id"])
        self.assertEqual(1, admission["review_round"])
        self.assertEqual("strict", admission["verification_profile"])
        self.assertEqual(self.content_hash, admission["spec_sha256"])
        self.assertEqual(self.full_spec_hash, admission["input_spec_sha256"])
        self.assertEqual(self.review_state_hash, admission["review_state_sha256"])
        self.assertEqual(self.pptx_hash, admission["pptx_sha256"])
        self.assertEqual(self.source_hash, admission["source_sha256"])
        self.assertEqual(self.preview_hash, admission["preview_sha256"])
        self.assertNotIn("decision", admission)
        self.assertNotIn("valid", admission)
        self.assertNotIn("status", admission)
        self.assertIn("runtime_preflight", admission["artifacts"])
        self.assertEqual(
            {"review-admission.json", "reviewer-prompt.txt"},
            {path.name for path in self.output.iterdir()},
        )

    def test_admission_id_is_canonical_and_tamper_sensitive(self) -> None:
        admission = self.issue()
        self.assertEqual(admission["admission_id"], contracts.recompute_admission_id(admission))
        tampered = copy.deepcopy(admission)
        tampered["preview_sha256"] = "9" * 64
        self.assertNotEqual(admission["admission_id"], contracts.recompute_admission_id(tampered))

    def test_prompt_lists_bound_evidence_hashes_and_exact_response_contract(self) -> None:
        admission = self.issue()
        prompt = self.prompt_path.read_text(encoding="utf-8")
        for value in (
            admission["admission_id"],
            admission["page_id"],
            admission["source_sha256"],
            admission["preview_sha256"],
            str(self.source.resolve()),
            str(self.preview.resolve()),
            str(self.overlay.resolve()),
            str(self.diff.resolve()),
            str(self.region_evidence.resolve()),
            file_sha256(self.overlay),
            file_sha256(self.diff),
            file_sha256(self.region_evidence),
        ):
            self.assertIn(str(value), prompt)
        self.assertIn("side-by-side", prompt)
        exact_fields = [
            "admission_id",
            "page_id",
            "review_round",
            "source_sha256",
            "preview_sha256",
            "decision",
            "coverage",
            "findings",
            "p2_disclosures",
        ]
        self.assertIn(json.dumps(exact_fields, ensure_ascii=False), prompt)
        coverage_fields = [
            "canvas_and_regions",
            "objects_and_geometry",
            "text_and_typography",
            "tables_and_matrices",
            "graphics_connectors_charts",
            "pictures_crop_layers",
            "high_risk_regions",
        ]
        coverage_contract = prompt.split(
            "coverage 必须精确包含全部七个键，每个键都必须出现，且无缺失、无多余键：",
            1,
        )[1].split("\n", 1)[0]
        for field in coverage_fields:
            self.assertEqual(1, coverage_contract.count(field))
        self.assertIn("必须精确包含全部七个键", prompt)
        self.assertIn("每个键都必须出现", prompt)
        self.assertIn("无缺失、无多余键", prompt)
        self.assertEqual(prompt, contracts.reviewer_prompt(admission))

    def test_reviewer_prompt_declares_machine_types_for_every_finding(self) -> None:
        self.issue()
        prompt = self.prompt_path.read_text(encoding="utf-8")
        self.assertIn(
            "findings[*].evidence 必须是非空 JSON 字符串数组 list[str]",
            prompt,
        )
        self.assertIn(
            "p2_disclosures 每项必须是 severity=P2 的完整 finding",
            prompt,
        )
        self.assertIn(
            "中性示例中所有尖括号占位符（包括 evidence 路径）仅表示类型，"
            "禁止原样复制到提交 JSON；必须改为本 prompt 已列出的实际值。",
            prompt,
        )
        self.assertNotIn("/absolute/path/from-this-prompt.png", prompt)

        marker = (
            "中性 JSON 结构示例（仅展示类型，不代表真实页面结论、severity "
            "结论或虚构 hash）：\n"
        )
        neutral = json.loads(prompt.split(marker, 1)[1])
        expected_fields = {
            "admission_id",
            "page_id",
            "review_round",
            "source_sha256",
            "preview_sha256",
            "decision",
            "coverage",
            "findings",
            "p2_disclosures",
        }
        expected_finding_fields = {
            "severity",
            "category",
            "location",
            "source_fact",
            "observed_difference",
            "evidence",
        }
        expected_coverage_fields = {
            "canvas_and_regions",
            "objects_and_geometry",
            "text_and_typography",
            "tables_and_matrices",
            "graphics_connectors_charts",
            "pictures_crop_layers",
            "high_risk_regions",
        }
        self.assertEqual(expected_fields, set(neutral))
        self.assertEqual(expected_coverage_fields, set(neutral["coverage"]))
        self.assertEqual(expected_finding_fields, set(neutral["findings"][0]))
        self.assertEqual(expected_finding_fields, set(neutral["p2_disclosures"][0]))
        self.assertEqual("<passed|changes_required|not_reviewable>", neutral["decision"])
        self.assertEqual("<P0|P1|P2>", neutral["findings"][0]["severity"])
        self.assertEqual("<P2>", neutral["p2_disclosures"][0]["severity"])
        self.assertEqual("<source_sha256>", neutral["source_sha256"])
        self.assertEqual("<preview_sha256>", neutral["preview_sha256"])
        self.assertEqual(
            ["<current_visual_evidence_absolute_path>"],
            neutral["findings"][0]["evidence"],
        )
        self.assertEqual(
            ["<current_visual_evidence_absolute_path>"],
            neutral["p2_disclosures"][0]["evidence"],
        )

    def test_failed_gate_reports_are_rejected_before_writing(self) -> None:
        for name, attribute in (
            ("build", "build_report"),
            ("structure", "structure_report"),
            ("text", "text_report"),
            ("background", "background_report"),
        ):
            with self.subTest(name=name):
                getattr(self, attribute)["valid"] = False
                self.assert_issue_rejected()
                self._restore_fixture()

    def test_production_build_and_structure_contracts_are_recomputed(self) -> None:
        mutations: tuple[tuple[str, Callable[[], None]], ...] = (
            ("stale-compiler", lambda: self.build_report.update(compiler_sha256="0" * 64)),
            (
                "stale-capability",
                lambda: self.build_report.update(capability_manifest_sha256="0" * 64),
            ),
            ("invalid-elements", lambda: self.build_report.update(elements={})),
            ("forged-structure", lambda: self.structure_report.update(slide_count=99)),
        )
        for name, mutation in mutations:
            with self.subTest(name=name):
                mutation()
                self._rebind_chain()
                self.assert_issue_rejected()
                self._restore_fixture()

    def test_non_pptx_is_rejected_even_when_all_report_hashes_are_rebound(self) -> None:
        self.pptx.write_bytes(b"not-a-pptx")
        digest = file_sha256(self.pptx)
        self.build_report["pptx_sha256"] = digest
        self.structure_report["pptx_sha256"] = digest
        self.render_report["pptx"]["sha256"] = digest
        self.text_report["pptx_sha256"] = digest
        self.text_report["inputs"]["pptx_sha256"] = digest
        self.background_report["pptx_sha256"] = digest
        self.visual_report["pptx_sha256"] = digest
        self._rebind_chain()
        self.assert_issue_rejected()

    def test_render_rejects_forged_pdf_blank_preview_and_stale_runtime(self) -> None:
        cases: tuple[tuple[str, Callable[[], None]], ...] = (
            ("forged-pdf", lambda: self.pdf.write_bytes(b"not-a-pdf")),
            ("blank-preview", lambda: Image.new("RGB", (1920, 1080), "white").save(self.preview)),
            (
                "stale-tool",
                lambda: self.pdftoppm.write_text("#!/usr/bin/env python3\nraise SystemExit(7)\n", encoding="utf-8"),
            ),
            (
                "stale-preflight",
                lambda: self.runtime.update(preview_size=[1600, 900]),
            ),
            (
                "stale-font-raw",
                lambda: self.raw_font_report_path.write_text("changed\n", encoding="utf-8"),
            ),
        )
        for name, mutation in cases:
            with self.subTest(name=name):
                mutation()
                if name == "forged-pdf":
                    self.render_report["pdf"]["sha256"] = file_sha256(self.pdf)
                elif name == "blank-preview":
                    self.render_report["preview"]["sha256"] = file_sha256(self.preview)
                elif name == "stale-preflight":
                    _write_json(self.runtime_path, self.runtime)
                self._rebind_chain()
                self.assert_issue_rejected()
                self._restore_fixture()

    def test_runtime_versions_are_reprobed_instead_of_trusting_synced_reports(self) -> None:
        for tool in self.runtime["executables"].values():
            tool["version"] = "forged 99.0"
        self.render_report["renderer"]["version"] = "forged 99.0"
        self.render_report["rasterizer"]["version"] = "forged 99.0"
        self.render_report["text_extractor"]["version"] = "forged 99.0"
        self.text_report["pdftotext"]["version"] = "forged 99.0"
        self._rebind_chain()
        self.assert_issue_rejected()

    def test_issue_rechecks_context_dependent_runtime_at_original_paths(self) -> None:
        version_file = self.soffice.with_name("soffice-version.txt")
        version_file.write_text("LibreOffice 26.2.3.2\n", encoding="utf-8")
        self.soffice.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            "if '--version' in sys.argv or '-v' in sys.argv:\n"
            "    version = Path(__file__).with_name('soffice-version.txt')\n"
            "    print(version.read_text(encoding='utf-8').strip())\n"
            "    raise SystemExit(0)\n",
            encoding="utf-8",
        )
        self.soffice.chmod(0o755)
        preflight = __import__("preflight_runtime")
        arguments = preflight._parse_args(
            [
                "--soffice",
                str(self.soffice),
                "--pdftoppm",
                str(self.pdftoppm),
                "--pdffonts",
                str(self.pdffonts),
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
        self.runtime = preflight.inspect_runtime(arguments)
        self.assertTrue(self.runtime["valid"], self.runtime)
        _write_json(self.runtime_path, self.runtime)
        arguments.expected_runtime = self.runtime_path
        current = preflight.inspect_runtime(arguments)
        self.assertTrue(current["valid"], current)
        self.assertEqual(self.runtime, current)
        soffice = self.runtime["executables"]["soffice"]
        self.render_report["renderer"].update(
            {
                "path": soffice["path"],
                "version": soffice["version"],
                "executable_sha256": soffice["sha256"],
            }
        )
        self._rebind_chain()

        admission = self.issue()

        self.assertEqual(
            file_sha256(self.runtime_path),
            admission["artifacts"]["runtime_preflight"]["sha256"],
        )
        self.assertTrue(self.prompt_path.is_file())

    def test_fonts_are_recomputed_from_the_current_pdf(self) -> None:
        forged_raw = (
            "name type encoding emb sub uni object ID\n"
            "----------------------------------------\n"
            "AAAAAA+ForgedFont Type1 Builtin yes yes yes 1 0\n"
        )
        self.raw_font_report_path.write_text(forged_raw, encoding="utf-8")
        _write_json(self.font_report_path, {"resolved_fonts": ["ForgedFont"]})
        self.render_report["font_report"].update(
            {
                "sha256": file_sha256(self.font_report_path),
                "raw_sha256": file_sha256(self.raw_font_report_path),
                "resolved_fonts": ["ForgedFont"],
            }
        )
        self._rebind_chain()
        self.assert_issue_rejected()

    def test_visual_diff_is_recomputed_from_source_preview_and_regions(self) -> None:
        cases: tuple[tuple[str, Callable[[], None]], ...] = (
            (
                "overlay-content",
                lambda: Image.new("RGB", (1600, 900), "red").save(self.overlay),
            ),
            (
                "diff-content",
                lambda: Image.new("RGB", (1600, 900), "blue").save(self.diff),
            ),
            (
                "region-content",
                lambda: Image.new("RGB", (640, 360), "green").save(
                    self.region_evidence
                ),
            ),
            (
                "metrics",
                lambda: self.visual_report["full_page"].update(similarity=0.123456),
            ),
            (
                "tripwire-incomplete",
                lambda: self.visual_report.update(
                    tripwire={"available": False, "triggered": None}
                ),
            ),
            (
                "presence-incomplete",
                lambda: self.visual_report.update(
                    region_presence={"status": "passed", "missing": []}
                ),
            ),
        )
        for name, mutation in cases:
            with self.subTest(name=name):
                mutation()
                if name == "overlay-content":
                    self.visual_report["evidence"]["overlay"]["sha256"] = file_sha256(self.overlay)
                elif name == "diff-content":
                    self.visual_report["evidence"]["diff"]["sha256"] = file_sha256(self.diff)
                elif name == "region-content":
                    self.visual_report["regions"][0]["evidence_sha256"] = file_sha256(self.region_evidence)
                self.assert_issue_rejected()
                self._restore_fixture()

    def test_stale_content_source_and_preview_are_rejected(self) -> None:
        mutations = (
            lambda: self.spec["elements"][0]["content"].update(text="changed"),
            lambda: Image.new("RGB", (1600, 900), "red").save(self.source),
            lambda: Image.new("RGB", (1920, 1080), "blue").save(self.preview),
        )
        for mutation in mutations:
            mutation()
            self.assert_issue_rejected()
            self._restore_fixture()

    def test_malformed_json_inputs_fail_closed_without_output(self) -> None:
        paths = [path for _name, (_attribute, path) in self._payload_map().items()]
        for path in paths:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_text("{not-json", encoding="utf-8")
                with self.assertRaises(ToolError):
                    contracts.issue_admission(self.inputs(), self.output)
                self.assertFalse(self.output.exists())
                path.write_bytes(original)

    def test_round_two_remains_fail_closed_before_reading_inputs(self) -> None:
        self.spec_path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ToolError) as raised:
            contracts.issue_admission(self.inputs(round_number=2), self.output)
        self.assertEqual("REVIEW_ROUND_NOT_ADMITTED", raised.exception.code)
        self.assertFalse(self.output.exists())

    def test_reviewed_profile_is_admitted_but_rapid_is_not(self) -> None:
        self._make_fixture("reviewed")
        admission = self.issue()
        self.assertEqual("reviewed", admission["verification_profile"])
        reviewed_output = self.output
        self.output = self.root / "rapid-admission"
        self._make_fixture("rapid")
        with self.assertRaises(ToolError) as raised:
            self.issue()
        self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)
        self.assertFalse(self.output.exists())
        self.assertTrue(reviewed_output.is_dir())

    def test_issue_directory_is_transactional_no_overwrite_and_concurrent(self) -> None:
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises(ToolError):
            self.issue()
        self.assertEqual("keep", marker.read_text(encoding="utf-8"))

        self.output = self.root / "concurrent-admission"
        self._write_payloads()

        def issue_once():
            try:
                return contracts.issue_admission(self.inputs(), self.output)
            except ToolError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: issue_once(), range(6)))
        self.assertEqual(1, sum(isinstance(item, dict) for item in results))
        self.assertEqual(
            5,
            results.count("REVIEW_ADMISSION_ALREADY_EXISTS"),
        )
        self.assertEqual(
            {"review-admission.json", "reviewer-prompt.txt"},
            {path.name for path in self.output.iterdir()},
        )

    def test_publish_boundary_preserves_empty_competing_directory(self) -> None:
        real_rename = contracts.os.rename
        real_no_replace = getattr(
            contracts, "_rename_directory_no_replace", None
        )
        competitor_identity: tuple[int, int] | None = None

        def inject_competitor(
            source: str | Path, destination: str | Path
        ) -> None:
            nonlocal competitor_identity
            target = Path(destination)
            if _same_path(target, self.output) and not target.exists():
                target.mkdir()
                stat_result = target.stat()
                competitor_identity = (
                    stat_result.st_dev,
                    stat_result.st_ino,
                )
            if real_no_replace is not None:
                real_no_replace(source, destination)
            else:
                real_rename(source, destination)

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    contracts.os,
                    "rename",
                    side_effect=inject_competitor,
                )
            )
            if real_no_replace is not None:
                stack.enter_context(
                    mock.patch.object(
                        contracts,
                        "_rename_directory_no_replace",
                        side_effect=inject_competitor,
                    )
                )
            with self.assertRaises(ToolError) as raised:
                self.issue()

        self.assertEqual(
            "REVIEW_ADMISSION_ALREADY_EXISTS", raised.exception.code
        )
        self.assertIsNotNone(competitor_identity)
        final = self.output.stat()
        self.assertEqual(
            competitor_identity,
            (final.st_dev, final.st_ino),
        )
        self.assertEqual([], list(self.output.iterdir()))

    def test_parent_fsync_rollback_never_deletes_post_check_competitor(
        self,
    ) -> None:
        real_is_dir = Path.is_dir
        real_stat = contracts.os.stat
        real_rename = contracts.os.rename
        real_fsync_directory = contracts._fsync_directory
        resolved_root = self.root.resolve()
        saved_owned = self.root / "saved-owned-admission"
        competitor = self.root / "competitor-admission"
        competitor.mkdir()
        competitor_payloads = {
            "review-admission.json": b"competitor admission\n",
            "reviewer-prompt.txt": b"competitor prompt\n",
        }
        for name, payload in competitor_payloads.items():
            (competitor / name).write_bytes(payload)
        state = {"parent_fsync_failed": False, "replaced": False}

        def fail_first_parent_fsync(path: Path) -> None:
            if (
                _same_path(path, self.root)
                and self.output.exists()
                and not state["parent_fsync_failed"]
            ):
                state["parent_fsync_failed"] = True
                raise ToolError(
                    "BUILD_OUTPUT_INCOMPLETE",
                    str(path),
                    "injected parent fsync failure",
                )
            real_fsync_directory(path)

        def replace_legacy_fixed_output(
            path: Path, *args: Any, **kwargs: Any
        ) -> bool:
            result = real_is_dir(path, *args, **kwargs)
            if (
                _same_path(path, self.output)
                and result
                and not state["replaced"]
            ):
                state["replaced"] = True
                real_rename(path, saved_owned)
                real_rename(competitor, path)
            return result

        def replace_quarantine_after_stat(
            path: str | Path, *args: Any, **kwargs: Any
        ):
            result = real_stat(path, *args, **kwargs)
            try:
                candidate = Path(path)
            except TypeError:
                return result
            if (
                candidate.parent == resolved_root
                and candidate.name.startswith(".admission.")
                and candidate.name.endswith(".rollback")
                and not state["replaced"]
            ):
                state["replaced"] = True
                real_rename(candidate, saved_owned)
                real_rename(competitor, candidate)
            return result

        with mock.patch.object(
            contracts,
            "_fsync_directory",
            side_effect=fail_first_parent_fsync,
        ), mock.patch.object(
            Path,
            "is_dir",
            autospec=True,
            side_effect=replace_legacy_fixed_output,
        ), mock.patch.object(
            contracts.os,
            "stat",
            side_effect=replace_quarantine_after_stat,
        ):
            with self.assertRaises(ToolError) as raised:
                self.issue()

        self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)
        self.assertTrue(state["parent_fsync_failed"])
        self.assertTrue(state["replaced"])
        self.assertFalse(self.output.exists())
        self.assertEqual(
            {"review-admission.json", "reviewer-prompt.txt"},
            {path.name for path in saved_owned.iterdir()},
        )
        competitor_copies = [
            directory
            for directory in self.root.iterdir()
            if directory.is_dir()
            and all(
                (directory / name).is_file()
                and (directory / name).read_bytes() == payload
                for name, payload in competitor_payloads.items()
            )
        ]
        self.assertEqual(1, len(competitor_copies))
        self.assertTrue(competitor_copies[0].name.startswith(".admission."))
        self.assertTrue(competitor_copies[0].name.endswith(".rollback"))

    def test_no_replace_unsupported_and_io_failures_fail_closed(self) -> None:
        real_rename = contracts.os.rename
        real_no_replace = getattr(
            contracts, "_rename_directory_no_replace", None
        )
        failures = (
            ("unsupported", NotImplementedError("no no-replace primitive")),
            ("io-error", OSError(errno.EIO, "injected rename failure")),
        )
        for name, failure in failures:
            with self.subTest(name=name):
                output = self.root / f"admission-{name}"

                def fail_publish(
                    source: str | Path, destination: str | Path
                ) -> None:
                    if _same_path(destination, output):
                        raise failure
                    if real_no_replace is not None:
                        real_no_replace(source, destination)
                    else:
                        real_rename(source, destination)

                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        mock.patch.object(
                            contracts.os,
                            "rename",
                            side_effect=fail_publish,
                        )
                    )
                    if real_no_replace is not None:
                        stack.enter_context(
                            mock.patch.object(
                                contracts,
                                "_rename_directory_no_replace",
                                side_effect=fail_publish,
                            )
                        )
                    with self.assertRaises(ToolError) as raised:
                        self.issue(output=output)
                self.assertEqual(
                    "REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code
                )
                self.assertFalse(output.exists())

    def test_failed_staging_cleanup_never_deletes_replacement_directory(
        self,
    ) -> None:
        output = self.root / "admission-staging-failure"
        competitor = self.root / "staging-competitor"
        competitor.mkdir()
        marker = competitor / "competitor-marker.txt"
        marker.write_text("competitor\n", encoding="utf-8")
        saved_owned = self.root / "saved-owned-staging"
        real_rename = contracts.os.rename
        real_rmtree = shutil.rmtree
        real_no_replace = getattr(
            contracts, "_rename_directory_no_replace", None
        )

        def fail_fixed_publication(
            source: str | Path, destination: str | Path
        ) -> None:
            if _same_path(destination, output):
                raise OSError(errno.EIO, "injected publication failure")
            if real_no_replace is not None:
                real_no_replace(source, destination)
            else:
                real_rename(source, destination)

        def replace_before_legacy_rmtree(
            path: str | Path, *args: Any, **kwargs: Any
        ) -> None:
            candidate = Path(path)
            if (
                candidate.parent == self.root.resolve()
                and candidate.name.startswith(f".{output.name}.")
                and competitor.exists()
            ):
                real_rename(candidate, saved_owned)
                real_rename(competitor, candidate)
            real_rmtree(candidate, *args, **kwargs)

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    contracts.os,
                    "rename",
                    side_effect=fail_fixed_publication,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    shutil,
                    "rmtree",
                    side_effect=replace_before_legacy_rmtree,
                )
            )
            if real_no_replace is not None:
                stack.enter_context(
                    mock.patch.object(
                        contracts,
                        "_rename_directory_no_replace",
                        side_effect=fail_fixed_publication,
                    )
                )
            with self.assertRaises(ToolError) as raised:
                self.issue(output=output)

        self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)
        self.assertFalse(output.exists())
        self.assertTrue(
            any(
                path.is_file()
                and path.read_text(encoding="utf-8") == "competitor\n"
                for path in self.root.rglob("competitor-marker.txt")
            )
        )
        owned_tombstones = [
            directory
            for directory in self.root.iterdir()
            if directory.name.startswith(f".{output.name}.")
            and directory.name.endswith(".rollback")
            and directory.is_dir()
            and {
                path.name for path in directory.iterdir()
            }
            == {"review-admission.json", "reviewer-prompt.txt"}
        ]
        self.assertEqual(1, len(owned_tombstones))


class ReviewResponseTests(AdmissionFixture):
    def test_valid_response_is_read_only_and_binds_all_input_hashes(self) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        response = self.valid_response(admission)
        response_path = self.root / "review-response.json"
        output = self.root / "review-response-validation.json"
        _write_json(response_path, response)
        response_before = response_path.read_bytes()

        report = contracts.validate_response(
            admission_path, invocation_path, response_path, output
        )

        self.assertTrue(report["valid"], report)
        self.assertEqual([], report["errors"])
        self.assertEqual(file_sha256(response_path), report["response_sha256"])
        self.assertEqual(file_sha256(invocation_path), report["invocation_sha256"])
        self.assertEqual(file_sha256(admission_path), report["admission_sha256"])
        self.assertEqual(admission["admission_id"], report["admission_id"])
        self.assertEqual(response_before, response_path.read_bytes())
        self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))

    def test_wrong_page_id_is_rejected_and_round_remains_consumed(self) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        response = self.valid_response(admission)
        response["page_id"] = "page-001-ai-operations"

        report = self.validate_response(admission_path, invocation_path, response)

        self.assertFalse(report["valid"])
        self.assertIn(
            "REVIEW_ADMISSION_PAGE_MISMATCH",
            {item["code"] for item in report["errors"]},
        )
        with self.assertRaises(ToolError) as raised:
            self.invoke_again(admission)
        self.assertEqual("REVIEW_ROUND_ALREADY_INVOKED", raised.exception.code)

    def test_non_page_identity_fields_must_equal_the_admission(self) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        cases = (
            ("admission_id", "f" * 64),
            ("review_round", 2),
            ("source_sha256", "e" * 64),
            ("preview_sha256", "d" * 64),
        )
        for field, wrong_value in cases:
            with self.subTest(field=field):
                response = self.valid_response(admission)
                response[field] = wrong_value
                report = self.validate_response(
                    admission_path, invocation_path, response
                )
                self.assertFalse(report["valid"])
                self.assertIn(
                    "REVIEW_RESPONSE_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_response_requires_exactly_the_nine_prompt_fields(self) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        missing = self.valid_response(admission)
        del missing["p2_disclosures"]
        extra = self.valid_response(admission)
        extra["comment"] = "not admitted"
        for name, response in (("missing", missing), ("extra", extra)):
            with self.subTest(name=name):
                report = self.validate_response(
                    admission_path, invocation_path, response
                )
                self.assertFalse(report["valid"])
                self.assertIn(
                    "REVIEW_RESPONSE_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_coverage_requires_exact_keys_and_allowed_values(self) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        cases: list[tuple[str, Callable[[dict[str, object]], None]]] = [
            ("missing", lambda coverage: coverage.pop("high_risk_regions")),
            ("extra", lambda coverage: coverage.update({"unknown": "checked"})),
            (
                "value",
                lambda coverage: coverage.update(
                    {"canvas_and_regions": "partially_checked"}
                ),
            ),
            (
                "non-scalar-value",
                lambda coverage: coverage.update({"canvas_and_regions": []}),
            ),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                response = self.valid_response(admission)
                coverage = response["coverage"]
                assert isinstance(coverage, dict)
                mutate(coverage)
                report = self.validate_response(
                    admission_path, invocation_path, response
                )
                self.assertFalse(report["valid"])
                self.assertIn(
                    "REVIEW_RESPONSE_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_each_finding_requires_all_six_contract_fields(self) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        required = (
            "severity",
            "category",
            "location",
            "source_fact",
            "observed_difference",
            "evidence",
        )
        for field in required:
            with self.subTest(field=field):
                response = self.valid_response(admission)
                response["decision"] = "changes_required"
                finding = {
                    "severity": "P1",
                    "category": "objects_and_geometry",
                    "location": "body",
                    "source_fact": "aligned in source",
                    "observed_difference": "misaligned in candidate",
                    "evidence": [str(self.overlay.resolve())],
                }
                del finding[field]
                response["findings"] = [finding]
                report = self.validate_response(
                    admission_path, invocation_path, response
                )
                self.assertFalse(report["valid"])
                self.assertIn(
                    "REVIEW_RESPONSE_INVALID",
                    {item["code"] for item in report["errors"]},
                )
        response = self.valid_response(admission)
        response["findings"] = [
            {
                "severity": [],
                "category": "objects_and_geometry",
                "location": "body",
                "source_fact": "source",
                "observed_difference": "difference",
                "evidence": [str(self.overlay.resolve())],
            }
        ]
        report = self.validate_response(admission_path, invocation_path, response)
        self.assertFalse(report["valid"])
        self.assertIn(
            "REVIEW_RESPONSE_INVALID", {item["code"] for item in report["errors"]}
        )

    def test_response_rejects_noncanonical_json_numbers_and_lone_surrogates(
        self,
    ) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        cases: list[tuple[str, bytes]] = []
        for name, value in (("nan", float("nan")), ("infinity", float("inf"))):
            response = self.valid_response(admission)
            response["p2_disclosures"] = [value]
            cases.append(
                (
                    name,
                    (json.dumps(response, allow_nan=True, sort_keys=True) + "\n").encode(
                        "utf-8"
                    ),
                )
            )
        surrogate = self.valid_response(admission)
        surrogate["p2_disclosures"] = [
            {
                "severity": "P2",
                "category": "objects_and_geometry",
                "location": "\ud800",
                "source_fact": "source",
                "observed_difference": "difference",
                "evidence": ["region.png"],
            }
        ]
        cases.append(
            (
                "lone-surrogate",
                (json.dumps(surrogate, ensure_ascii=True, sort_keys=True) + "\n").encode(
                    "ascii"
                ),
            )
        )
        for index, (name, raw) in enumerate(cases):
            with self.subTest(name=name):
                response_path = self.root / f"strict-json-{name}.json"
                output = self.root / f"strict-json-{name}-report.json"
                response_path.write_bytes(raw)

                report = contracts.validate_response(
                    admission_path, invocation_path, response_path, output
                )

                self.assertFalse(report["valid"])
                self.assertIn(
                    "REVIEW_RESPONSE_INVALID",
                    {item["code"] for item in report["errors"]},
                )
                self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))

    def test_response_requires_typed_nonempty_findings_and_p2_disclosures(
        self,
    ) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        base = {
            "severity": "P1",
            "category": "objects_and_geometry",
            "location": "body",
            "source_fact": "source",
            "observed_difference": "difference",
            "evidence": ["region.png"],
        }
        cases: list[tuple[str, Callable[[dict[str, object]], None]]] = []
        for value in ([], {}, 1, None, "", "unknown"):
            cases.append(
                (
                    f"category-{value!r}",
                    lambda response, value=value: response["findings"][0].update(
                        {"category": value}
                    ),
                )
            )
        for field in ("location", "source_fact", "observed_difference"):
            for value in ([], {}, 1, None, "", "   "):
                cases.append(
                    (
                        f"{field}-{value!r}",
                        lambda response, field=field, value=value: response[
                            "findings"
                        ][0].update({field: value}),
                    )
                )
        for value in ({}, 1, None, [], [""], ["   "], [1], ["ok", {}]):
            cases.append(
                (
                    f"evidence-{value!r}",
                    lambda response, value=value: response["findings"][0].update(
                        {"evidence": value}
                    ),
                )
            )
        disclosure_cases = (
            1,
            {},
            "P2",
            [1],
            [{}],
            [{**base, "severity": "P1"}],
            [{**base, "severity": "P2", "evidence": []}],
        )
        for value in disclosure_cases:
            cases.append(
                (
                    f"p2-disclosures-{value!r}",
                    lambda response, value=value: response.update(
                        {"p2_disclosures": value}
                    ),
                )
            )
        for index, (name, mutate) in enumerate(cases):
            with self.subTest(name=name):
                response = self.valid_response(admission)
                response["decision"] = "changes_required"
                response["findings"] = [copy.deepcopy(base)]
                mutate(response)

                report = self.validate_response(
                    admission_path, invocation_path, response
                )

                self.assertFalse(report["valid"])
                self.assertIn(
                    "REVIEW_RESPONSE_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_decision_and_p0_p1_findings_are_consistent_both_ways(self) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        cases = (
            ("passed-with-p0", "passed", ["P0"]),
            ("not-reviewable-with-p1", "not_reviewable", ["P1"]),
            ("changes-without-blocker", "changes_required", ["P2"]),
            ("non-scalar-decision", [], []),
        )
        for name, decision, severities in cases:
            with self.subTest(name=name):
                response = self.valid_response(admission)
                response["decision"] = decision
                response["findings"] = [
                    {
                        "severity": severity,
                        "category": "objects_and_geometry",
                        "location": "body",
                        "source_fact": "source",
                        "observed_difference": "difference",
                        "evidence": [str(self.overlay.resolve())],
                    }
                    for severity in severities
                ]
                report = self.validate_response(
                    admission_path, invocation_path, response
                )
                self.assertFalse(report["valid"])
                self.assertIn(
                    "REVIEW_RESPONSE_INVALID",
                    {item["code"] for item in report["errors"]},
                )

    def test_passed_rejects_not_reviewable_coverage(self) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        response = self.valid_response(admission)
        response["coverage"]["pictures_crop_layers"] = "not_reviewable"

        report = self.validate_response(admission_path, invocation_path, response)

        self.assertFalse(report["valid"])
        self.assertIn(
            "REVIEW_RESPONSE_INVALID", {item["code"] for item in report["errors"]}
        )

    def test_response_without_invocation_is_invalid_but_still_writes_report(self) -> None:
        admission = self.issue()
        response = self.valid_response(admission)
        response_path = self.root / "review-response.json"
        output = self.root / "review-response-validation.json"
        _write_json(response_path, response)

        report = contracts.validate_response(
            self.admission_path,
            self.root / "missing-invocation.json",
            response_path,
            output,
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "REVIEW_RESPONSE_INVALID", {item["code"] for item in report["errors"]}
        )
        self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))

    def test_missing_malformed_or_stale_inputs_still_publish_exact_invalid_audit(
        self,
    ) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        original_admission = admission_path.read_bytes()
        valid_response = self.valid_response(admission)
        base_response_path = self.root / "audit-response.json"
        _write_json(base_response_path, valid_response)
        stale = copy.deepcopy(admission)
        stale["admission_id"] = "f" * 64
        cases = (
            (
                "missing-admission",
                self.root / "missing-admission.json",
                base_response_path,
                None,
            ),
            ("malformed-admission", admission_path, base_response_path, b"{\n"),
            (
                "stale-admission",
                admission_path,
                base_response_path,
                (json.dumps(stale, sort_keys=True) + "\n").encode("utf-8"),
            ),
            (
                "missing-response",
                admission_path,
                self.root / "missing-response.json",
                original_admission,
            ),
            (
                "malformed-response",
                admission_path,
                self.root / "malformed-response.json",
                original_admission,
            ),
        )
        expected_fields = {
            "schema_version",
            "admission_id",
            "page_id",
            "review_round",
            "admission_path",
            "admission_sha256",
            "invocation_path",
            "invocation_sha256",
            "response_path",
            "response_sha256",
            "valid",
            "errors",
        }
        for name, candidate_admission, response_path, admission_bytes in cases:
            with self.subTest(name=name):
                if candidate_admission == admission_path and admission_bytes is not None:
                    admission_path.write_bytes(admission_bytes)
                if name == "malformed-response":
                    response_path.write_bytes(b"{\n")
                output = self.root / f"{name}-audit.json"
                try:
                    escaped_error = None
                    try:
                        report = contracts.validate_response(
                            candidate_admission,
                            invocation_path,
                            response_path,
                            output,
                        )
                    except ToolError as exc:
                        escaped_error = exc.as_dict()
                        report = None
                    if escaped_error is not None:
                        self.fail(
                            f"validation failure escaped audit report: {escaped_error}"
                        )
                    assert report is not None
                    self.assertFalse(report["valid"])
                    self.assertEqual(expected_fields, set(report))
                    self.assertEqual(
                        str(candidate_admission.resolve(strict=False)),
                        report["admission_path"],
                    )
                    self.assertEqual(
                        str(response_path.resolve(strict=False)), report["response_path"]
                    )
                    self.assertEqual(
                        None if not candidate_admission.exists() else file_sha256(candidate_admission),
                        report["admission_sha256"],
                    )
                    self.assertEqual(
                        None if not response_path.exists() else file_sha256(response_path),
                        report["response_sha256"],
                    )
                    if name.endswith("admission"):
                        self.assertIsNone(report["admission_id"])
                        self.assertIsNone(report["page_id"])
                        self.assertIsNone(report["review_round"])
                    self.assertEqual(
                        report, json.loads(output.read_text(encoding="utf-8"))
                    )
                finally:
                    admission_path.write_bytes(original_admission)

    def test_validate_response_cli_publishes_audit_for_missing_response(self) -> None:
        _admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        missing = self.root / "cli-missing-response.json"
        output = self.root / "cli-missing-response-audit.json"
        cli = _load_cli()

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            code = cli.main(
                [
                    "validate-response",
                    "--admission",
                    str(admission_path),
                    "--invocation",
                    str(invocation_path),
                    "--response",
                    str(missing),
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(2, code)
        self.assertTrue(output.exists(), "CLI must persist the invalid audit report")
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(report["valid"])
        self.assertIsNone(report["response_sha256"])

    def test_existing_response_validation_output_is_not_overwritten(self) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        response_path = self.root / "review-response.json"
        output = self.root / "review-response-validation.json"
        _write_json(response_path, self.valid_response(admission))
        output.write_bytes(b"original\n")

        with self.assertRaises(ToolError) as raised:
            contracts.validate_response(
                admission_path, invocation_path, response_path, output
            )

        self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
        self.assertEqual(b"original\n", output.read_bytes())

    def test_response_validation_rolls_back_after_any_input_drifts_postpublish(
        self,
    ) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        real_publish = contracts.publish_json_no_overwrite
        targets = (
            ("response", lambda response_path: response_path),
            ("admission", lambda _response_path: admission_path),
            ("invocation", lambda _response_path: invocation_path),
        )
        for validity in ("valid", "invalid"):
            for target_name, target_for in targets:
                with self.subTest(validity=validity, target=target_name):
                    response = self.valid_response(admission)
                    if validity == "invalid":
                        response["page_id"] = "wrong-page"
                    response_path = self.root / f"transaction-{validity}-{target_name}.json"
                    output = self.root / f"transaction-{validity}-{target_name}-report.json"
                    _write_json(response_path, response)
                    target = target_for(response_path)
                    original = target.read_bytes()

                    def publish_then_drift(path, payload):
                        receipt = real_publish(path, payload)
                        target.write_bytes(original + b" \n")
                        return receipt

                    try:
                        with mock.patch.object(
                            contracts,
                            "publish_json_no_overwrite",
                            side_effect=publish_then_drift,
                        ):
                            with self.assertRaises(ToolError) as raised:
                                contracts.validate_response(
                                    admission_path,
                                    invocation_path,
                                    response_path,
                                    output,
                                )
                        self.assertEqual(
                            "BUILD_OUTPUT_INCOMPLETE", raised.exception.code
                        )
                        self.assertFalse(output.exists())
                    finally:
                        target.write_bytes(original)

    def test_response_validation_rejects_fixed_report_replacement_and_preserves_competitor(
        self,
    ) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        real_publish = contracts.publish_json_no_overwrite
        competitor = b'{"competitor":true}\n'
        for validity in ("valid", "invalid"):
            with self.subTest(validity=validity):
                response = self.valid_response(admission)
                if validity == "invalid":
                    response["page_id"] = "wrong-page"
                response_path = self.root / f"replacement-{validity}.json"
                output = self.root / f"replacement-{validity}-report.json"
                owned = self.root / f"replacement-{validity}-owned.json"
                _write_json(response_path, response)

                def publish_then_replace(path, payload):
                    receipt = real_publish(path, payload)
                    Path(path).rename(owned)
                    Path(path).write_bytes(competitor)
                    return receipt

                with mock.patch.object(
                    contracts,
                    "publish_json_no_overwrite",
                    side_effect=publish_then_replace,
                ):
                    with self.assertRaises(ToolError) as raised:
                        contracts.validate_response(
                            admission_path,
                            invocation_path,
                            response_path,
                            output,
                        )

                self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
                self.assertEqual(competitor, output.read_bytes())
                self.assertTrue(owned.exists())

    def test_response_validation_final_receipt_rejects_same_inode_rewrite(
        self,
    ) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        real_publish = contracts.publish_json_no_overwrite
        for validity in ("valid", "invalid"):
            with self.subTest(validity=validity):
                response = self.valid_response(admission)
                if validity == "invalid":
                    response["page_id"] = "wrong-page"
                response_path = self.root / f"same-inode-{validity}.json"
                output = self.root / f"same-inode-{validity}-report.json"
                _write_json(response_path, response)

                def publish_then_rewrite(path, payload):
                    receipt = real_publish(path, payload)
                    with Path(path).open("r+b") as stream:
                        stream.write(b" " * receipt.byte_count)
                        stream.truncate(receipt.byte_count)
                    return receipt

                with mock.patch.object(
                    contracts,
                    "publish_json_no_overwrite",
                    side_effect=publish_then_rewrite,
                ):
                    with self.assertRaises(ToolError) as raised:
                        contracts.validate_response(
                            admission_path,
                            invocation_path,
                            response_path,
                            output,
                        )

                self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
                self.assertFalse(output.exists())
                retained = list(self.root.glob(f".{output.name}.*.rollback"))
                self.assertEqual(1, len(retained))

    def test_validate_response_cli_writes_invalid_report_and_exits_two(self) -> None:
        admission, _invocation, admission_path, invocation_path = self.invoked_round_one()
        response = self.valid_response(admission)
        response["page_id"] = "page-001-ai-operations"
        response_path = self.root / "review-response.json"
        output = self.root / "review-response-validation.json"
        _write_json(response_path, response)
        cli = _load_cli()

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            code = cli.main(
                [
                    "validate-response",
                    "--admission",
                    str(admission_path),
                    "--invocation",
                    str(invocation_path),
                    "--response",
                    str(response_path),
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(2, code)
        self.assertFalse(json.loads(output.read_text(encoding="utf-8"))["valid"])

    def test_round_two_requires_every_p0_p1_mapping_passed(self) -> None:
        prior = self.validated_changes_required_response(severities=["P1", "P1"])
        self.add_high_risk_mapping(prior, finding_index=0, result="passed")

        with self.assertRaises(ToolError) as raised:
            self.issue_round_two(prior)

        self.assertEqual("REVIEW_ROUND_NOT_ADMITTED", raised.exception.code)

    def test_round_two_rejects_a_complete_prior_chain_from_another_page(
        self,
    ) -> None:
        prior = self.validated_changes_required_response()
        self._make_fixture(page_id="page-002")
        self.output = self.root / "admission-round-2-cross-page"
        self.invocations = self.root / "invocations-round-2-cross-page"
        self.add_high_risk_mapping(prior)

        with self.assertRaises(ToolError) as raised:
            self.issue_round_two(prior)

        self.assertEqual("REVIEW_ROUND_NOT_ADMITTED", raised.exception.code)
        self.assertFalse(self.output.exists())

    def test_round_two_requires_new_pptx_and_preview_hashes(self) -> None:
        prior = self.validated_changes_required_response()
        prior_pptx = Path(prior["admission"]["artifacts"]["pptx"]["path"])
        prior_preview = Path(prior["admission"]["artifacts"]["preview"]["path"])
        cases = (
            ("unchanged-pptx", {"pptx_bytes": prior_pptx.read_bytes()}),
            ("unchanged-preview", {"preview_bytes": prior_preview.read_bytes()}),
        )
        for name, fixture_kwargs in cases:
            with self.subTest(name=name):
                self._make_fixture(**fixture_kwargs)
                self.output = self.root / f"admission-round-2-{name}"
                self.invocations = self.root / f"invocations-round-2-{name}"
                self.add_high_risk_mapping(prior)

                with self.assertRaises(ToolError) as raised:
                    self.issue_round_two(prior)

                self.assertEqual("REVIEW_ROUND_NOT_ADMITTED", raised.exception.code)
                self.assertFalse(self.output.exists())

    def test_round_two_requires_exactly_one_matching_item_per_finding(self) -> None:
        prior = self.validated_changes_required_response()
        copied_evidence = self.root / "copied-overlay.png"
        shutil.copy2(self.overlay, copied_evidence)
        cases: tuple[tuple[str, Callable[[], None]], ...] = (
            (
                "wrong-source",
                lambda: self.add_high_risk_mapping(prior, source="manual:finding:0"),
            ),
            (
                "wrong-severity",
                lambda: self.add_high_risk_mapping(prior, severity="P0"),
            ),
            (
                "wrong-category",
                lambda: self.add_high_risk_mapping(
                    prior, category="canvas_and_regions"
                ),
            ),
            (
                "not-passed",
                lambda: self.add_high_risk_mapping(
                    prior, result="changes_required"
                ),
            ),
            (
                "empty-evidence",
                lambda: self.add_high_risk_mapping(prior, evidence=[]),
            ),
            (
                "unbound-evidence-copy",
                lambda: self.add_high_risk_mapping(
                    prior, evidence=[str(copied_evidence.resolve())]
                ),
            ),
            (
                "duplicate",
                lambda: (
                    self.add_high_risk_mapping(prior),
                    self.add_high_risk_mapping(prior),
                ),
            ),
        )
        for name, arrange in cases:
            with self.subTest(name=name):
                self._restore_fixture()
                arrange()
                with self.assertRaises(ToolError) as raised:
                    self.issue_round_two(prior)
                self.assertEqual("REVIEW_ROUND_NOT_ADMITTED", raised.exception.code)

    def test_round_two_requires_exact_admitted_evidence_path_strings(self) -> None:
        prior = self.validated_changes_required_response(
            category="text_and_typography"
        )
        alias_dir = self.overlay.parent / "alias-dir"
        alias_dir.mkdir()
        symlink = self.overlay.parent / "overlay-alias.png"
        symlink.symlink_to(self.overlay)
        dotdot = str(alias_dir / ".." / self.overlay.name)
        cases: tuple[tuple[str, str, Callable[[], None]], ...] = (
            (
                "item-symlink",
                "REVIEW_ADMISSION_NOT_ISSUED",
                lambda: self.add_high_risk_mapping(
                    prior, evidence=[str(symlink)]
                ),
            ),
            (
                "item-dotdot",
                "REVIEW_ROUND_NOT_ADMITTED",
                lambda: self.add_high_risk_mapping(prior, evidence=[dotdot]),
            ),
            (
                "typography-symlink",
                "REVIEW_ADMISSION_NOT_ISSUED",
                lambda: self.add_high_risk_mapping(
                    prior,
                    verification={
                        name: {
                            "status": "passed",
                            "path": str(symlink),
                            "sha256": file_sha256(self.overlay),
                        }
                        for name in (
                            "dense_text",
                            "numbers_and_units",
                            "wrap_sensitive",
                        )
                    },
                ),
            ),
        )
        for name, expected_code, arrange in cases:
            with self.subTest(name=name):
                self._restore_fixture()
                arrange()

                with self.assertRaises(ToolError) as raised:
                    self.issue_round_two(prior)

                self.assertEqual(expected_code, raised.exception.code)

    def test_round_two_rejects_copy_hardlink_and_drifted_evidence(self) -> None:
        prior = self.validated_changes_required_response()
        copied = self.root / "same-bytes-copy.png"
        linked = self.root / "same-inode-hardlink.png"
        shutil.copy2(self.overlay, copied)
        linked.hardlink_to(self.overlay)
        cases = (
            ("copy", str(copied), "REVIEW_ADMISSION_NOT_ISSUED", False),
            ("hardlink", str(linked), "REVIEW_ADMISSION_NOT_ISSUED", False),
            ("hash-drift", str(self.overlay), "REVIEW_ADMISSION_NOT_ISSUED", True),
        )
        for name, evidence, expected_code, drift in cases:
            with self.subTest(name=name):
                self._restore_fixture()
                self.add_high_risk_mapping(prior, evidence=[evidence])
                if drift:
                    self.overlay.write_bytes(self.overlay.read_bytes() + b"drift")

                with self.assertRaises(ToolError) as raised:
                    self.issue_round_two(prior)

                self.assertEqual(expected_code, raised.exception.code)

    def test_typography_p1_requires_three_global_closure_regions(self) -> None:
        prior = self.validated_changes_required_response(
            category="text_and_typography"
        )
        copied_evidence = self.root / "copied-typography-evidence.png"
        shutil.copy2(self.overlay, copied_evidence)
        valid = self._current_verification()
        missing = {"dense_text": valid["dense_text"]}
        extra = copy.deepcopy(valid)
        extra["extra"] = copy.deepcopy(valid["dense_text"])
        bad_status = copy.deepcopy(valid)
        bad_status["numbers_and_units"]["status"] = "changes_required"
        wrong_path = copy.deepcopy(valid)
        wrong_path["wrap_sensitive"]["path"] = str(copied_evidence.resolve())
        wrong_hash = copy.deepcopy(valid)
        wrong_hash["dense_text"]["sha256"] = "0" * 64
        cases = (
            ("missing", missing),
            ("extra", extra),
            ("status", bad_status),
            ("path", wrong_path),
            ("hash", wrong_hash),
        )
        for name, verification in cases:
            with self.subTest(name=name):
                self._restore_fixture()
                self.add_high_risk_mapping(prior, verification=verification)
                with self.assertRaises(ToolError) as raised:
                    self.issue_round_two(prior)
                self.assertEqual("REVIEW_ROUND_NOT_ADMITTED", raised.exception.code)

    def test_round_two_requires_valid_changes_required_prior_response(self) -> None:
        passed_prior = self._validated_prior_response(
            decision="passed", severities=[], category="objects_and_geometry"
        )
        with self.assertRaises(ToolError) as raised:
            self.issue_round_two(passed_prior)
        self.assertEqual("REVIEW_ROUND_NOT_ADMITTED", raised.exception.code)

        self._restore_fixture()
        changes_prior = self.validated_changes_required_response()
        invalid = copy.deepcopy(changes_prior["validation"])
        invalid["valid"] = False
        invalid["errors"] = [
            {
                "code": "REVIEW_RESPONSE_INVALID",
                "path": "response",
                "detail": "invalidated fixture",
            }
        ]
        _write_json(changes_prior["validation_path"], invalid)
        self.add_high_risk_mapping(changes_prior)
        with self.assertRaises(ToolError) as raised:
            self.issue_round_two(changes_prior)
        self.assertEqual("REVIEW_ROUND_NOT_ADMITTED", raised.exception.code)

    def test_round_two_rechecks_common_gates_before_admission(self) -> None:
        prior = self.validated_changes_required_response()
        self.add_high_risk_mapping(prior)
        self.text_report["valid"] = False

        with self.assertRaises(ToolError) as raised:
            self.issue_round_two(prior)

        self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)
        self.assertFalse(self.output.exists())

    def test_round_two_binds_current_candidate_and_can_be_invoked(self) -> None:
        prior = self.validated_changes_required_response(
            severities=["P0", "P1"]
        )
        self.add_high_risk_mapping(prior, finding_index=0)
        self.add_high_risk_mapping(prior, finding_index=1)

        admission = self.issue_round_two(prior)

        self.assertEqual(2, admission["review_round"])
        self.assertEqual(str(self.pptx.resolve()), admission["artifacts"]["pptx"]["path"])
        self.assertEqual(
            str(self.preview.resolve()), admission["artifacts"]["preview"]["path"]
        )
        self.assertNotEqual(
            prior["admission"]["artifacts"]["pptx"]["path"],
            admission["artifacts"]["pptx"]["path"],
        )
        self.assertNotEqual(
            prior["admission"]["artifacts"]["preview"]["path"],
            admission["artifacts"]["preview"]["path"],
        )
        self.assertNotEqual(
            prior["admission"]["pptx_sha256"], admission["pptx_sha256"]
        )
        self.assertNotEqual(
            prior["admission"]["preview_sha256"], admission["preview_sha256"]
        )
        invocation = contracts.record_invocation(
            self.admission_path, self.invocations
        )
        self.assertEqual(2, invocation["review_round"])
        self.assertEqual(admission["admission_id"], invocation["admission_id"])


class ReviewInvocationTests(AdmissionFixture):
    def test_invocation_records_fixed_hashes_without_timestamp(self) -> None:
        admission = self.issue()
        invocation = contracts.record_invocation(self.admission_path, self.invocations)
        self.assertEqual(
            {
                "schema_version",
                "admission_sha256",
                "admission_id",
                "page_id",
                "review_round",
                "prompt_sha256",
            },
            set(invocation),
        )
        self.assertEqual(file_sha256(self.admission_path), invocation["admission_sha256"])
        self.assertEqual(admission["admission_id"], invocation["admission_id"])
        self.assertEqual(file_sha256(self.prompt_path), invocation["prompt_sha256"])
        self.assertFalse(any("time" in key for key in invocation))

    def test_same_page_round_and_admission_are_rejected_across_files(self) -> None:
        admission = self.issue()
        first = contracts.record_invocation(self.admission_path, self.invocations)
        with self.assertRaises(ToolError) as raised:
            contracts.record_invocation(self.admission_path, self.invocations)
        self.assertEqual("REVIEW_ROUND_ALREADY_INVOKED", raised.exception.code)
        self.assertEqual(first, json.loads(self.invocation_path.read_text()))

        self.invocation_path.unlink()
        other = self.invocations / "other.json"
        _write_json(
            other,
            {
                "admission_id": admission["admission_id"],
                "page_id": "other-page",
                "review_round": 2,
            },
        )
        with self.assertRaises(ToolError) as raised:
            contracts.record_invocation(self.admission_path, self.invocations)
        self.assertEqual("REVIEW_ROUND_ALREADY_INVOKED", raised.exception.code)

    def test_malformed_invocation_json_fails_closed(self) -> None:
        self.issue()
        self.invocations.mkdir()
        (self.invocations / "unknown.json").write_text("[]\n", encoding="utf-8")
        with self.assertRaises(ToolError) as raised:
            contracts.record_invocation(self.admission_path, self.invocations)
        self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
        self.assertFalse(self.invocation_path.exists())

    def test_invalid_fixed_and_non_target_entries_propagate_scanner_failure(self) -> None:
        self.issue()
        builders: tuple[tuple[str, Callable[[Path], None]], ...] = (
            ("malformed", lambda path: path.write_text("{bad", encoding="utf-8")),
            ("missing-identity", lambda path: _write_json(path, {"schema_version": 1})),
            ("symlink", lambda path: path.symlink_to(self.root / "missing")),
            ("non-file", lambda path: path.mkdir()),
        )
        for location in ("fixed", "non-target"):
            for name, build in builders:
                with self.subTest(location=location, name=name):
                    directory = self.root / f"invocations-{location}-{name}"
                    directory.mkdir()
                    filename = (
                        "page-001-round-1-invocation.json"
                        if location == "fixed"
                        else "unknown.json"
                    )
                    entry = directory / filename
                    build(entry)
                    with self.assertRaises(ToolError) as raised:
                        contracts.record_invocation(self.admission_path, directory)
                    self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
                    self.assertTrue(entry.exists() or entry.is_symlink())

    def test_real_destination_competition_after_clean_scan_is_already_invoked(self) -> None:
        self.issue()
        real_publish = contracts.publish_json_no_overwrite
        competitor = {"competitor": True}

        def compete_then_publish(path: Path, payload: Any) -> None:
            _write_json(path, competitor)
            real_publish(path, payload)

        with mock.patch.object(
            contracts,
            "publish_json_no_overwrite",
            side_effect=compete_then_publish,
        ):
            with self.assertRaises(ToolError) as raised:
                contracts.record_invocation(self.admission_path, self.invocations)
        self.assertEqual("REVIEW_ROUND_ALREADY_INVOKED", raised.exception.code)
        self.assertEqual(competitor, json.loads(self.invocation_path.read_text()))

    def test_non_fileexists_publish_failure_is_not_mapped_by_destination_presence(
        self,
    ) -> None:
        self.issue()
        competitor = {"competitor": True}

        def appear_then_fail(path: Path, _payload: Any) -> None:
            _write_json(path, competitor)
            raise ToolError("BUILD_OUTPUT_INCOMPLETE", str(path), "disk failure")

        with mock.patch.object(
            contracts,
            "publish_json_no_overwrite",
            side_effect=appear_then_fail,
        ):
            with self.assertRaises(ToolError) as raised:
                contracts.record_invocation(self.admission_path, self.invocations)
        self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
        self.assertEqual(competitor, json.loads(self.invocation_path.read_text()))

    def test_tampered_admission_prompt_or_current_evidence_is_stale(self) -> None:
        admission = self.issue()
        tampered = copy.deepcopy(admission)
        tampered["source_sha256"] = "0" * 64
        _write_json(self.admission_path, tampered)
        with self.assertRaises(ToolError) as raised:
            contracts.record_invocation(self.admission_path, self.invocations)
        self.assertEqual("REVIEW_ADMISSION_STALE", raised.exception.code)
        _write_json(self.admission_path, admission)
        self.prompt_path.write_text("changed\n", encoding="utf-8")
        with self.assertRaises(ToolError) as raised:
            contracts.record_invocation(self.admission_path, self.invocations)
        self.assertEqual("REVIEW_ADMISSION_STALE", raised.exception.code)

    def test_validation_to_publish_drift_rolls_back_owned_invocation(self) -> None:
        mutations: tuple[tuple[str, Callable[[], None]], ...] = (
            ("prompt", lambda: self.prompt_path.write_text("changed\n", encoding="utf-8")),
            ("pptx", lambda: self.pptx.write_bytes(b"changed")),
            ("source", lambda: Image.new("RGB", (1600, 900), "red").save(self.source)),
            ("preview", lambda: Image.new("RGB", (1920, 1080), "red").save(self.preview)),
        )
        for name, mutation in mutations:
            with self.subTest(name=name):
                self.issue()
                real_publish = contracts.publish_json_no_overwrite

                def mutate_then_publish(path: Path, payload: Any):
                    mutation()
                    return real_publish(path, payload)

                with mock.patch.object(
                    contracts,
                    "publish_json_no_overwrite",
                    side_effect=mutate_then_publish,
                ):
                    with self.assertRaises(ToolError) as raised:
                        contracts.record_invocation(
                            self.admission_path, self.invocations
                        )
                self.assertEqual("REVIEW_ADMISSION_STALE", raised.exception.code)
                self.assertFalse(self.invocation_path.exists())
                self.output = self.root / f"admission-after-{name}"
                self.invocations = self.root / f"invocations-after-{name}"
                self._restore_fixture()

    def test_publication_infrastructure_failure_is_not_already_invoked(self) -> None:
        self.issue()
        with mock.patch.object(
            contracts,
            "publish_json_no_overwrite",
            side_effect=ToolError(
                "BUILD_OUTPUT_INCOMPLETE", str(self.invocation_path), "disk failure"
            ),
        ):
            with self.assertRaises(ToolError) as raised:
                contracts.record_invocation(self.admission_path, self.invocations)
        self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
        self.assertFalse(self.invocation_path.exists())

    def test_concurrent_invocation_has_one_winner(self) -> None:
        self.issue()

        def invoke_once():
            try:
                return contracts.record_invocation(self.admission_path, self.invocations)
            except ToolError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: invoke_once(), range(6)))
        self.assertEqual(1, sum(isinstance(item, dict) for item in results))
        self.assertTrue(self.invocation_path.is_file())


class AtomicNoOverwriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_directory_no_replace_classifies_existing_and_unsupported_errno(
        self,
    ) -> None:
        class FakeRename:
            argtypes: list[object] | None = None
            restype: object | None = None

            def __init__(self, error_number: int):
                self.error_number = error_number

            def __call__(self, *_args: object) -> int:
                contracts.ctypes.set_errno(self.error_number)
                return -1

        class FakeLibc:
            def __init__(self, error_number: int):
                self.renamex_np = FakeRename(error_number)

        cases = (
            (errno.EEXIST, FileExistsError),
            (errno.EINVAL, NotImplementedError),
        )
        for error_number, expected_type in cases:
            with self.subTest(error_number=error_number), mock.patch.object(
                contracts.sys, "platform", "darwin"
            ), mock.patch.object(
                contracts.ctypes,
                "CDLL",
                return_value=FakeLibc(error_number),
            ):
                try:
                    contracts._rename_directory_no_replace(
                        self.root / "source", self.root / "destination"
                    )
                except Exception as exc:  # noqa: BLE001 - behavior under test
                    raised = exc
                else:
                    self.fail("no-replace rename unexpectedly succeeded")
                self.assertIsInstance(raised, expected_type)

    def test_invocation_rollback_never_deletes_a_competing_inode(self) -> None:
        destination = self.root / "invocation.json"
        competitor = self.root / "competitor.json"
        destination.write_text("owned\n", encoding="utf-8")
        owned = destination.stat()
        competitor.write_text("competitor\n", encoding="utf-8")
        real_rename = contracts.os.rename
        real_no_replace = contracts._rename_directory_no_replace

        def replace_at_quarantine_boundary(source: Path, target: Path) -> None:
            if Path(source) == destination:
                real_rename(destination, self.root / "saved-owned-before-rollback")
                real_rename(competitor, destination)
            real_no_replace(source, target)

        with mock.patch.object(
            contracts,
            "_rename_directory_no_replace",
            side_effect=replace_at_quarantine_boundary,
        ):
            with self.assertRaises(ToolError) as raised:
                contracts._rollback_owned_invocation(
                    destination, (owned.st_dev, owned.st_ino)
                )
        self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
        self.assertEqual("competitor\n", destination.read_text(encoding="utf-8"))
        tombstones = list(self.root.glob(".*.rollback"))
        self.assertEqual([], tombstones)
        self.assertEqual(
            "owned\n",
            (self.root / "saved-owned-before-rollback").read_text(encoding="utf-8"),
        )

    def test_owned_rollback_does_not_unlink_a_post_stat_replacement(self) -> None:
        destination = self.root / "invocation.json"
        competitor = self.root / "competitor"
        saved_owned = self.root / "saved-owned"
        destination.write_text("owned\n", encoding="utf-8")
        owned = destination.stat()
        competitor.write_text("competitor\n", encoding="utf-8")
        real_stat = contracts.os.stat
        real_rename = contracts.os.rename
        replaced = False

        def replace_after_stat(path: Path, *args: Any, **kwargs: Any):
            nonlocal replaced
            result = real_stat(path, *args, **kwargs)
            if not replaced and str(path).endswith(".rollback"):
                replaced = True
                real_rename(path, saved_owned)
                real_rename(competitor, path)
            return result

        with mock.patch.object(contracts.os, "stat", side_effect=replace_after_stat):
            contracts._rollback_owned_invocation(
                destination, (owned.st_dev, owned.st_ino)
            )
        self.assertFalse(destination.exists())
        contents = {path.read_text(encoding="utf-8") for path in self.root.iterdir()}
        self.assertEqual({"owned\n", "competitor\n"}, contents)

    def test_competitor_restore_keeps_quarantine_and_never_unlinks_replacement(self) -> None:
        destination = self.root / "invocation.json"
        owned = self.root / "owned"
        replacement = self.root / "replacement"
        saved_competitor = self.root / "saved-competitor"
        destination.write_text("competitor\n", encoding="utf-8")
        owned.write_text("owned\n", encoding="utf-8")
        owned_stat = owned.stat()
        replacement.write_text("replacement\n", encoding="utf-8")
        real_unlink = Path.unlink
        real_rename = contracts.os.rename

        def replace_before_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
            if str(path).endswith(".rollback"):
                real_rename(path, saved_competitor)
                real_rename(replacement, path)
            real_unlink(path, *args, **kwargs)

        with mock.patch.object(
            Path, "unlink", autospec=True, side_effect=replace_before_unlink
        ):
            with self.assertRaises(ToolError) as raised:
                contracts._rollback_owned_invocation(
                    destination, (owned_stat.st_dev, owned_stat.st_ino)
                )
        self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
        contents = [path.read_text(encoding="utf-8") for path in self.root.iterdir()]
        self.assertEqual(1, contents.count("competitor\n"))
        self.assertEqual(1, contents.count("replacement\n"))

    def test_scanner_invalid_json_entries_do_not_claim_the_round_was_consumed(self) -> None:
        builders: tuple[tuple[str, Callable[[Path], None]], ...] = (
            ("malformed", lambda path: path.write_text("{bad", encoding="utf-8")),
            ("missing-identity", lambda path: _write_json(path, {"schema_version": 1})),
            ("non-file", lambda path: path.mkdir()),
            ("symlink", lambda path: path.symlink_to(self.root / "missing")),
        )
        for name, build in builders:
            with self.subTest(name=name):
                directory = self.root / name
                directory.mkdir()
                build(directory / "unknown.json")
                with self.assertRaises(ToolError) as raised:
                    contracts._scan_invocations(
                        directory,
                        admission_id="a" * 64,
                        page_id="page-001",
                        review_round=1,
                    )
                self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)

    def test_scanner_ignores_non_json_lock_and_tombstone_controls(self) -> None:
        (self.root / ".invocation.lock").write_text("lock\n", encoding="utf-8")
        (self.root / ".page-001.rollback").write_text(
            "tombstone\n", encoding="utf-8"
        )
        contracts._scan_invocations(
            self.root,
            admission_id="a" * 64,
            page_id="page-001",
            review_round=1,
        )

    def test_locked_pdffonts_recheck_rejects_non_utf8_and_oversized_output(self) -> None:
        pdf = self.root / "page.pdf"
        pdf.write_bytes(b"%PDF-fixture")
        cases = (
            ("non-utf8", "import sys\nsys.stdout.buffer.write(b'\\xff')\n"),
            (
                "oversized",
                "import sys\n"
                f"sys.stdout.buffer.write(b'x' * {contracts._PDFFONTS_STDOUT_LIMIT_BYTES + 1})\n",
            ),
        )
        for name, body in cases:
            with self.subTest(name=name):
                executable = self.root / name
                executable.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
                executable.chmod(0o755)
                with self.assertRaises(ToolError) as raised:
                    contracts._run_pdffonts_bounded(executable, pdf)
                self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)

    def test_locked_pdffonts_recheck_enforces_timeout(self) -> None:
        pdf = self.root / "page.pdf"
        pdf.write_bytes(b"%PDF-fixture")
        executable = self.root / "slow-pdffonts"
        executable.write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(2)\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        with mock.patch.object(contracts, "_PDFFONTS_TIMEOUT_SECONDS", 0.05):
            with self.assertRaises(ToolError) as raised:
                contracts._run_pdffonts_bounded(executable, pdf)
        self.assertEqual("REVIEW_ADMISSION_NOT_ISSUED", raised.exception.code)

    def test_existing_destination_is_never_overwritten(self) -> None:
        destination = self.root / "record.json"
        destination.write_bytes(b"original\n")
        with self.assertRaises(ToolError):
            publish_json_no_overwrite(destination, {"value": "replacement"})
        self.assertEqual(b"original\n", destination.read_bytes())

    def test_prepublish_failure_retains_one_auditable_candidate(self) -> None:
        destination = self.root / "record.json"
        with mock.patch.object(
            atomic_write, "_rename_no_replace", side_effect=OSError("boom")
        ):
            with self.assertRaises(ToolError):
                publish_json_no_overwrite(destination, {"value": 1})
        self.assertFalse(destination.exists())
        tombstones = list(self.root.glob(".record.json.*.rollback"))
        self.assertEqual(1, len(tombstones))
        self.assertEqual({"value": 1}, json.loads(tombstones[0].read_text()))

    def test_fsync_failure_quarantines_owned_publication_as_non_json(self) -> None:
        destination = self.root / "record.json"
        real_fsync_directory = atomic_write._fsync_directory
        calls = 0

        def fail_first_fsync(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ToolError(
                    "BUILD_OUTPUT_INCOMPLETE", str(path), "injected fsync failure"
                )
            real_fsync_directory(path)

        with mock.patch.object(
            atomic_write, "_fsync_directory", side_effect=fail_first_fsync
        ):
            with self.assertRaises(ToolError) as raised:
                publish_json_no_overwrite(destination, {"value": 1})
        self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
        self.assertFalse(destination.exists())
        tombstones = list(self.root.glob(".record.json.*.rollback"))
        self.assertGreaterEqual(len(tombstones), 1)
        self.assertTrue(
            all(
                json.loads(path.read_text(encoding="utf-8")) == {"value": 1}
                for path in tombstones
            )
        )
        self.assertEqual(
            [],
            [
                path
                for path in self.root.iterdir()
                if path.name.startswith(".record.json.")
                and not path.name.endswith(".rollback")
            ],
        )

    def test_fsync_failure_never_unlinks_destination_replaced_after_lstat(self) -> None:
        destination = self.root / "record.json"
        competitor = self.root / "competitor"
        saved_owned = self.root / "saved-owned"
        competitor.write_text("competitor\n", encoding="utf-8")
        real_lstat = Path.lstat
        real_rename = atomic_write.os.rename
        real_no_replace = atomic_write._rename_no_replace
        real_fsync_directory = atomic_write._fsync_directory
        fsync_calls = 0
        replaced = False

        def fail_first_fsync(path: Path) -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 1:
                raise ToolError(
                    "BUILD_OUTPUT_INCOMPLETE", str(path), "injected fsync failure"
                )
            real_fsync_directory(path)

        def replace_after_lstat(path: Path, *args: Any, **kwargs: Any):
            nonlocal replaced
            result = real_lstat(path, *args, **kwargs)
            if path == destination and not replaced:
                replaced = True
                real_rename(destination, saved_owned)
                real_rename(competitor, destination)
            return result

        with mock.patch.object(
            atomic_write, "_fsync_directory", side_effect=fail_first_fsync
        ), mock.patch.object(
            Path, "lstat", autospec=True, side_effect=replace_after_lstat
        ):
            with self.assertRaises(ToolError):
                publish_json_no_overwrite(destination, {"value": 1})
        contents = [path.read_text(encoding="utf-8") for path in self.root.iterdir()]
        self.assertIn("competitor\n", contents)

    def test_fsync_failure_restores_competitor_seen_at_quarantine_boundary(self) -> None:
        destination = self.root / "record.json"
        competitor = self.root / "competitor"
        competitor.write_text("competitor\n", encoding="utf-8")
        real_rename = atomic_write.os.rename
        real_no_replace = atomic_write._rename_no_replace
        real_fsync_directory = atomic_write._fsync_directory
        fsync_calls = 0
        replaced = False

        def fail_first_fsync(path: Path) -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 1:
                raise ToolError(
                    "BUILD_OUTPUT_INCOMPLETE", str(path), "injected fsync failure"
                )
            real_fsync_directory(path)

        def replace_at_quarantine_boundary(source: Path, target: Path) -> None:
            nonlocal replaced
            if source == destination and not replaced:
                replaced = True
                real_rename(destination, self.root / "saved-owned-before-quarantine")
                real_rename(competitor, destination)
            real_no_replace(source, target)

        with mock.patch.object(
            atomic_write, "_fsync_directory", side_effect=fail_first_fsync
        ), mock.patch.object(
            atomic_write,
            "_rename_no_replace",
            side_effect=replace_at_quarantine_boundary,
        ):
            with self.assertRaises(ToolError) as raised:
                publish_json_no_overwrite(destination, {"value": 1})
        self.assertEqual("BUILD_OUTPUT_INCOMPLETE", raised.exception.code)
        self.assertEqual("competitor\n", destination.read_text(encoding="utf-8"))
        tombstones = list(self.root.glob(".record.json.*.rollback"))
        self.assertEqual([], tombstones)

    def test_fsync_failure_never_unlinks_replaced_candidate_path(self) -> None:
        destination = self.root / "record.json"
        competitor = self.root / "candidate-competitor"
        saved_candidate = self.root / "saved-candidate"
        competitor.write_text("competitor\n", encoding="utf-8")
        real_unlink = Path.unlink
        real_rename = atomic_write.os.rename
        real_fsync_directory = atomic_write._fsync_directory
        fsync_calls = 0
        replaced = False

        def fail_first_fsync(path: Path) -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 1:
                raise ToolError(
                    "BUILD_OUTPUT_INCOMPLETE", str(path), "injected fsync failure"
                )
            real_fsync_directory(path)

        def replace_before_candidate_unlink(
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
                real_rename(path, saved_candidate)
                real_rename(competitor, path)
            real_unlink(path, *args, **kwargs)

        with mock.patch.object(
            atomic_write, "_fsync_directory", side_effect=fail_first_fsync
        ), mock.patch.object(
            Path,
            "unlink",
            autospec=True,
            side_effect=replace_before_candidate_unlink,
        ):
            with self.assertRaises(ToolError):
                publish_json_no_overwrite(destination, {"value": 1})
        contents = [path.read_text(encoding="utf-8") for path in self.root.iterdir()]
        self.assertIn("competitor\n", contents)
        self.assertEqual(
            [],
            [
                path
                for path in self.root.iterdir()
                if path.name.startswith(".record.json.")
                and not path.name.endswith(".rollback")
            ],
        )

    def test_concurrent_publication_has_one_winner(self) -> None:
        destination = self.root / "record.json"

        def publish(index: int):
            try:
                publish_json_no_overwrite(destination, {"winner": index})
                return index
            except ToolError:
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(publish, range(6)))
        winners = [item for item in results if item is not None]
        self.assertEqual(1, len(winners))
        self.assertEqual({"winner": winners[0]}, json.loads(destination.read_text()))
        self.assertEqual([], list(self.root.glob(".record.json.*")))


class ReviewAdmissionCliTests(AdmissionFixture):
    def test_issue_and_invoke_cli_publish_fixed_contracts(self) -> None:
        cli = _load_cli()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(
                [
                    "issue",
                    "--spec",
                    str(self.spec_path),
                    "--pptx",
                    str(self.pptx),
                    "--build-report",
                    str(self.build_path),
                    "--structure-report",
                    str(self.structure_path),
                    "--render-report",
                    str(self.render_path),
                    "--text-geometry",
                    str(self.text_path),
                    "--background-report",
                    str(self.background_path),
                    "--visual-diff",
                    str(self.visual_path),
                    "--review-round",
                    "1",
                    "--output-dir",
                    str(self.output),
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual("page-001", json.loads(stdout.getvalue())["page_id"])
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(
                [
                    "invoke",
                    "--admission",
                    str(self.admission_path),
                    "--invocation-dir",
                    str(self.invocations),
                ]
            )
        self.assertEqual(0, code)

    def test_round_two_cli_is_structured_fail_closed(self) -> None:
        cli = _load_cli()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(
                [
                    "issue",
                    "--spec",
                    str(self.spec_path),
                    "--pptx",
                    str(self.pptx),
                    "--build-report",
                    str(self.build_path),
                    "--structure-report",
                    str(self.structure_path),
                    "--render-report",
                    str(self.render_path),
                    "--text-geometry",
                    str(self.text_path),
                    "--background-report",
                    str(self.background_path),
                    "--visual-diff",
                    str(self.visual_path),
                    "--review-round",
                    "2",
                    "--output-dir",
                    str(self.output),
                ]
            )
        self.assertEqual(2, code)
        self.assertEqual(
            "REVIEW_ROUND_NOT_ADMITTED", json.loads(stderr.getvalue())["error"]["code"]
        )


if __name__ == "__main__":
    unittest.main()
