"""Immutable reviewer-admission and invocation contracts."""

from __future__ import annotations

import copy
import ctypes
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomic_write import _fsync_directory, publish_json_no_overwrite
from .error_codes import ToolError
from .hashing import canonical_json_sha256
from .no_replace_transactions import (
    DirectoryPublicationReceipt,
    DirectoryLock,
    FileIdentity,
    PublicationReceipt,
    TombstoneReceipt,
    TransactionFailure,
    publish_directory_no_replace,
    quarantine_publication,
    rename_no_replace,
    verify_directory_receipt,
    verify_file_receipt,
)
from .review_evidence import (
    EvidenceSnapshot,
    StableEvidenceView,
    activate_evidence_view,
    current_evidence_view,
    ensure_unchanged as _ensure_unchanged,
    expect as _expect,
    identity as _identity,
    is_sha256 as _is_sha256,
    load_json as _load_json,
    recorded_file as _recorded_file,
    snapshot_file as _snapshot_file,
)
from .reviewer_contracts import (
    VISUAL_REVIEW_COVERAGE_FIELDS,
    VISUAL_REVIEW_COVERAGE_RESULTS,
    VISUAL_REVIEW_DECISIONS,
    VISUAL_REVIEW_FINDING_FIELDS,
    valid_response_p2_disclosures,
    valid_visual_review_finding,
)
from .schema_io import reject_nonstandard_json_number
from .review_validators import (
    _run_pdffonts_bounded as _validator_run_pdffonts_bounded,
    _validate_background,
    _validate_build_and_structure,
    _validate_render,
    _validate_text_geometry,
    _validate_visual_diff,
)
from .spec_identity import (
    content_spec_sha256,
    input_spec_sha256,
    review_state_sha256,
)


_PAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PDFFONTS_TIMEOUT_SECONDS = 10.0
_PDFFONTS_STDOUT_LIMIT_BYTES = 1024 * 1024
_PDFFONTS_STDERR_LIMIT_BYTES = 64 * 1024
_PROMPT_FIELDS = [
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
_COVERAGE_FIELDS = VISUAL_REVIEW_COVERAGE_FIELDS
_COVERAGE_VALUES = VISUAL_REVIEW_COVERAGE_RESULTS
_FINDING_FIELDS = VISUAL_REVIEW_FINDING_FIELDS
_DECISIONS = VISUAL_REVIEW_DECISIONS
_ADMISSION_FIELDS = {
    "schema_version",
    "admission_id",
    "page_id",
    "review_round",
    "verification_profile",
    "spec_sha256",
    "input_spec_sha256",
    "review_state_sha256",
    "pptx_sha256",
    "source_sha256",
    "preview_sha256",
    "build_report_sha256",
    "structure_report_sha256",
    "render_report_sha256",
    "rendered_text_geometry_sha256",
    "background_contract_sha256",
    "visual_diff_sha256",
    "artifacts",
    "render_identity",
    "visual_evidence",
    "generator",
}
_INVOCATION_FIELDS = {
    "schema_version",
    "admission_sha256",
    "admission_id",
    "page_id",
    "review_round",
    "prompt_sha256",
}
_RESPONSE_VALIDATION_FIELDS = {
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
_PRIOR_REVIEW_FIELDS = {
    "admission",
    "invocation",
    "response",
    "response_validation",
}
_ARTIFACT_FIELDS = {
    "spec",
    "pptx",
    "source",
    "preview",
    "pdf",
    "font_report",
    "build_report",
    "structure_report",
    "render_report",
    "runtime_preflight",
    "rendered_text_geometry",
    "background_contract",
    "visual_diff",
}


@dataclass(frozen=True)
class AdmissionInputs:
    spec: Path
    pptx: Path
    build_report: Path
    structure_report: Path
    render_report: Path
    text_geometry: Path
    background_report: Path
    visual_diff: Path
    review_round: int
    prior_admission: Path | None = None
    prior_invocation: Path | None = None
    prior_response_validation: Path | None = None


_Snapshot = EvidenceSnapshot


@dataclass(frozen=True)
class _InvocationAdmission:
    payload: dict[str, Any]
    admission_snapshot: _Snapshot
    prompt_snapshot: _Snapshot
    stable_snapshots: tuple[_Snapshot, ...]


def _error(code: str, path: str, detail: str) -> ToolError:
    return ToolError(code, path, detail)


def _run_pdffonts_bounded(executable: Path, pdf: Path) -> bytes:
    """Compatibility seam for the focused runner tests."""

    from . import review_validators

    previous = (
        review_validators._PDFFONTS_TIMEOUT_SECONDS,
        review_validators._PDFFONTS_STDOUT_LIMIT_BYTES,
        review_validators._PDFFONTS_STDERR_LIMIT_BYTES,
    )
    review_validators._PDFFONTS_TIMEOUT_SECONDS = _PDFFONTS_TIMEOUT_SECONDS
    review_validators._PDFFONTS_STDOUT_LIMIT_BYTES = _PDFFONTS_STDOUT_LIMIT_BYTES
    review_validators._PDFFONTS_STDERR_LIMIT_BYTES = _PDFFONTS_STDERR_LIMIT_BYTES
    try:
        return _validator_run_pdffonts_bounded(executable, pdf)
    finally:
        (
            review_validators._PDFFONTS_TIMEOUT_SECONDS,
            review_validators._PDFFONTS_STDOUT_LIMIT_BYTES,
            review_validators._PDFFONTS_STDERR_LIMIT_BYTES,
        ) = previous


def recompute_admission_id(admission: dict[str, Any]) -> str:
    """Recompute an admission ID after excluding only the ID field itself."""
    if not isinstance(admission, dict):
        raise _error("REVIEW_ADMISSION_STALE", "admission", "admission must be an object")
    projected = copy.deepcopy(admission)
    projected.pop("admission_id", None)
    try:
        return canonical_json_sha256(projected)
    except (TypeError, UnicodeError, ValueError, OverflowError, RecursionError) as exc:
        raise _error("REVIEW_ADMISSION_STALE", "admission", "admission is not canonical JSON") from exc


def _construct_admission(inputs: AdmissionInputs) -> tuple[dict[str, Any], list[_Snapshot]]:
    if type(inputs.review_round) is not int or inputs.review_round not in {1, 2}:
        raise _error("REVIEW_ADMISSION_NOT_ISSUED", "review_round", "review round must be 1 or 2")
    prior_paths = (
        inputs.prior_admission,
        inputs.prior_invocation,
        inputs.prior_response_validation,
    )
    if inputs.review_round == 1 and any(path is not None for path in prior_paths):
        raise _error(
            "REVIEW_ADMISSION_NOT_ISSUED",
            "review_round",
            "round 1 must not supply a prior-review chain",
        )
    if inputs.review_round == 2 and any(path is None for path in prior_paths):
        raise _error(
            "REVIEW_ROUND_NOT_ADMITTED",
            "review_round",
            "round 2 requires prior admission, invocation, and response validation",
        )

    original_spec, spec_snapshot = _load_json(inputs.spec, "spec")
    original_build, build_snapshot = _load_json(inputs.build_report, "build_report")
    original_structure, structure_snapshot = _load_json(inputs.structure_report, "structure_report")
    original_render, render_snapshot = _load_json(inputs.render_report, "render_report")
    original_text, text_snapshot = _load_json(inputs.text_geometry, "text_geometry")
    text_input_paths = original_text.get("input_paths")
    _expect(isinstance(text_input_paths, dict), "text_geometry.input_paths", "complete input paths are required")
    controller_spec_path = text_input_paths.get("spec")
    _expect(
        isinstance(controller_spec_path, str) and bool(controller_spec_path),
        "text_geometry.input_paths.spec",
        "artifact-locked build spec snapshot path is required",
    )
    controller_spec, controller_spec_snapshot = _load_json(
        controller_spec_path,
        "controller_spec",
    )
    runtime_path = text_input_paths.get("runtime")
    _expect(isinstance(runtime_path, str) and bool(runtime_path), "text_geometry.input_paths.runtime", "runtime path is required")
    original_runtime, original_runtime_snapshot = _load_json(
        runtime_path, "runtime_preflight"
    )
    runtime_snapshot = original_runtime_snapshot
    original_background, background_snapshot = _load_json(inputs.background_report, "background_report")
    original_visual, visual_snapshot = _load_json(inputs.visual_diff, "visual_diff")
    _pptx_raw, pptx_snapshot = _snapshot_file(inputs.pptx, "pptx")
    view = current_evidence_view()
    if view is None:
        spec = original_spec
        build = original_build
        structure = original_structure
        render = original_render
        text = original_text
        runtime = original_runtime
        background = original_background
        visual = original_visual
    else:
        runtime = view.rebind_paths(original_runtime)
        runtime_snapshot = view.project_json(
            runtime_snapshot, runtime, "runtime_preflight"
        )
        render = view.rebind_paths(original_render)
        render_snapshot = view.project_json(
            render_snapshot, render, "render_report"
        )
        view.capture_paths(original_spec, preserve_path_semantics=True)
        spec = original_spec
        build = original_build
        structure = original_structure
        text = view.rebind_paths(original_text)
        background = view.rebind_paths(original_background)
        visual = view.rebind_paths(original_visual)

    snapshots = [
        spec_snapshot,
        build_snapshot,
        structure_snapshot,
        render_snapshot,
        runtime_snapshot,
        text_snapshot,
        controller_spec_snapshot,
        background_snapshot,
        visual_snapshot,
        pptx_snapshot,
    ]

    page_id = original_spec.get("page_id")
    _expect(isinstance(page_id, str) and _PAGE_ID.fullmatch(page_id) is not None, "spec.page_id", "page ID must be filename-safe")
    profile = original_spec.get("verification_profile")
    _expect(profile in {"reviewed", "strict"}, "spec.verification_profile", "independent reviewer admission requires reviewed or strict profile")
    try:
        content_hash = content_spec_sha256(original_spec)
        input_hash = input_spec_sha256(original_spec)
        state_hash = review_state_sha256(original_spec)
        controller_content_hash = content_spec_sha256(controller_spec)
        controller_input_hash = input_spec_sha256(controller_spec)
    except ToolError as exc:
        raise _error("REVIEW_ADMISSION_NOT_ISSUED", "spec", exc.detail) from exc
    _expect(
        controller_content_hash == content_hash,
        "controller_spec",
        "artifact-locked build spec does not match the current renderable content",
    )

    source_record = spec.get("clean_visual_reference")
    source = _recorded_file(source_record, "spec.clean_visual_reference", snapshots, image=True)
    _validate_build_and_structure(controller_spec, build, structure, pptx_snapshot)
    pdf, preview, font_report = _validate_render(
        render,
        render_snapshot,
        runtime,
        runtime_snapshot,
        pptx_snapshot,
        snapshots,
        production_runtime=original_runtime,
        production_runtime_snapshot=original_runtime_snapshot,
    )
    _validate_text_geometry(
        text,
        page_id=page_id,
        content_hash=content_hash,
        input_hash=controller_input_hash,
        source_hash=source.sha256,
        spec=controller_spec_snapshot,
        pptx=pptx_snapshot,
        build=build_snapshot,
        render=render_snapshot,
        runtime=runtime_snapshot,
        pdf=pdf,
        render_payload=render,
    )
    _validate_background(
        background,
        page_id=page_id,
        content_hash=content_hash,
        input_hash=controller_input_hash,
        pptx=pptx_snapshot,
        build=build_snapshot,
        build_payload=original_build,
        structure=structure_snapshot,
        structure_payload=original_structure,
    )
    visual_evidence = _validate_visual_diff(
        visual,
        visual_snapshot,
        spec,
        source,
        preview,
        pptx_snapshot,
        render_snapshot,
        render,
        pdf,
        snapshots,
    )
    prior_review = None
    if inputs.review_round == 2:
        prior_review = _validate_round_two_prior(
            inputs,
            current_spec=original_spec,
            current_visual_evidence=visual_evidence,
            current_pptx_sha256=pptx_snapshot.sha256,
            current_preview_sha256=preview.sha256,
        )

    generator_path = Path(__file__).resolve()
    _generator_raw, generator = _snapshot_file(generator_path, "review_admission.generator")
    snapshots.append(generator)
    artifacts = {
        "spec": _identity(spec_snapshot),
        "pptx": _identity(pptx_snapshot),
        "source": _identity(source),
        "preview": _identity(preview),
        "pdf": _identity(pdf),
        "font_report": _identity(font_report),
        "build_report": _identity(build_snapshot),
        "structure_report": _identity(structure_snapshot),
        "render_report": _identity(render_snapshot),
        "runtime_preflight": _identity(runtime_snapshot),
        "rendered_text_geometry": _identity(text_snapshot),
        "background_contract": _identity(background_snapshot),
        "visual_diff": _identity(visual_snapshot),
    }
    admission: dict[str, Any] = {
        "schema_version": 1,
        "page_id": page_id,
        "review_round": inputs.review_round,
        "verification_profile": profile,
        "spec_sha256": content_hash,
        "input_spec_sha256": input_hash,
        "review_state_sha256": state_hash,
        "pptx_sha256": pptx_snapshot.sha256,
        "source_sha256": source.sha256,
        "preview_sha256": preview.sha256,
        "build_report_sha256": build_snapshot.sha256,
        "structure_report_sha256": structure_snapshot.sha256,
        "render_report_sha256": render_snapshot.sha256,
        "rendered_text_geometry_sha256": text_snapshot.sha256,
        "background_contract_sha256": background_snapshot.sha256,
        "visual_diff_sha256": visual_snapshot.sha256,
        "artifacts": artifacts,
        "render_identity": {
            "renderer": copy.deepcopy(original_render["renderer"]),
            "rasterizer": copy.deepcopy(original_render["rasterizer"]),
            "text_extractor": copy.deepcopy(original_render["text_extractor"]),
            "pdffonts": copy.deepcopy(original_runtime["executables"]["pdffonts"]),
            "fontconfig": copy.deepcopy(original_runtime["fontconfig"]),
            "runtime_preflight_sha256": runtime_snapshot.sha256,
            "pdf_sha256": pdf.sha256,
            "font_report_sha256": font_report.sha256,
        },
        "visual_evidence": visual_evidence,
        "generator": {
            "name": "review_contracts.issue_admission",
            "schema_version": 1,
            **_identity(generator),
        },
    }
    if prior_review is not None:
        admission["prior_review"] = prior_review
    admission["admission_id"] = recompute_admission_id(admission)
    return admission, snapshots


def reviewer_prompt(admission: dict[str, Any]) -> str:
    """Build the fixed read-only reviewer prompt solely from an admission."""
    evidence = admission.get("visual_evidence")
    if not isinstance(evidence, dict):
        raise _error("REVIEW_ADMISSION_STALE", "visual_evidence", "visual evidence is missing")
    regions = evidence.get("regions")
    if not isinstance(regions, list):
        raise _error("REVIEW_ADMISSION_STALE", "visual_evidence.regions", "region evidence is missing")
    identity = {
        "admission_id": admission.get("admission_id"),
        "page_id": admission.get("page_id"),
        "review_round": admission.get("review_round"),
        "source_sha256": admission.get("source_sha256"),
        "preview_sha256": admission.get("preview_sha256"),
    }
    neutral_finding = {
        "severity": "<P0|P1|P2>",
        "category": "<coverage_key>",
        "location": "<non-empty string>",
        "source_fact": "<non-empty string>",
        "observed_difference": "<non-empty string>",
        "evidence": ["<current_visual_evidence_absolute_path>"],
    }
    neutral_response = {
        "admission_id": "<admission_id>",
        "page_id": "<page_id>",
        "review_round": "<integer 1|2>",
        "source_sha256": "<source_sha256>",
        "preview_sha256": "<preview_sha256>",
        "decision": "<passed|changes_required|not_reviewable>",
        "coverage": {
            field: "<checked|not_applicable|not_reviewable>"
            for field in sorted(_COVERAGE_FIELDS)
        },
        "findings": [neutral_finding],
        "p2_disclosures": [{**neutral_finding, "severity": "<P2>"}],
    }
    lines = [
        "你是独立视觉审查员，只读审查；不得修改任何文件，也不得读取构建脚本、规格或上一轮结论。",
        "本次准入身份：",
        json.dumps(identity, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True),
        "只比较以下准入凭证绑定的视觉证据：",
        (
            f"source: {json.dumps(evidence['source']['path'], ensure_ascii=False)} "
            f"sha256={evidence['source']['sha256']}"
        ),
        (
            f"preview: {json.dumps(evidence['preview']['path'], ensure_ascii=False)} "
            f"sha256={evidence['preview']['sha256']}"
        ),
        (
            "side-by-side: "
            f"source={json.dumps(evidence['side_by_side']['source']['path'], ensure_ascii=False)} | "
            f"preview={json.dumps(evidence['side_by_side']['preview']['path'], ensure_ascii=False)}"
        ),
        (
            f"overlay: {json.dumps(evidence['overlay']['path'], ensure_ascii=False)} "
            f"sha256={evidence['overlay']['sha256']}"
        ),
        (
            f"diff: {json.dumps(evidence['diff']['path'], ensure_ascii=False)} "
            f"sha256={evidence['diff']['sha256']}"
        ),
        f"当前 profile: {admission.get('verification_profile')}",
        "profile 所需 region 证据：",
    ]
    if regions:
        for item in regions:
            lines.append(
                f"- {json.dumps(item['region_id'], ensure_ascii=False)}: "
                f"{json.dumps(item['path'], ensure_ascii=False)} sha256={item['sha256']}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "检查画布、对象、文字、表格/矩阵、图形/图表、图片和高风险区域。一次返回全部可见 P0/P1，不得只报告首个问题。",
            "仅返回一个 JSON object，必须精确包含这九个字段（无多余字段）：",
            json.dumps(_PROMPT_FIELDS, ensure_ascii=False),
            "coverage 必须精确包含全部七个键，每个键都必须出现，且无缺失、无多余键：canvas_and_regions、objects_and_geometry、text_and_typography、tables_and_matrices、graphics_connectors_charts、pictures_crop_layers、high_risk_regions；每个值只能是 checked、not_applicable 或 not_reviewable。",
            "decision 只允许 passed、changes_required、not_reviewable。findings 每项必须包含 severity、category、location、source_fact、observed_difference、evidence。",
            "findings[*].evidence 必须是非空 JSON 字符串数组 list[str]，数组每项必须是本 prompt 已列出的当前视觉证据绝对路径。",
            "p2_disclosures 每项必须是 severity=P2 的完整 finding，字段与 findings 相同，evidence 同样为非空 list[str]。",
            "存在 P0/P1 时必须 changes_required；证据缺失、错页或哈希不一致时必须 not_reviewable；passed 时 coverage 不得含 not_reviewable。",
            "中性示例中所有尖括号占位符（包括 evidence 路径）仅表示类型，禁止原样复制到提交 JSON；必须改为本 prompt 已列出的实际值。",
            "中性 JSON 结构示例（仅展示类型，不代表真实页面结论、severity 结论或虚构 hash）：",
            json.dumps(neutral_response, allow_nan=False, ensure_ascii=False),
        ]
    )
    return "\n".join(lines) + "\n"


def _rename_directory_no_replace(
    source: str | Path, destination: str | Path
) -> None:
    """Compatibility wrapper around the shared file/directory primitive."""
    rename_no_replace(Path(source), Path(destination))


def _rollback_published_admission(
    receipt: DirectoryPublicationReceipt,
) -> TombstoneReceipt | None:
    output = receipt.destination
    try:
        return quarantine_publication(
            receipt,
            phase="admission_postcheck",
            fsync_directory=_fsync_directory,
            rename=lambda source, destination: _rename_directory_no_replace(
                source, destination
            ),
        )
    except TransactionFailure as exc:
        raise _error(
            "REVIEW_ADMISSION_NOT_ISSUED", str(output), exc.detail
        ) from exc
    except (FileExistsError, NotImplementedError, OSError, ToolError, ValueError) as exc:
        raise _error(
            "REVIEW_ADMISSION_NOT_ISSUED",
            str(output),
            "cannot quarantine admission during rollback",
        ) from exc


def _publish_admission_directory(
    output_dir: Path, admission: dict[str, Any], prompt: str
) -> DirectoryPublicationReceipt:
    output = Path(output_dir).expanduser()
    parent = output.parent.resolve(strict=False)
    if parent.is_symlink() or not parent.is_dir():
        raise _error(
            "REVIEW_ADMISSION_NOT_ISSUED",
            str(parent),
            "output parent must be an existing real directory",
        )
    output = parent / output.name
    payloads = {
        "review-admission.json": (
            json.dumps(
                admission,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        "reviewer-prompt.txt": prompt.encode("utf-8"),
    }
    try:
        return publish_directory_no_replace(
            output,
            payloads,
            fsync_directory=_fsync_directory,
            rename=lambda source, destination: _rename_directory_no_replace(
                source, destination
            ),
        )
    except TransactionFailure as exc:
        code = (
            "REVIEW_ADMISSION_ALREADY_EXISTS"
            if exc.already_exists
            else "REVIEW_ADMISSION_NOT_ISSUED"
        )
        raise _error(code, str(output), exc.detail) from exc
    except (NotImplementedError, OSError, ToolError, TypeError, ValueError) as exc:
        raise _error(
            "REVIEW_ADMISSION_NOT_ISSUED",
            str(output),
            "cannot atomically publish admission directory",
        ) from exc


def issue_admission(inputs: AdmissionInputs, output_dir: Path) -> dict[str, Any]:
    """Issue one immutable round-1 reviewer admission after all gates pass."""
    try:
        with StableEvidenceView() as view:
            with activate_evidence_view(view):
                admission, _ = _construct_admission(inputs)
                prompt = reviewer_prompt(admission)
                snapshots = list(view.original_snapshots)
                _ensure_unchanged(snapshots)
                receipt = _publish_admission_directory(output_dir, admission, prompt)
                try:
                    _ensure_unchanged(snapshots)
                    verify_directory_receipt(receipt)
                except (ToolError, OSError) as primary_error:
                    try:
                        tombstone = _rollback_published_admission(
                            receipt
                        )
                    except ToolError as rollback_error:
                        raise rollback_error from primary_error
                    detail = (
                        primary_error.detail
                        if isinstance(primary_error, ToolError)
                        else "published admission does not match its receipt"
                    )
                    if tombstone is not None:
                        detail += "; " + tombstone.detail()
                    code = (
                        primary_error.code
                        if isinstance(primary_error, ToolError)
                        else "REVIEW_ADMISSION_NOT_ISSUED"
                    )
                    path = (
                        primary_error.path
                        if isinstance(primary_error, ToolError)
                        else str(receipt.destination)
                    )
                    raise _error(
                        code, path, detail
                    ) from primary_error
                return admission
    except ToolError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, OverflowError) as exc:
        raise _error("REVIEW_ADMISSION_NOT_ISSUED", "inputs", "invalid admission evidence") from exc


def _load_admission_for_invocation_in_view(path: Path) -> _InvocationAdmission:
    try:
        payload, snapshot = _load_json(path, "admission")
    except ToolError as exc:
        raise _error("REVIEW_ADMISSION_STALE", str(path), exc.detail) from exc
    if snapshot.original_path.name != "review-admission.json":
        raise _error("REVIEW_ADMISSION_STALE", str(snapshot.original_path), "admission filename is not fixed")
    expected_fields = (
        _ADMISSION_FIELDS | {"prior_review"}
        if payload.get("review_round") == 2
        else _ADMISSION_FIELDS
    )
    if set(payload) != expected_fields:
        raise _error("REVIEW_ADMISSION_STALE", "admission", "admission fields are not exact")
    if payload.get("schema_version") != 1 or not _is_sha256(payload.get("admission_id")):
        raise _error("REVIEW_ADMISSION_STALE", "admission", "admission identity is malformed")
    if recompute_admission_id(payload) != payload["admission_id"]:
        raise _error("REVIEW_ADMISSION_STALE", "admission.admission_id", "canonical admission ID mismatch")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != _ARTIFACT_FIELDS:
        raise _error("REVIEW_ADMISSION_STALE", "admission.artifacts", "admission artifacts are not exact")
    prior_review = payload.get("prior_review")
    if payload.get("review_round") == 2:
        if not isinstance(prior_review, dict) or set(prior_review) != _PRIOR_REVIEW_FIELDS:
            raise _error(
                "REVIEW_ADMISSION_STALE",
                "admission.prior_review",
                "round-2 prior-review identities are not exact",
            )
        if not all(
            isinstance(prior_review.get(field), dict)
            and set(prior_review[field]) == {"path", "sha256"}
            for field in _PRIOR_REVIEW_FIELDS
        ):
            raise _error(
                "REVIEW_ADMISSION_STALE",
                "admission.prior_review",
                "round-2 prior-review artifact identities are malformed",
            )
    try:
        rebuilt, rebuilt_snapshots = _construct_admission(
            AdmissionInputs(
                spec=Path(artifacts["spec"]["path"]),
                pptx=Path(artifacts["pptx"]["path"]),
                build_report=Path(artifacts["build_report"]["path"]),
                structure_report=Path(artifacts["structure_report"]["path"]),
                render_report=Path(artifacts["render_report"]["path"]),
                text_geometry=Path(artifacts["rendered_text_geometry"]["path"]),
                background_report=Path(artifacts["background_contract"]["path"]),
                visual_diff=Path(artifacts["visual_diff"]["path"]),
                review_round=payload["review_round"],
                prior_admission=(
                    Path(prior_review["admission"]["path"])
                    if isinstance(prior_review, dict)
                    else None
                ),
                prior_invocation=(
                    Path(prior_review["invocation"]["path"])
                    if isinstance(prior_review, dict)
                    else None
                ),
                prior_response_validation=(
                    Path(prior_review["response_validation"]["path"])
                    if isinstance(prior_review, dict)
                    else None
                ),
            )
        )
    except (ToolError, KeyError, TypeError, ValueError) as exc:
        detail = exc.detail if isinstance(exc, ToolError) else "admission artifacts are malformed"
        raise _error("REVIEW_ADMISSION_STALE", "admission.artifacts", detail) from exc
    if rebuilt != payload:
        raise _error("REVIEW_ADMISSION_STALE", "admission", "admission no longer matches current evidence")
    prompt_path = snapshot.original_path.with_name("reviewer-prompt.txt")
    try:
        prompt_bytes, prompt_snapshot = _snapshot_file(prompt_path, "reviewer_prompt")
    except ToolError as exc:
        raise _error("REVIEW_ADMISSION_STALE", str(prompt_path), exc.detail) from exc
    if prompt_bytes != reviewer_prompt(payload).encode("utf-8"):
        raise _error("REVIEW_ADMISSION_STALE", str(prompt_path), "reviewer prompt was changed")
    return _InvocationAdmission(
        payload=payload,
        admission_snapshot=snapshot,
        prompt_snapshot=prompt_snapshot,
        stable_snapshots=tuple([snapshot, prompt_snapshot, *rebuilt_snapshots]),
    )


def _load_admission_for_invocation(path: Path) -> _InvocationAdmission:
    with StableEvidenceView() as view:
        with activate_evidence_view(view):
            validated = _load_admission_for_invocation_in_view(path)
            validated = _InvocationAdmission(
                payload=validated.payload,
                admission_snapshot=validated.admission_snapshot,
                prompt_snapshot=validated.prompt_snapshot,
                stable_snapshots=view.original_snapshots,
            )
            _ensure_unchanged(list(validated.stable_snapshots))
            return validated


def _ensure_invocation_current(snapshots: tuple[_Snapshot, ...]) -> None:
    try:
        _ensure_unchanged(list(snapshots))
    except ToolError as exc:
        raise _error(
            "REVIEW_ADMISSION_STALE",
            exc.path,
            "admission, prompt, or bound evidence changed before invocation",
        ) from exc


def _rollback_owned_invocation(
    destination: Path,
    identity: tuple[int, int],
    receipt: PublicationReceipt | None = None,
) -> TombstoneReceipt | None:
    try:
        return quarantine_publication(
            receipt
            or PublicationReceipt(
                    destination=destination,
                    identity=FileIdentity(*identity),
                    sha256="",
                    byte_count=0,
                    encoded=b"",
                ),
            phase="invocation_postcheck",
            fsync_directory=_fsync_directory,
            rename=lambda source, target: _rename_directory_no_replace(
                source, target
            ),
        )
    except TransactionFailure as exc:
        raise _error(
            "BUILD_OUTPUT_INCOMPLETE", str(destination), exc.detail
        ) from exc
    except (FileExistsError, NotImplementedError, OSError, ToolError, ValueError) as exc:
        raise _error(
            "BUILD_OUTPUT_INCOMPLETE",
            str(destination),
            "cannot quarantine invocation during rollback",
        ) from exc


_DirectoryLock = DirectoryLock


def _scan_invocations(
    invocation_dir: Path, *, admission_id: str, page_id: str, review_round: int
) -> None:
    for path in sorted(invocation_dir.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise _error("BUILD_OUTPUT_INCOMPLETE", str(path), "invocation directory contains an invalid JSON entry")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _error("BUILD_OUTPUT_INCOMPLETE", str(path), "invocation state JSON is unreadable or malformed") from exc
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("admission_id"), str)
            or not isinstance(payload.get("page_id"), str)
            or type(payload.get("review_round")) is not int
        ):
            raise _error("BUILD_OUTPUT_INCOMPLETE", str(path), "invocation state JSON lacks required identity fields")
        if payload["admission_id"] == admission_id or (
            payload["page_id"] == page_id and payload["review_round"] == review_round
        ):
            raise _error("REVIEW_ROUND_ALREADY_INVOKED", str(path), "admission or page round was already invoked")


def record_invocation(
    admission_path: Path, invocation_dir: Path
) -> dict[str, Any]:
    """Atomically consume one admission before the reviewer is launched."""
    validated = _load_admission_for_invocation(admission_path)
    admission = validated.payload
    directory = Path(invocation_dir).expanduser()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        directory = directory.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error("BUILD_OUTPUT_INCOMPLETE", str(directory), "invocation directory is unavailable") from exc
    if directory.is_symlink() or not directory.is_dir():
        raise _error("BUILD_OUTPUT_INCOMPLETE", str(directory), "invocation directory must be a real directory")
    payload = {
        "schema_version": 1,
        "admission_sha256": validated.admission_snapshot.sha256,
        "admission_id": admission["admission_id"],
        "page_id": admission["page_id"],
        "review_round": admission["review_round"],
        "prompt_sha256": validated.prompt_snapshot.sha256,
    }
    destination = directory / (
        f"{admission['page_id']}-round-{admission['review_round']}-invocation.json"
    )
    try:
        with _DirectoryLock(directory):
            _scan_invocations(
                directory,
                admission_id=admission["admission_id"],
                page_id=admission["page_id"],
                review_round=admission["review_round"],
            )
            _ensure_invocation_current(validated.stable_snapshots)
            try:
                receipt = publish_json_no_overwrite(destination, payload)
            except ToolError as exc:
                if isinstance(exc.__cause__, FileExistsError) and (
                    destination.exists() or destination.is_symlink()
                ):
                    raise _error(
                        "REVIEW_ROUND_ALREADY_INVOKED",
                        str(destination),
                        "invocation already exists",
                    ) from exc
                raise
            if not isinstance(receipt, PublicationReceipt):
                raise _error(
                    "BUILD_OUTPUT_INCOMPLETE",
                    str(destination),
                    "publisher did not return an ownership receipt",
                )
            try:
                verify_file_receipt(receipt)
                _ensure_invocation_current(validated.stable_snapshots)
                verify_file_receipt(receipt)
            except (ToolError, OSError) as primary_error:
                try:
                    tombstone = _rollback_owned_invocation(
                        destination,
                        receipt.identity.as_tuple(),
                        receipt,
                    )
                except ToolError as rollback_error:
                    raise rollback_error from primary_error
                detail = (
                    primary_error.detail
                    if isinstance(primary_error, ToolError)
                    else "published invocation does not match its receipt"
                )
                if tombstone is not None:
                    detail += "; " + tombstone.detail()
                code = (
                    primary_error.code
                    if isinstance(primary_error, ToolError)
                    else "BUILD_OUTPUT_INCOMPLETE"
                )
                path = (
                    primary_error.path
                    if isinstance(primary_error, ToolError)
                    else str(destination)
                )
                raise _error(code, path, detail) from primary_error
    except OSError as exc:
        raise _error("BUILD_OUTPUT_INCOMPLETE", str(destination), "cannot lock invocation directory") from exc
    return payload


def _response_issue(code: str, path: str, detail: str) -> dict[str, str]:
    return {"code": code, "path": path, "detail": detail}


def _validate_response_payload(
    response: Any, admission: dict[str, Any]
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(response, dict):
        return [
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response",
                "review response must be a JSON object",
            )
        ]
    if set(response) != set(_PROMPT_FIELDS):
        errors.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response",
                "review response fields must exactly match the nine-field contract",
            )
        )

    identity_fields = (
        "admission_id",
        "review_round",
        "source_sha256",
        "preview_sha256",
    )
    if response.get("page_id") != admission.get("page_id"):
        errors.append(
            _response_issue(
                "REVIEW_ADMISSION_PAGE_MISMATCH",
                "response.page_id",
                "review response page ID does not match the admission",
            )
        )
    for field in identity_fields:
        if response.get(field) != admission.get(field):
            errors.append(
                _response_issue(
                    "REVIEW_RESPONSE_INVALID",
                    f"response.{field}",
                    f"review response {field} does not match the admission",
                )
            )

    decision = response.get("decision")
    if not isinstance(decision, str) or decision not in _DECISIONS:
        errors.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.decision",
                "decision must be passed, changes_required, or not_reviewable",
            )
        )

    coverage = response.get("coverage")
    coverage_valid = (
        isinstance(coverage, dict)
        and set(coverage) == _COVERAGE_FIELDS
        and all(
            isinstance(value, str) and value in _COVERAGE_VALUES
            for value in coverage.values()
        )
    )
    if not coverage_valid:
        errors.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.coverage",
                "coverage must contain exactly seven keys with allowed values",
            )
        )

    findings = response.get("findings")
    findings_valid = isinstance(findings, list)
    blocking = False
    if not findings_valid:
        errors.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.findings",
                "findings must be an array",
            )
        )
    else:
        for index, finding in enumerate(findings):
            if not valid_visual_review_finding(
                finding, evidence_mode="string_list"
            ):
                errors.append(
                    _response_issue(
                        "REVIEW_RESPONSE_INVALID",
                        f"response.findings[{index}]",
                        "finding fields must be typed, non-empty, and complete",
                    )
                )
                continue
            severity = finding.get("severity")
            assert isinstance(severity, str)
            if severity in {"P0", "P1"}:
                blocking = True
    if blocking and decision != "changes_required":
        errors.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.decision",
                "P0/P1 findings require changes_required",
            )
        )
    if findings_valid and not blocking and decision == "changes_required":
        errors.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.decision",
                "changes_required requires at least one P0/P1 finding",
            )
        )
    if (
        decision == "passed"
        and isinstance(coverage, dict)
        and "not_reviewable" in coverage.values()
    ):
        errors.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.coverage",
                "passed responses cannot contain not_reviewable coverage",
            )
        )
    if not valid_response_p2_disclosures(response.get("p2_disclosures")):
        errors.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "response.p2_disclosures",
                "p2_disclosures must contain only complete P2 findings",
            )
        )
    return errors


