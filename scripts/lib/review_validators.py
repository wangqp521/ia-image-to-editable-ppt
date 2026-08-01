"""Production validators for stable reviewer-admission evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import os
import selectors
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .error_codes import ToolError
from .hashing import canonical_json_sha256
from .review_evidence import (
    EvidenceSnapshot as _Snapshot,
    error as _error,
    expect as _expect,
    identity as _identity,
    is_sha256 as _is_sha256,
    load_json as _load_json,
    current_evidence_view as _current_evidence_view,
    recorded_file as _recorded_file,
    snapshot_file as _snapshot_file,
)


_PAGE_SIZE_PT = (960.0, 540.0)
_PREVIEW_SIZE = [1920, 1080]
_PDFFONTS_TIMEOUT_SECONDS = 10.0
_PDFFONTS_LOCKED_DYLIB_TIMEOUT_SECONDS = 30.0
_PDFFONTS_STDOUT_LIMIT_BYTES = 1024 * 1024
_PDFFONTS_STDERR_LIMIT_BYTES = 64 * 1024


def _pdffonts_subprocess_env(library_directory: Path | None = None) -> dict[str, str]:
    env = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    if os.name == "nt" and isinstance(os.environ.get("SYSTEMROOT"), str):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    if library_directory is not None:
        env["DYLD_LIBRARY_PATH"] = str(library_directory)
    return env


def _locked_pdffonts_env(entry: Any) -> dict[str, str]:
    _expect(
        isinstance(entry, dict),
        "runtime_preflight.executables.pdffonts",
        "pdffonts runtime identity is required",
    )
    dependencies = entry.get("dynamic_libraries")
    _expect(
        isinstance(dependencies, list),
        "runtime_preflight.executables.pdffonts.dynamic_libraries",
        "pdffonts dynamic dependency closure is required",
    )
    if not dependencies:
        return _pdffonts_subprocess_env()
    view = _current_evidence_view()
    _expect(
        view is not None,
        "runtime_preflight.executables.pdffonts.dynamic_libraries",
        "locked dependencies require a stable evidence view",
    )
    aliases: dict[str, Path] = {}
    for index, dependency in enumerate(dependencies):
        label = (
            "runtime_preflight.executables.pdffonts."
            f"dynamic_libraries[{index}]"
        )
        _expect(
            isinstance(dependency, dict)
            and set(dependency) == {"path", "sha256", "load_names"},
            label,
            "dynamic dependency identity fields are not exact",
        )
        path_value = dependency.get("path")
        digest = dependency.get("sha256")
        load_names = dependency.get("load_names")
        _expect(
            isinstance(path_value, str)
            and Path(path_value).is_absolute()
            and _is_sha256(digest)
            and isinstance(load_names, list)
            and bool(load_names)
            and len(load_names) == len(set(load_names))
            and all(
                isinstance(name, str)
                and bool(name)
                and name not in {".", ".."}
                and Path(name).name == name
                and "/" not in name
                and "\0" not in name
                for name in load_names
            ),
            label,
            "dynamic dependency identity is malformed",
        )
        raw, snapshot = _snapshot_file(path_value, f"{label}.path")
        _expect(
            snapshot.sha256 == digest
            and hashlib.sha256(raw).hexdigest() == digest,
            f"{label}.sha256",
            "dynamic dependency bytes are stale",
        )
        for name in load_names:
            existing = aliases.setdefault(name, snapshot.path)
            _expect(
                existing == snapshot.path,
                f"{label}.load_names",
                "dynamic dependency alias collides",
            )
    assert view is not None
    directory = view.materialize_alias_directory(
        aliases,
        "runtime_preflight.executables.pdffonts.dynamic_libraries",
    )
    return _pdffonts_subprocess_env(directory)


def _same_page_size(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
        and all(abs(float(actual) - expected) <= 1.0 for actual, expected in zip(value, _PAGE_SIZE_PT))
    )


def _validate_runtime_preflight(
    runtime: dict[str, Any], runtime_snapshot: _Snapshot
) -> None:
    executables = runtime.get("executables")
    fontconfig = runtime.get("fontconfig")
    modules = runtime.get("python_modules")
    _expect(isinstance(executables, dict), "runtime_preflight.executables", "runtime executable identities are required")
    _expect(isinstance(fontconfig, dict), "runtime_preflight.fontconfig", "runtime fontconfig identity is required")
    _expect(isinstance(modules, dict), "runtime_preflight.python_modules", "runtime Python module identities are required")
    for name in ("soffice", "pdftoppm", "pdffonts", "pdftotext"):
        _expect(isinstance(executables.get(name), dict), f"runtime_preflight.executables.{name}", "runtime executable identity is required")
        requested = executables[name].get("requested")
        _expect(isinstance(requested, str) and Path(requested).is_absolute(), f"runtime_preflight.executables.{name}.requested", "production preflight requires an absolute requested tool path")
    try:
        preflight = importlib.import_module("preflight_runtime")
        arguments = argparse.Namespace(
            soffice=executables["soffice"]["requested"],
            pdftoppm=executables["pdftoppm"]["requested"],
            pdffonts=executables["pdffonts"]["requested"],
            pdftotext=executables["pdftotext"]["requested"],
            fontconfig=Path(fontconfig.get("path", "")),
            expected_runtime=runtime_snapshot.path,
            python_module=sorted(modules),
            output=runtime_snapshot.path,
        )
        current = preflight.inspect_runtime(arguments)
    except Exception as exc:
        raise _error(
            "REVIEW_ADMISSION_NOT_ISSUED",
            "runtime_preflight",
            "production runtime preflight could not be repeated",
        ) from exc
    _expect(current.get("valid") is True and current.get("errors") == [], "runtime_preflight.valid", "current production runtime preflight must pass")
    _expect(current == runtime, "runtime_preflight", "runtime artifact is not the current production preflight result")


def _run_pdffonts_bounded(
    executable: Path,
    pdf: Path,
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> bytes:
    command = [str(executable), str(pdf)]
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {
        "stdout": _PDFFONTS_STDOUT_LIMIT_BYTES,
        "stderr": _PDFFONTS_STDERR_LIMIT_BYTES,
    }
    selector: selectors.BaseSelector | None = None
    process: subprocess.Popen[bytes] | None = None
    exceeded: str | None = None
    timed_out = False
    returncode: int | None = None
    deadline = time.monotonic() + (
        _PDFFONTS_TIMEOUT_SECONDS
        if timeout_seconds is None
        else timeout_seconds
    )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or _pdffonts_subprocess_env(),
        )
        if process.stdout is None or process.stderr is None:
            raise OSError("pdffonts pipes are unavailable")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            for key, _events in selector.select(min(remaining, 0.1)):
                stream_name = key.data
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                available = limits[stream_name] - len(buffers[stream_name])
                buffers[stream_name].extend(chunk[: max(0, available)])
                if len(chunk) > available and exceeded is None:
                    exceeded = stream_name
                    process.kill()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            process.kill()
            returncode = process.wait()
        else:
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                returncode = process.wait()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error("REVIEW_ADMISSION_NOT_ISSUED", "render_report.font_report", "cannot run locked pdffonts") from exc
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        if selector is not None:
            selector.close()
    _expect(not timed_out, "render_report.font_report", "locked pdffonts timed out")
    _expect(exceeded is None, f"render_report.font_report.{exceeded or 'output'}", "locked pdffonts output exceeded its byte limit")
    _expect(returncode == 0, "render_report.font_report", "locked pdffonts command failed")
    try:
        bytes(buffers["stdout"]).decode("utf-8", errors="strict")
        bytes(buffers["stderr"]).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _error("REVIEW_ADMISSION_NOT_ISSUED", "render_report.font_report", "locked pdffonts output must be UTF-8") from exc
    return bytes(buffers["stdout"])


def _validate_build_and_structure(
    spec: dict[str, Any],
    build: dict[str, Any],
    structure: dict[str, Any],
    pptx: _Snapshot,
) -> None:
    try:
        validator = importlib.import_module("validate_pptx")
        current = validator.validate_pptx(
            pptx.path,
            expected_slides=1,
            reconstruction_spec=spec,
            build_report=build,
        )
    except Exception as exc:
        raise _error(
            "REVIEW_ADMISSION_NOT_ISSUED",
            "structure_report",
            "production PPTX validation could not be completed",
        ) from exc
    _expect(current.get("valid") is True, "structure_report.valid", "production PPTX validation must pass")
    current["path"] = str(pptx.original_path)
    _expect(structure == current, "structure_report", "structure report is not the current production validation result")


def _validate_render(
    render: dict[str, Any],
    render_snapshot: _Snapshot,
    runtime: dict[str, Any],
    runtime_snapshot: _Snapshot,
    pptx: _Snapshot,
    snapshots: list[_Snapshot],
    *,
    production_runtime: dict[str, Any],
    production_runtime_snapshot: _Snapshot,
) -> tuple[_Snapshot, _Snapshot, _Snapshot]:
    _validate_runtime_preflight(
        production_runtime, production_runtime_snapshot
    )
    try:
        renderer_module = importlib.import_module("render_preview")
        current_runtime = renderer_module._load_runtime(runtime_snapshot.path)
    except Exception as exc:
        raise _error(
            "REVIEW_ADMISSION_NOT_ISSUED",
            "runtime_preflight",
            "render runtime is stale or invalid",
        ) from exc
    _expect(current_runtime == runtime, "runtime_preflight", "runtime preflight changed while validating")
    _expect(render.get("schema_version") == 1, "render_report.schema_version", "expected schema version 1")
    _recorded_file(render.get("pptx"), "render_report.pptx", snapshots, expected_path=pptx.path)
    _expect(render["pptx"].get("sha256") == pptx.sha256, "render_report.pptx.sha256", "rendered PPTX is stale")

    renderer = render.get("renderer")
    _expect(isinstance(renderer, dict), "render_report.renderer", "renderer identity is required")
    _expect(renderer.get("backend") == "libreoffice", "render_report.renderer.backend", "review requires LibreOffice")
    _expect(renderer.get("isolated_profile") is True, "render_report.renderer.isolated_profile", "isolated profile is required")
    for field in ("path", "version", "fontconfig_path"):
        _expect(isinstance(renderer.get(field), str) and bool(renderer[field]), f"render_report.renderer.{field}", "renderer field is required")
    for field in ("executable_sha256", "fontconfig_sha256"):
        _expect(_is_sha256(renderer.get(field)), f"render_report.renderer.{field}", "renderer SHA-256 is required")
    soffice = runtime["executables"]["soffice"]
    fontconfig = runtime["fontconfig"]
    expected_renderer = {
        "backend": runtime["renderer_backend"],
        "path": soffice["path"],
        "version": soffice["version"],
        "executable_sha256": soffice["sha256"],
        "fontconfig_path": fontconfig["path"],
        "fontconfig_sha256": fontconfig["sha256"],
    }
    for field, value in expected_renderer.items():
        _expect(renderer.get(field) == value, f"render_report.renderer.{field}", "renderer identity is stale")
    _expect(type(renderer.get("attempt_count")) is int and renderer["attempt_count"] > 0, "render_report.renderer.attempt_count", "render attempt count must be positive")

    pdf = _recorded_file(render.get("pdf"), "render_report.pdf", snapshots)
    try:
        pages, page_size = renderer_module._inspect_pdf(
            pdf.path, Path(runtime["executables"]["pdftoppm"]["path"])
        )
    except Exception as exc:
        raise _error("REVIEW_ADMISSION_NOT_ISSUED", "render_report.pdf", "rendered PDF is invalid") from exc
    _expect(pages == 1 and render["pdf"].get("pages") == pages, "render_report.pdf.pages", "exactly one PDF page is required")
    _expect(_same_page_size(page_size) and render["pdf"].get("page_size_pt") == page_size, "render_report.pdf.page_size_pt", "PDF must be 960x540 pt")
    preview = _recorded_file(render.get("preview"), "render_report.preview", snapshots, image=True)
    _expect(render["preview"].get("size") == _PREVIEW_SIZE, "render_report.preview.size", "preview must be 1920x1080")
    try:
        with Image.open(preview.path) as image_value:
            _expect(list(image_value.size) == _PREVIEW_SIZE, "render_report.preview.size", "preview pixels do not match reported size")
            grayscale = image_value.convert("L")
            extrema = grayscale.getextrema()
            _expect(extrema is not None and extrema[0] < 245, "render_report.preview", "preview must contain visible non-blank content")
    except (OSError, UnidentifiedImageError) as exc:
        raise _error("REVIEW_ADMISSION_NOT_ISSUED", "render_report.preview", "preview is invalid") from exc
    font_report = _recorded_file(render.get("font_report"), "render_report.font_report", snapshots)
    font_payload, font_snapshot = _load_json(font_report.path, "render_report.font_report")
    _expect(font_snapshot.sha256 == font_report.sha256, "render_report.font_report.sha256", "font report changed while validating")
    raw_path = render["font_report"].get("raw_path")
    raw_hash = render["font_report"].get("raw_sha256")
    _expect(isinstance(raw_path, str) and _is_sha256(raw_hash), "render_report.font_report.raw_path", "raw font evidence is required")
    raw_bytes, raw_snapshot = _snapshot_file(raw_path, "render_report.font_report.raw_path")
    snapshots.append(raw_snapshot)
    _expect(raw_snapshot.sha256 == raw_hash, "render_report.font_report.raw_sha256", "raw font evidence is stale")
    try:
        resolved_fonts = renderer_module._parse_pdffonts(raw_bytes.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise _error("REVIEW_ADMISSION_NOT_ISSUED", "render_report.font_report", "raw font evidence is invalid") from exc
    _expect(font_payload == {"resolved_fonts": resolved_fonts}, "render_report.font_report", "font report does not match raw font evidence")
    _expect(render["font_report"].get("resolved_fonts") == resolved_fonts, "render_report.font_report.resolved_fonts", "font list is stale")
    pdffonts_entry = runtime["executables"]["pdffonts"]
    actual_raw = _run_pdffonts_bounded(
        Path(pdffonts_entry["path"]),
        pdf.path,
        env=_locked_pdffonts_env(pdffonts_entry),
        timeout_seconds=(
            _PDFFONTS_LOCKED_DYLIB_TIMEOUT_SECONDS
            if pdffonts_entry["dynamic_libraries"]
            else _PDFFONTS_TIMEOUT_SECONDS
        ),
    )
    _expect(actual_raw == raw_bytes, "render_report.font_report.raw_sha256", "raw font evidence does not match locked pdffonts on the current PDF")
    actual_fonts = renderer_module._parse_pdffonts(
        actual_raw.decode("utf-8", errors="strict")
    )
    _expect(actual_fonts == resolved_fonts, "render_report.font_report.resolved_fonts", "font list does not match locked pdffonts on the current PDF")

    rasterizer = render.get("rasterizer")
    extractor = render.get("text_extractor")
    _expect(isinstance(rasterizer, dict), "render_report.rasterizer", "rasterizer identity is required")
    _expect(isinstance(extractor, dict), "render_report.text_extractor", "text extractor identity is required")
    _expect(rasterizer.get("output_size") == _PREVIEW_SIZE, "render_report.rasterizer.output_size", "rasterizer size is stale")
    for label, value in (("rasterizer", rasterizer), ("text_extractor", extractor)):
        for field in ("path", "version"):
            _expect(isinstance(value.get(field), str) and bool(value[field]), f"render_report.{label}.{field}", "tool identity is required")
        _expect(_is_sha256(value.get("executable_sha256")), f"render_report.{label}.executable_sha256", "tool SHA-256 is required")
    for label, runtime_name in (("rasterizer", "pdftoppm"), ("text_extractor", "pdftotext")):
        value = render[label]
        executable = runtime["executables"][runtime_name]
        expected_tool = {
            "path": executable["path"],
            "version": executable["version"],
            "executable_sha256": executable["sha256"],
        }
        for field, expected in expected_tool.items():
            _expect(value.get(field) == expected, f"render_report.{label}.{field}", "render tool identity is stale")
    if render_snapshot.path == render_snapshot.original_path:
        _expect(render_snapshot.sha256 == hashlib.sha256(render_snapshot.path.read_bytes()).hexdigest(), "render_report", "render report changed")
    return pdf, preview, font_report


def _validate_text_geometry(
    text: dict[str, Any],
    *,
    page_id: str,
    content_hash: str,
    input_hash: str,
    source_hash: str,
    spec: _Snapshot,
    pptx: _Snapshot,
    build: _Snapshot,
    render: _Snapshot,
    runtime: _Snapshot,
    pdf: _Snapshot,
    render_payload: dict[str, Any],
) -> None:
    _expect(text.get("schema_version") == 1, "text_geometry.schema_version", "expected schema version 1")
    _expect(text.get("valid") is True, "text_geometry.valid", "rendered text geometry must pass")
    _expect(text.get("decision") == "passed", "text_geometry.decision", "rendered text decision must pass")
    _expect(text.get("errors") == [], "text_geometry.errors", "rendered text errors must be empty")
    expected = {
        "page_id": page_id,
        "spec_sha256": content_hash,
        "input_spec_sha256": input_hash,
        "source_sha256": source_hash,
        "spec_file_sha256": spec.sha256,
        "pptx_sha256": pptx.sha256,
        "build_report_sha256": build.sha256,
        "render_report_sha256": render.sha256,
        "runtime_sha256": runtime.sha256,
        "pdf_sha256": pdf.sha256,
    }
    inputs = text.get("inputs")
    _expect(isinstance(inputs, dict), "text_geometry.inputs", "complete input identities are required")
    for field, value in expected.items():
        _expect(text.get(field) == value, f"text_geometry.{field}", "text geometry identity is stale")
        if field in inputs:
            _expect(inputs.get(field) == value, f"text_geometry.inputs.{field}", "text geometry input is stale")
    input_paths = text.get("input_paths")
    _expect(isinstance(input_paths, dict), "text_geometry.input_paths", "complete input paths are required")
    try:
        reported_runtime = Path(input_paths.get("runtime", "")).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _error("REVIEW_ADMISSION_NOT_ISSUED", "text_geometry.input_paths.runtime", "runtime path is invalid") from exc
    _expect(reported_runtime == runtime.path, "text_geometry.input_paths.runtime", "runtime path is stale")
    _expect(_same_page_size(text.get("page_size_pt")), "text_geometry.page_size_pt", "text geometry page size is stale")
    extractor = text.get("pdftotext")
    rendered_extractor = render_payload.get("text_extractor")
    _expect(isinstance(extractor, dict) and isinstance(rendered_extractor, dict), "text_geometry.pdftotext", "extractor identity is required")
    _expect(extractor.get("path") == rendered_extractor.get("path"), "text_geometry.pdftotext.path", "extractor path is stale")
    _expect(extractor.get("version") == rendered_extractor.get("version"), "text_geometry.pdftotext.version", "extractor version is stale")
    _expect(extractor.get("sha256") == rendered_extractor.get("executable_sha256"), "text_geometry.pdftotext.sha256", "extractor hash is stale")
    elements = text.get("elements")
    _expect(isinstance(elements, list), "text_geometry.elements", "element results must be an array")
    _expect(all(isinstance(item, dict) and item.get("status") == "passed" for item in elements), "text_geometry.elements", "every rendered text element must pass")


def _validate_background(
    background: dict[str, Any],
    *,
    page_id: str,
    content_hash: str,
    input_hash: str,
    pptx: _Snapshot,
    build: _Snapshot,
    build_payload: dict[str, Any],
    structure: _Snapshot,
    structure_payload: dict[str, Any],
) -> None:
    _expect(background.get("schema_version") == 1, "background_report.schema_version", "expected schema version 1")
    _expect(background.get("valid") is True, "background_report.valid", "background contract must pass")
    _expect(background.get("errors") == [], "background_report.errors", "background errors must be empty")
    expected = {
        "page_id": page_id,
        "spec_sha256": content_hash,
        "input_spec_sha256": input_hash,
        "pptx_sha256": pptx.sha256,
        "build_report_sha256": canonical_json_sha256(build_payload),
        "build_report_file_sha256": build.sha256,
        "structure_report_sha256": canonical_json_sha256(structure_payload),
        "structure_report_file_sha256": structure.sha256,
    }
    for field, value in expected.items():
        _expect(background.get(field) == value, f"background_report.{field}", "background identity is stale")
    items = background.get("items")
    _expect(isinstance(items, list) and bool(items), "background_report.items", "background evidence is required")
    _expect(all(isinstance(item, dict) and item.get("valid") is True for item in items), "background_report.items", "every background item must pass")


def _visual_artifact(
    value: Any,
    label: str,
    snapshots: list[_Snapshot],
    *,
    expected_path: Path | None = None,
) -> dict[str, str]:
    snapshot = _recorded_file(
        value, label, snapshots, expected_path=expected_path, image=True
    )
    return {"path": str(snapshot.path), "sha256": snapshot.sha256}


def _original_identity_from_validation(value: dict[str, str]) -> dict[str, str]:
    _raw, snapshot = _snapshot_file(value["path"], "visual_evidence")
    return _identity(snapshot)


def _validate_visual_diff(
    visual: dict[str, Any],
    visual_snapshot: _Snapshot,
    spec_payload: dict[str, Any],
    source: _Snapshot,
    preview: _Snapshot,
    pptx: _Snapshot,
    render: _Snapshot,
    render_payload: dict[str, Any],
    pdf: _Snapshot,
    snapshots: list[_Snapshot],
) -> dict[str, Any]:
    profile = spec_payload.get("verification_profile")
    _expect(visual.get("verification_profile") == profile, "visual_diff.verification_profile", "visual profile is stale")
    _recorded_file(visual.get("reference"), "visual_diff.reference", snapshots, expected_path=source.path, image=True)
    _recorded_file(visual.get("preview"), "visual_diff.preview", snapshots, expected_path=preview.path, image=True)
    _expect(visual.get("pptx_sha256") == pptx.sha256, "visual_diff.pptx_sha256", "visual diff PPTX is stale")
    render_record = visual.get("render_report")
    _expect(isinstance(render_record, dict), "visual_diff.render_report", "render report identity is required")
    _expect(render_record.get("path") == str(render.path), "visual_diff.render_report.path", "visual diff points to another render report")
    _expect(render_record.get("sha256") == render.sha256, "visual_diff.render_report.sha256", "visual diff render report is stale")
    _expect(visual.get("renderer") == render_payload.get("renderer"), "visual_diff.renderer", "visual diff renderer is stale")
    _expect(visual.get("pdf_sha256") == pdf.sha256, "visual_diff.pdf_sha256", "visual diff PDF is stale")
    try:
        report_path = Path(visual.get("report", "")).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _error("REVIEW_ADMISSION_NOT_ISSUED", "visual_diff.report", "invalid report path") from exc
    _expect(report_path == visual_snapshot.path, "visual_diff.report", "visual diff report path is stale")

    presence = visual.get("region_presence")
    _expect(isinstance(presence, dict), "visual_diff.region_presence", "region presence is required")
    _expect(presence.get("status") == "passed" and presence.get("missing") == [], "visual_diff.region_presence", "region presence must pass")
    tripwire = visual.get("tripwire")
    _expect(isinstance(tripwire, dict), "visual_diff.tripwire", "tripwire result is required")
    if tripwire.get("available") is True:
        _expect(tripwire.get("triggered") is False, "visual_diff.tripwire.triggered", "triggered tripwire blocks review")
    else:
        _expect(tripwire.get("available") is False and tripwire.get("triggered") is None, "visual_diff.tripwire", "unavailable tripwire must be explicit")

    evidence = visual.get("evidence")
    _expect(isinstance(evidence, dict), "visual_diff.evidence", "full-page evidence is required")
    overlay = _visual_artifact(evidence.get("overlay"), "visual_diff.evidence.overlay", snapshots)
    diff = _visual_artifact(evidence.get("diff"), "visual_diff.evidence.diff", snapshots)
    _expect(visual.get("overlay") == overlay["path"], "visual_diff.overlay", "overlay path is inconsistent")
    _expect(visual.get("diff") == diff["path"], "visual_diff.diff", "diff path is inconsistent")

    summary = visual.get("region_summary")
    regions = visual.get("regions")
    _expect(isinstance(summary, dict), "visual_diff.region_summary", "region summary is required")
    _expect(isinstance(regions, list), "visual_diff.regions", "region evidence must be an array")
    counts = [summary.get(key) for key in ("requested", "generated", "skipped")]
    _expect(all(type(value) is int and value >= 0 for value in counts), "visual_diff.region_summary", "region counts must be non-negative integers")
    _expect(counts[2] == 0 and counts[0] == counts[1] == len(regions), "visual_diff.region_summary", "all requested regions must be generated with skipped=0")
    _expect(visual.get("skipped_regions") == [], "visual_diff.skipped_regions", "skipped region details must be empty")

    spec_regions = spec_payload.get("regions")
    _expect(isinstance(spec_regions, list), "spec.regions", "spec regions must be an array")
    spec_region_map = {
        item.get("region_id"): item
        for item in spec_regions
        if isinstance(item, dict) and isinstance(item.get("region_id"), str)
    }
    if profile == "strict":
        _expect(len(regions) == len(spec_regions), "visual_diff.regions", "strict review requires every current spec region")
    region_evidence: list[dict[str, Any]] = []
    selected_spec_regions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(regions):
        label = f"visual_diff.regions[{index}]"
        _expect(isinstance(item, dict), label, "region evidence must be an object")
        region_id = item.get("region_id")
        _expect(isinstance(region_id, str) and region_id in spec_region_map and region_id not in seen, f"{label}.region_id", "region ID is missing, stale, or duplicated")
        seen.add(region_id)
        selected_spec_regions.append(spec_region_map[region_id])
        _expect(item.get("source_bbox") == spec_region_map[region_id].get("source_bbox"), f"{label}.source_bbox", "region bbox is stale")
        _expect(item.get("scale_percent") == 200, f"{label}.scale_percent", "region evidence must be 200 percent")
        artifact = _visual_artifact(
            {"path": item.get("evidence"), "sha256": item.get("evidence_sha256")},
            label,
            snapshots,
        )
        region_evidence.append(
            {
                "region_id": region_id,
                "source_bbox": copy.deepcopy(item["source_bbox"]),
                **artifact,
                "scale_percent": 200,
            }
        )

    changed_threshold = visual.get("changed_threshold")
    _expect(type(changed_threshold) is int and 0 <= changed_threshold <= 255, "visual_diff.changed_threshold", "changed threshold is invalid")
    minimum_similarity = tripwire.get("minimum_similarity")
    _expect(
        minimum_similarity is None
        or (
            isinstance(minimum_similarity, (int, float))
            and not isinstance(minimum_similarity, bool)
            and 0.0 <= float(minimum_similarity) <= 1.0
        ),
        "visual_diff.tripwire.minimum_similarity",
        "tripwire threshold is invalid",
    )
    try:
        visual_module = importlib.import_module("create_visual_diff")
        with tempfile.TemporaryDirectory(prefix="review-visual-recompute-") as temporary:
            recomputed = visual_module.build_visual_diff_from_render_report(
                source.path,
                render.path,
                Path(temporary),
                regions=selected_spec_regions,
                minimum_similarity=minimum_similarity,
                changed_threshold=changed_threshold,
                profile=profile,
            )
            semantic_fields = (
                "verification_profile",
                "reference_size",
                "preview_size",
                "alignment",
                "changed_threshold",
                "full_page",
                "tripwire",
                "region_presence",
                "skipped_regions",
                "region_summary",
            )
            for field in semantic_fields:
                _expect(
                    visual.get(field) == recomputed.get(field),
                    f"visual_diff.{field}",
                    "visual evidence does not match a current production recomputation",
                )

            for evidence_name, reported_identity in (("overlay", overlay), ("diff", diff)):
                recomputed_path = Path(recomputed["evidence"][evidence_name]["path"])
                recomputed_hash = hashlib.sha256(recomputed_path.read_bytes()).hexdigest()
                _expect(
                    reported_identity["sha256"] == recomputed_hash,
                    f"visual_diff.evidence.{evidence_name}.sha256",
                    "visual evidence image does not match a current production recomputation",
                )

            recomputed_regions = recomputed.get("regions")
            _expect(isinstance(recomputed_regions, list) and len(recomputed_regions) == len(regions), "visual_diff.regions", "recomputed region evidence is incomplete")
            for index, (reported_region, current_region) in enumerate(zip(regions, recomputed_regions)):
                reported_semantics = {
                    key: value
                    for key, value in reported_region.items()
                    if key not in {"evidence", "evidence_sha256"}
                }
                current_semantics = {
                    key: value
                    for key, value in current_region.items()
                    if key not in {"evidence", "evidence_sha256"}
                }
                _expect(
                    reported_semantics == current_semantics,
                    f"visual_diff.regions[{index}]",
                    "region metrics do not match a current production recomputation",
                )
                current_hash = hashlib.sha256(Path(current_region["evidence"]).read_bytes()).hexdigest()
                _expect(
                    region_evidence[index]["sha256"] == current_hash,
                    f"visual_diff.regions[{index}].evidence_sha256",
                    "region evidence image does not match a current production recomputation",
                )
    except ToolError:
        raise
    except Exception as exc:
        raise _error(
            "REVIEW_ADMISSION_NOT_ISSUED",
            "visual_diff",
            "visual evidence could not be recomputed",
        ) from exc
    return {
        "source": _identity(source),
        "preview": _identity(preview),
        "side_by_side": {
            "source": _identity(source),
            "preview": _identity(preview),
        },
        "overlay": _original_identity_from_validation(overlay),
        "diff": _original_identity_from_validation(diff),
        "regions": [
            {
                **item,
                **_original_identity_from_validation(
                    {"path": item["path"], "sha256": item["sha256"]}
                ),
            }
            for item in region_evidence
        ],
    }
