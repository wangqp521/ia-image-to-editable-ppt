"""Read-only collection and cross-binding of current reconstruction artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_identity import EvidenceSnapshot, ensure_unchanged, is_sha256, snapshot_file
from .hashing import canonical_json_sha256
from .reviewer_contracts import (
    REVIEW_CONTEXT_ARTIFACT_FIELDS,
    VISUAL_REVIEW_COVERAGE_FIELDS,
    build_review_context,
)
from .spec_identity import content_spec_sha256, input_spec_sha256


@dataclass(frozen=True)
class CurrentArtifacts:
    identities: dict[str, dict[str, str]]
    region_evidence: tuple[dict[str, Any], ...]
    required_coverage: frozenset[str]
    allowed_evidence: frozenset[str]


class _InvalidIdentity(Exception):
    def __init__(self, path: str, detail: str, code: str = "FINAL_IDENTITY_INVALID") -> None:
        super().__init__(detail)
        self.code = code
        self.path = path
        self.detail = detail

    def issue(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


def _expect(condition: bool, path: str, detail: str) -> None:
    if not condition:
        raise _InvalidIdentity(path, detail)


def _record(
    value: Any,
    label: str,
    snapshots: list[EvidenceSnapshot],
    *,
    expected_path: str | None = None,
) -> tuple[bytes, EvidenceSnapshot, dict[str, str]]:
    _expect(isinstance(value, dict), label, "file identity must be an object")
    path = value.get("path")
    digest = value.get("sha256")
    _expect(isinstance(path, str) and Path(path).is_absolute(), f"{label}.path", "absolute path is required")
    _expect(is_sha256(digest), f"{label}.sha256", "lowercase SHA-256 is required")
    try:
        raw, snapshot = snapshot_file(path, f"{label}.path")
    except Exception as exc:
        raise _InvalidIdentity(f"{label}.path", "required artifact is missing or unstable") from exc
    _expect(snapshot.sha256 == digest, f"{label}.sha256", "reported file hash is stale")
    if expected_path is not None:
        try:
            expected = str(Path(expected_path).resolve(strict=True))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _InvalidIdentity(f"{label}.path", "expected artifact path is invalid") from exc
        _expect(str(snapshot.original_path) == expected, f"{label}.path", "reported path is stale")
    snapshots.append(snapshot)
    return raw, snapshot, {"path": str(snapshot.original_path), "sha256": snapshot.sha256}


def _json_record(
    value: Any,
    label: str,
    snapshots: list[EvidenceSnapshot],
    *,
    expected_path: str | None = None,
) -> tuple[dict[str, Any], EvidenceSnapshot, dict[str, str]]:
    raw, snapshot, identity = _record(value, label, snapshots, expected_path=expected_path)
    try:
        payload = json.loads(raw.decode("utf-8"))
        _expect(isinstance(payload, dict), label, "JSON root must be an object")
        canonical_json_sha256(payload)
    except _InvalidIdentity:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _InvalidIdentity(label, "artifact must contain finite UTF-8 JSON") from exc
    return payload, snapshot, identity


def _dict(value: Any, path: str) -> dict[str, Any]:
    _expect(isinstance(value, dict), path, "object is required")
    return value


def _same_identity(value: Any, identity: dict[str, str], path: str) -> None:
    _expect(isinstance(value, dict), path, "file identity is required")
    _expect(value.get("path") == identity["path"], f"{path}.path", "path is stale")
    _expect(value.get("sha256") == identity["sha256"], f"{path}.sha256", "hash is stale")


def _same_page_size(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
        and all(abs(float(actual) - expected) <= 1.0 for actual, expected in zip(value, (960.0, 540.0)))
    )


def _tool_identity(
    value: Any, label: str, snapshots: list[EvidenceSnapshot]
) -> dict[str, Any]:
    tool = _dict(value, label)
    _record({"path": tool.get("path"), "sha256": tool.get("sha256")}, label, snapshots)
    _expect(isinstance(tool.get("version"), str) and bool(tool["version"]), f"{label}.version", "tool version is required")
    return tool


def _required_coverage(spec: dict[str, Any], profile: str) -> frozenset[str]:
    required = {"canvas_and_regions", "objects_and_geometry"}
    elements = spec.get("elements")
    kinds = {
        item.get("kind")
        for item in (elements if isinstance(elements, list) else [])
        if isinstance(item, dict)
    }
    activated = set(spec.get("activated_modules", [])) if isinstance(spec.get("activated_modules"), list) else set()
    if kinds & {"text", "special_text"} or activated & {"typography", "special_text"}:
        required.add("text_and_typography")
    if kinds & {"table", "matrix"}:
        required.add("tables_and_matrices")
    if kinds & {"shape", "line", "status", "diagram", "chart"} or activated & {"graphics", "diagram", "chart"}:
        required.add("graphics_connectors_charts")
    if kinds & {"icon", "picture"} or activated & {"icons", "picture_framing"}:
        required.add("pictures_crop_layers")
    modules = spec.get("modules")
    high_risk = modules.get("high_risk") if isinstance(modules, dict) else None
    if "high_risk" in activated and isinstance(high_risk, dict) and high_risk.get("items"):
        required.add("high_risk_regions")
    return frozenset(required)


def collect_current_artifacts(
    spec: Any,
) -> tuple[CurrentArtifacts | None, list[dict[str, str]]]:
    """Hash and cross-check existing artifacts without running any producer."""
    snapshots: list[EvidenceSnapshot] = []
    try:
        _expect(isinstance(spec, dict), "spec", "spec must be a JSON object")
        profile = spec.get("verification_profile")
        _expect(profile in {"rapid", "reviewed"}, "verification_profile", "rapid or reviewed profile is required")
        page_id = spec.get("page_id")
        _expect(isinstance(page_id, str) and bool(page_id), "page_id", "page ID is required")
        content_hash = content_spec_sha256(spec)

        visual_gate = _dict(spec.get("visual_gate"), "visual_gate")
        editability_gate = _dict(spec.get("editability_gate"), "editability_gate")
        _, pptx, pptx_identity = _record(visual_gate.get("pptx"), "visual_gate.pptx", snapshots)
        _same_identity(editability_gate.get("pptx"), pptx_identity, "editability_gate.pptx")
        _, source, source_identity = _record(spec.get("clean_visual_reference"), "clean_visual_reference", snapshots)
        _, preview, preview_identity = _record(visual_gate.get("preview"), "visual_gate.preview", snapshots)

        runtime, runtime_snapshot, runtime_identity = _json_record(spec.get("runtime_preflight"), "runtime_preflight", snapshots)
        render, render_snapshot, render_identity = _json_record(visual_gate.get("render_report"), "visual_gate.render_report", snapshots)
        structure, structure_snapshot, structure_identity = _json_record(editability_gate.get("validator"), "editability_gate.validator", snapshots)
        background, background_snapshot, background_identity = _json_record(visual_gate.get("background_contract"), "visual_gate.background_contract", snapshots)
        visual, visual_snapshot, visual_identity = _json_record(visual_gate.get("report"), "visual_gate.report", snapshots)

        build_spec, build_spec_snapshot, build_spec_identity = _json_record(
            editability_gate.get("build_spec_snapshot"),
            "editability_gate.build_spec_snapshot",
            snapshots,
        )
        build, build_snapshot, build_identity = _json_record(
            editability_gate.get("build_report"),
            "editability_gate.build_report",
            snapshots,
        )
        _expect(content_spec_sha256(build_spec) == content_hash, "build_spec_snapshot", "build snapshot content identity is stale")
        build_input_hash = input_spec_sha256(build_spec)
        _expect(build.get("schema_version") == 1 and build.get("valid") is True, "build_report.valid", "build report must pass")
        for field, expected in (
            ("schema_sha256", canonical_json_sha256(build_spec)),
            ("content_spec_sha256", content_hash),
            ("input_spec_sha256", build_input_hash),
            ("pptx_sha256", pptx.sha256),
        ):
            _expect(build.get(field) == expected, f"build_report.{field}", "build report identity is stale")
        for field in ("compiler_sha256", "capability_manifest_sha256"):
            _expect(is_sha256(build.get(field)), f"build_report.{field}", "build capability identity is required")
        _expect(build.get("unsupported") == [], "build_report.unsupported", "unsupported build output blocks review")

        _expect(runtime.get("valid") is True and runtime.get("errors") == [], "runtime_preflight.valid", "runtime preflight must pass")
        _expect(runtime.get("renderer_backend") == "libreoffice", "runtime_preflight.renderer_backend", "LibreOffice runtime is required")
        executables = _dict(runtime.get("executables"), "runtime_preflight.executables")
        tools = {name: _tool_identity(executables.get(name), f"runtime_preflight.executables.{name}", snapshots) for name in ("soffice", "pdftoppm", "pdffonts", "pdftotext")}
        fontconfig = _dict(runtime.get("fontconfig"), "runtime_preflight.fontconfig")
        _record(fontconfig, "runtime_preflight.fontconfig", snapshots)

        _expect(render.get("schema_version") == 1, "render_report.schema_version", "render schema version must be 1")
        _same_identity(render.get("pptx"), pptx_identity, "render_report.pptx")
        renderer = _dict(render.get("renderer"), "render_report.renderer")
        expected_renderer = {
            "backend": "libreoffice",
            "path": tools["soffice"]["path"],
            "version": tools["soffice"]["version"],
            "executable_sha256": tools["soffice"]["sha256"],
            "fontconfig_path": fontconfig["path"],
            "fontconfig_sha256": fontconfig["sha256"],
        }
        for field, expected in expected_renderer.items():
            _expect(renderer.get(field) == expected, f"render_report.renderer.{field}", "renderer identity is stale")
        _expect(renderer.get("isolated_profile") is True, "render_report.renderer.isolated_profile", "isolated renderer profile is required")
        _, pdf, pdf_identity = _record(render.get("pdf"), "render_report.pdf", snapshots)
        _expect(render["pdf"].get("pages") == 1 and _same_page_size(render["pdf"].get("page_size_pt")), "render_report.pdf", "single 960x540 pt page is required")
        _same_identity(render.get("preview"), preview_identity, "render_report.preview")
        _expect(render["preview"].get("size") == [1920, 1080], "render_report.preview.size", "1920x1080 preview is required")
        font_payload, _, _ = _json_record(render.get("font_report"), "render_report.font_report", snapshots)
        raw_path = render["font_report"].get("raw_path")
        raw_bytes, _, _ = _record({"path": raw_path, "sha256": render["font_report"].get("raw_sha256")}, "render_report.font_report.raw", snapshots)
        try:
            raw_fonts = raw_bytes.decode("utf-8")
        except UnicodeError as exc:
            raise _InvalidIdentity("render_report.font_report.raw", "raw font evidence must be UTF-8") from exc
        resolved_fonts = render["font_report"].get("resolved_fonts")
        _expect(font_payload == {"resolved_fonts": resolved_fonts}, "render_report.font_report", "font report payload is stale")
        _expect(isinstance(resolved_fonts, list) and all(isinstance(font, str) and font in raw_fonts for font in resolved_fonts), "render_report.font_report.resolved_fonts", "resolved fonts do not match raw pdffonts evidence")
        for label, runtime_name in (("rasterizer", "pdftoppm"), ("text_extractor", "pdftotext")):
            value = _dict(render.get(label), f"render_report.{label}")
            for field, expected in (("path", tools[runtime_name]["path"]), ("version", tools[runtime_name]["version"]), ("executable_sha256", tools[runtime_name]["sha256"])):
                _expect(value.get(field) == expected, f"render_report.{label}.{field}", "render tool identity is stale")

        _expect(structure.get("valid") is True and structure.get("errors") == [], "structure_validation.valid", "structure validation must pass")
        _expect(structure.get("pptx_sha256") == pptx.sha256, "structure_validation.pptx_sha256", "structure PPTX identity is stale")
        _expect(structure.get("path") == pptx_identity["path"], "structure_validation.path", "structure report points to another PPTX")
        _expect(structure.get("slide_count") == 1, "structure_validation.slide_count", "exactly one slide is required")

        background_expected = {
            "schema_version": 1,
            "page_id": page_id,
            "spec_sha256": content_hash,
            "input_spec_sha256": build_input_hash,
            "pptx_sha256": pptx.sha256,
            "build_report_sha256": canonical_json_sha256(build),
            "build_report_file_sha256": build_snapshot.sha256,
            "structure_report_sha256": canonical_json_sha256(structure),
            "structure_report_file_sha256": structure_snapshot.sha256,
        }
        for field, expected in background_expected.items():
            _expect(background.get(field) == expected, f"background_contract.{field}", "background identity is stale")
        _expect(background.get("valid") is True and background.get("errors") == [], "background_contract.valid", "background contract must pass")
        _expect(isinstance(background.get("items"), list) and bool(background["items"]) and all(isinstance(item, dict) and item.get("valid") is True for item in background["items"]), "background_contract.items", "all background items must pass")

        _expect(visual.get("verification_profile") == profile, "visual_diff.verification_profile", "visual profile is stale")
        _same_identity(visual.get("reference"), source_identity, "visual_diff.reference")
        _same_identity(visual.get("preview"), preview_identity, "visual_diff.preview")
        _expect(visual.get("pptx_sha256") == pptx.sha256, "visual_diff.pptx_sha256", "visual PPTX identity is stale")
        _expect(visual.get("pdf_sha256") == pdf.sha256, "visual_diff.pdf_sha256", "visual PDF identity is stale")
        _same_identity(visual.get("render_report"), render_identity, "visual_diff.render_report")
        _expect(visual.get("renderer") == renderer, "visual_diff.renderer", "visual renderer identity is stale")
        _expect(visual.get("report") == visual_identity["path"], "visual_diff.report", "visual report path is stale")
        presence = _dict(visual.get("region_presence"), "visual_diff.region_presence")
        _expect(presence.get("status") == "passed" and presence.get("missing") == [], "visual_diff.region_presence", "region presence must pass")
        tripwire = _dict(visual.get("tripwire"), "visual_diff.tripwire")
        _expect((tripwire.get("available") is False and tripwire.get("triggered") is None) or (tripwire.get("available") is True and tripwire.get("triggered") is False), "visual_diff.tripwire", "tripwire blocks review")
        evidence = _dict(visual.get("evidence"), "visual_diff.evidence")
        _, _, overlay_identity = _record(evidence.get("overlay"), "visual_diff.evidence.overlay", snapshots)
        _, _, diff_identity = _record(evidence.get("diff"), "visual_diff.evidence.diff", snapshots)
        _expect(visual.get("overlay") == overlay_identity["path"], "visual_diff.overlay", "overlay path is stale")
        _expect(visual.get("diff") == diff_identity["path"], "visual_diff.diff", "diff path is stale")
        regions = visual.get("regions")
        summary = _dict(visual.get("region_summary"), "visual_diff.region_summary")
        _expect(isinstance(regions, list), "visual_diff.regions", "region evidence must be an array")
        _expect(summary.get("skipped") == 0 and summary.get("requested") == summary.get("generated") == len(regions) and visual.get("skipped_regions") == [], "visual_diff.region_summary", "all requested regions must be generated")
        spec_regions = {item.get("region_id"): item for item in spec.get("regions", []) if isinstance(item, dict) and isinstance(item.get("region_id"), str)}
        normalized_regions = []
        seen = set()
        for index, region in enumerate(regions):
            label = f"visual_diff.regions[{index}]"
            _expect(isinstance(region, dict), label, "region evidence must be an object")
            region_id = region.get("region_id")
            _expect(region_id in spec_regions and region_id not in seen, f"{label}.region_id", "region ID is stale or duplicated")
            seen.add(region_id)
            bbox = region.get("source_bbox")
            _expect(bbox == spec_regions[region_id].get("source_bbox"), f"{label}.source_bbox", "region bbox is stale")
            _expect(region.get("scale_percent") == 200, f"{label}.scale_percent", "region evidence must be 200 percent")
            _, _, region_identity = _record({"path": region.get("evidence"), "sha256": region.get("evidence_sha256")}, label, snapshots)
            normalized_regions.append({"region_id": region_id, **region_identity, "bbox": list(bbox), "scale": 2.0})

        ensure_unchanged(snapshots)
        identities = {
            "build_spec_snapshot": build_spec_identity,
            "build_report": build_identity,
            "current_pptx": pptx_identity,
            "source": source_identity,
            "preview": preview_identity,
            "render_report": render_identity,
            "runtime_preflight": runtime_identity,
            "structure_validation": structure_identity,
            "background_contract": background_identity,
            "visual_diff": visual_identity,
        }
        _expect(tuple(identities) == REVIEW_CONTEXT_ARTIFACT_FIELDS, "artifacts", "artifact field order is invalid")
        allowed = {source_identity["path"], preview_identity["path"], overlay_identity["path"], diff_identity["path"]}
        allowed.update(item["path"] for item in normalized_regions)
        return CurrentArtifacts(
            identities=identities,
            region_evidence=tuple(sorted(normalized_regions, key=lambda item: (item["region_id"], item["path"]))),
            required_coverage=_required_coverage(spec, profile),
            allowed_evidence=frozenset(allowed),
        ), []
    except _InvalidIdentity as exc:
        return None, [exc.issue()]
    except Exception as exc:
        return None, [{"code": "FINAL_IDENTITY_INVALID", "path": "artifacts", "detail": f"cannot collect current artifact identities: {exc}"}]


def prepare_review_context(
    spec: Any, review_round: int
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    if isinstance(spec, dict) and spec.get("verification_profile") == "rapid":
        return None, [
            {
                "code": "REVIEWER_NOT_ALLOWED",
                "path": "verification_profile",
                "detail": "rapid uses the primary-agent review and has no reviewer context",
            }
        ]
    artifacts, errors = collect_current_artifacts(spec)
    if artifacts is None:
        return None, errors
    try:
        context = build_review_context(
            page_id=spec["page_id"],
            review_round=review_round,
            verification_profile=spec["verification_profile"],
            content_spec_sha256=content_spec_sha256(spec),
            artifacts=artifacts.identities,
            region_evidence=list(artifacts.region_evidence),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return None, [{"code": "FINAL_IDENTITY_INVALID", "path": "review_context", "detail": str(exc)}]
    return context, []