def _load_invocation_for_response(
    path: Path,
    admission: _InvocationAdmission,
) -> tuple[dict[str, Any] | None, _Snapshot | None, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    try:
        payload, snapshot = _load_json(path, "invocation")
    except ToolError as exc:
        errors.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                str(path),
                f"review invocation is missing or invalid: {exc.detail}",
            )
        )
        return None, None, errors
    expected_filename = (
        f"{admission.payload['page_id']}-round-"
        f"{admission.payload['review_round']}-invocation.json"
    )
    if snapshot.original_path.name != expected_filename:
        errors.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                str(snapshot.original_path),
                "review invocation filename does not match the admitted page round",
            )
        )
    expected = {
        "schema_version": 1,
        "admission_sha256": admission.admission_snapshot.sha256,
        "admission_id": admission.payload["admission_id"],
        "page_id": admission.payload["page_id"],
        "review_round": admission.payload["review_round"],
        "prompt_sha256": admission.prompt_snapshot.sha256,
    }
    if set(payload) != _INVOCATION_FIELDS or any(
        payload.get(field) != value for field, value in expected.items()
    ):
        errors.append(
            _response_issue(
                "REVIEW_RESPONSE_INVALID",
                "invocation",
                "review invocation does not bind the supplied admission",
            )
        )
    return payload, snapshot, errors


def _round_two_error(path: str, detail: str) -> ToolError:
    return _error("REVIEW_ROUND_NOT_ADMITTED", path, detail)


def _normalized_evidence_map(value: Any) -> dict[str, str]:
    evidence: dict[str, str] = {}

    def collect(item: Any) -> None:
        if isinstance(item, dict):
            path = item.get("path")
            sha256 = item.get("sha256")
            if (
                isinstance(path, str)
                and Path(path).is_absolute()
                and _is_sha256(sha256)
            ):
                try:
                    Path(path)
                except (OSError, RuntimeError, ValueError):
                    exact_path = ""
                else:
                    exact_path = path
                if exact_path:
                    previous = evidence.get(exact_path)
                    if previous is not None and previous != sha256:
                        raise _round_two_error(
                            "visual_evidence",
                            "current visual evidence has conflicting hashes",
                        )
                    evidence[exact_path] = sha256
            for nested in item.values():
                collect(nested)
        elif isinstance(item, list):
            for nested in item:
                collect(nested)

    collect(value)
    return evidence


def _evidence_is_current(value: Any, evidence: dict[str, str]) -> bool:
    if not isinstance(value, str) or not Path(value).is_absolute():
        return False
    expected_hash = evidence.get(value)
    if expected_hash is None:
        return False
    try:
        _raw, snapshot = _snapshot_file(value, "round_two.evidence")
    except ToolError:
        return False
    return snapshot.sha256 == expected_hash


def _validate_round_two_closure(
    *,
    current_spec: dict[str, Any],
    current_visual_evidence: dict[str, Any],
    prior_admission_id: str,
    prior_response: dict[str, Any],
) -> None:
    modules = current_spec.get("modules")
    high_risk = modules.get("high_risk") if isinstance(modules, dict) else None
    items = high_risk.get("items") if isinstance(high_risk, dict) else None
    if not isinstance(items, list):
        raise _round_two_error(
            "spec.modules.high_risk.items",
            "round 2 requires current high-risk closure items",
        )
    evidence_map = _normalized_evidence_map(current_visual_evidence)
    findings = prior_response.get("findings")
    if not isinstance(findings, list):
        raise _round_two_error(
            "prior_response.findings", "prior blocking findings are unavailable"
        )
    typography_regions = {"dense_text", "numbers_and_units", "wrap_sensitive"}
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict) or finding.get("severity") not in {"P0", "P1"}:
            continue
        source = f"reviewer:{prior_admission_id}:finding:{index}"
        matches = [
            item
            for item in items
            if isinstance(item, dict) and item.get("source") == source
        ]
        if len(matches) != 1:
            raise _round_two_error(
                f"spec.modules.high_risk.items[{index}]",
                "each prior P0/P1 finding requires exactly one mapped item",
            )
        item = matches[0]
        item_evidence = item.get("evidence")
        if (
            item.get("severity") != finding.get("severity")
            or item.get("category") != finding.get("category")
            or item.get("result") != "passed"
            or not isinstance(item_evidence, list)
            or not item_evidence
            or not all(
                _evidence_is_current(value, evidence_map)
                for value in item_evidence
            )
        ):
            raise _round_two_error(
                f"spec.modules.high_risk.items[{index}]",
                "mapped high-risk item does not prove current passed closure",
            )
        if finding.get("category") == "text_and_typography":
            verification = item.get("verification")
            if not isinstance(verification, dict) or set(verification) != typography_regions:
                raise _round_two_error(
                    f"spec.modules.high_risk.items[{index}].verification",
                    "typography closure requires exactly three global regions",
                )
            for region in typography_regions:
                result = verification.get(region)
                if (
                    not isinstance(result, dict)
                    or set(result) != {"status", "path", "sha256"}
                    or result.get("status") != "passed"
                    or not _evidence_is_current(result.get("path"), evidence_map)
                ):
                    raise _round_two_error(
                        f"spec.modules.high_risk.items[{index}].verification.{region}",
                        "typography region must pass against current bound evidence",
                    )
                if result.get("sha256") != evidence_map.get(result["path"]):
                    raise _round_two_error(
                        f"spec.modules.high_risk.items[{index}].verification.{region}.sha256",
                        "typography region evidence hash is stale",
                    )


def _validate_round_two_prior(
    inputs: AdmissionInputs,
    *,
    current_spec: dict[str, Any],
    current_visual_evidence: dict[str, Any],
    current_pptx_sha256: str,
    current_preview_sha256: str,
) -> dict[str, Any]:
    assert inputs.prior_admission is not None
    assert inputs.prior_invocation is not None
    assert inputs.prior_response_validation is not None
    try:
        prior = _load_admission_for_invocation_in_view(inputs.prior_admission)
        if prior.payload.get("review_round") != 1:
            raise _round_two_error(
                "prior_admission.review_round",
                "round 2 requires a round-1 prior admission",
            )
        current_page_id = current_spec.get("page_id")
        if prior.payload.get("page_id") != current_page_id:
            raise _round_two_error(
                "prior_admission.page_id",
                "round 2 prior admission must belong to the current page",
            )
        if prior.payload.get("pptx_sha256") == current_pptx_sha256:
            raise _round_two_error(
                "current_candidate.pptx_sha256",
                "round 2 requires a new PPTX candidate",
            )
        if prior.payload.get("preview_sha256") == current_preview_sha256:
            raise _round_two_error(
                "current_candidate.preview_sha256",
                "round 2 requires a new rendered preview",
            )
        _invocation, invocation_snapshot, invocation_errors = (
            _load_invocation_for_response(inputs.prior_invocation, prior)
        )
        if invocation_snapshot is None or invocation_errors:
            raise _round_two_error(
                "prior_invocation", "prior invocation does not bind the admission"
            )
        validation, validation_snapshot = _load_json(
            inputs.prior_response_validation, "prior_response_validation"
        )
        if (
            set(validation) != _RESPONSE_VALIDATION_FIELDS
            or validation.get("schema_version") != 1
            or validation.get("valid") is not True
            or validation.get("errors") != []
            or validation.get("page_id") != current_page_id
        ):
            raise _round_two_error(
                "prior_response_validation",
                "prior response validation must be an exact valid report",
            )
        response_path = validation.get("response_path")
        if not isinstance(response_path, str):
            raise _round_two_error(
                "prior_response_validation.response_path",
                "prior raw response path is missing",
            )
        response, response_snapshot = _load_json(response_path, "prior_response")
        expected = {
            "admission_id": prior.payload["admission_id"],
            "page_id": prior.payload["page_id"],
            "review_round": prior.payload["review_round"],
            "admission_path": str(prior.admission_snapshot.original_path),
            "admission_sha256": prior.admission_snapshot.sha256,
            "invocation_path": str(invocation_snapshot.original_path),
            "invocation_sha256": invocation_snapshot.sha256,
            "response_path": str(response_snapshot.original_path),
            "response_sha256": response_snapshot.sha256,
        }
        if any(validation.get(field) != value for field, value in expected.items()):
            raise _round_two_error(
                "prior_response_validation",
                "prior validation identities do not match current immutable files",
            )
        if _validate_response_payload(response, prior.payload):
            raise _round_two_error(
                "prior_response", "prior reviewer response is no longer valid"
            )
        if response.get("decision") != "changes_required":
            raise _round_two_error(
                "prior_response.decision",
                "round 2 requires a changes_required prior response",
            )
        _validate_round_two_closure(
            current_spec=current_spec,
            current_visual_evidence=current_visual_evidence,
            prior_admission_id=prior.payload["admission_id"],
            prior_response=response,
        )
        return {
            "admission": _identity(prior.admission_snapshot),
            "invocation": _identity(invocation_snapshot),
            "response": _identity(response_snapshot),
            "response_validation": _identity(validation_snapshot),
        }
    except ToolError as exc:
        if exc.code == "REVIEW_ROUND_NOT_ADMITTED":
            raise
        raise _round_two_error(
            exc.path, f"prior-review chain is invalid: {exc.detail}"
        ) from exc


def _rollback_response_validation(
    receipt: PublicationReceipt,
) -> TombstoneReceipt | None:
    try:
        return quarantine_publication(
            receipt,
            phase="response_validation_postcheck",
            fsync_directory=_fsync_directory,
        )
    except TransactionFailure as exc:
        raise _error(
            "BUILD_OUTPUT_INCOMPLETE", str(receipt.destination), exc.detail
        ) from exc
    except (FileExistsError, NotImplementedError, OSError, ToolError, ValueError) as exc:
        raise _error(
            "BUILD_OUTPUT_INCOMPLETE",
            str(receipt.destination),
            "cannot quarantine response validation during rollback",
        ) from exc


def _audit_path(path: Path, snapshot: _Snapshot | None) -> str:
    if snapshot is not None:
        return str(snapshot.original_path)
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return str(path)


def _ensure_missing_inputs_still_missing(paths: list[Path]) -> None:
    for path in paths:
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _error(
                "BUILD_OUTPUT_INCOMPLETE",
                str(path),
                "cannot recheck missing response-validation input",
            ) from exc
        raise _error(
            "BUILD_OUTPUT_INCOMPLETE",
            str(path),
            "missing response-validation input appeared during validation",
        )


def validate_response(
    admission_path: Path,
    invocation_path: Path,
    response_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Validate one immutable reviewer response and publish its bound report."""
    with StableEvidenceView() as view:
        with activate_evidence_view(view):
            missing_paths: list[Path] = []
            snapshots_by_name: dict[str, _Snapshot | None] = {}
            raw_by_name: dict[str, bytes | None] = {}
            capture_errors: list[dict[str, str]] = []
            for name, path, code in (
                ("admission", admission_path, "REVIEW_ADMISSION_STALE"),
                ("invocation", invocation_path, "REVIEW_RESPONSE_INVALID"),
                ("response", response_path, "REVIEW_RESPONSE_INVALID"),
            ):
                try:
                    raw, snapshot = _snapshot_file(path, name)
                except ToolError as exc:
                    raw = None
                    snapshot = None
                    missing_paths.append(Path(path))
                    capture_errors.append(
                        _response_issue(
                            code,
                            _audit_path(Path(path), None),
                            f"{name} input is missing or unstable: {exc.detail}",
                        )
                    )
                raw_by_name[name] = raw
                snapshots_by_name[name] = snapshot

            response: Any = None
            response_errors = list(capture_errors)
            response_raw = raw_by_name["response"]
            if response_raw is not None:
                try:
                    response = json.loads(
                        response_raw.decode("utf-8"),
                        parse_constant=reject_nonstandard_json_number,
                    )
                    canonical_json_sha256(response)
                except (
                    TypeError,
                    UnicodeError,
                    ValueError,
                    json.JSONDecodeError,
                    OverflowError,
                    RecursionError,
                ):
                    response_errors.append(
                        _response_issue(
                            "REVIEW_RESPONSE_INVALID",
                            "response",
                            "review response must be finite canonical UTF-8 JSON",
                        )
                    )

            admission: _InvocationAdmission | None = None
            try:
                admission = _load_admission_for_invocation_in_view(admission_path)
            except ToolError as exc:
                if not any(
                    item["code"] == "REVIEW_ADMISSION_STALE"
                    for item in response_errors
                ):
                    response_errors.append(
                        _response_issue(
                            "REVIEW_ADMISSION_STALE",
                            _audit_path(
                                Path(admission_path), snapshots_by_name["admission"]
                            ),
                            exc.detail,
                        )
                    )

            invocation_snapshot = snapshots_by_name["invocation"]
            invocation_errors: list[dict[str, str]] = []
            if admission is not None:
                _invocation, validated_invocation_snapshot, invocation_errors = (
                    _load_invocation_for_response(invocation_path, admission)
                )
                if validated_invocation_snapshot is not None:
                    invocation_snapshot = validated_invocation_snapshot
                payload_errors = _validate_response_payload(
                    response, admission.payload
                )
            else:
                payload_errors = []
            errors = [*response_errors, *payload_errors, *invocation_errors]
            admission_snapshot = (
                admission.admission_snapshot
                if admission is not None
                else snapshots_by_name["admission"]
            )
            response_snapshot = snapshots_by_name["response"]
            report = {
                "schema_version": 1,
                "admission_id": (
                    admission.payload["admission_id"] if admission is not None else None
                ),
                "page_id": admission.payload["page_id"] if admission is not None else None,
                "review_round": (
                    admission.payload["review_round"] if admission is not None else None
                ),
                "admission_path": _audit_path(Path(admission_path), admission_snapshot),
                "admission_sha256": (
                    admission_snapshot.sha256 if admission_snapshot is not None else None
                ),
                "invocation_path": _audit_path(
                    Path(invocation_path), invocation_snapshot
                ),
                "invocation_sha256": (
                    invocation_snapshot.sha256
                    if invocation_snapshot is not None
                    else None
                ),
                "response_path": _audit_path(Path(response_path), response_snapshot),
                "response_sha256": (
                    response_snapshot.sha256 if response_snapshot is not None else None
                ),
                "valid": not errors,
                "errors": errors,
            }
            snapshots = list(view.original_snapshots)
            _ensure_unchanged(snapshots)
            _ensure_missing_inputs_still_missing(missing_paths)
            receipt = publish_json_no_overwrite(output_path, report)
            if not isinstance(receipt, PublicationReceipt):
                raise _error(
                    "BUILD_OUTPUT_INCOMPLETE",
                    str(output_path),
                    "publisher did not return an ownership receipt",
                )
            try:
                _ensure_unchanged(snapshots)
                _ensure_missing_inputs_still_missing(missing_paths)
                verify_file_receipt(receipt)
                _ensure_unchanged(snapshots)
                _ensure_missing_inputs_still_missing(missing_paths)
                verify_file_receipt(receipt)
            except (ToolError, OSError) as primary_error:
                try:
                    tombstone = _rollback_response_validation(receipt)
                except ToolError as rollback_error:
                    raise rollback_error from primary_error
                detail = "response validation transaction changed after publication"
                if tombstone is not None:
                    detail += "; " + tombstone.detail()
                raise _error(
                    "BUILD_OUTPUT_INCOMPLETE", str(receipt.destination), detail
                ) from primary_error
            return report
