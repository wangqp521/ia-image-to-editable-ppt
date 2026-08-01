#!/usr/bin/env python3
"""Gate native text against actual Poppler PDF bounding boxes."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import selectors
import subprocess
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from lib.hashing import canonical_json_sha256
from lib.spec_identity import content_spec_sha256, input_spec_sha256


OVERFLOW_TOLERANCE_PT = 1.5
FIRST_TOKEN_SEARCH_MARGIN_PT = 12.0
AMBIGUITY_DISTANCE_PT = 0.5
PDFTOTEXT_TIMEOUT_SECONDS = 30
PDFTOTEXT_STDOUT_LIMIT_BYTES = 8 * 1024 * 1024
PDFTOTEXT_STDERR_LIMIT_BYTES = 64 * 1024
COMMAND_ERROR_DETAIL_LIMIT_CHARS = 160
EXPECTED_PAGE_SIZE_PT = (960.0, 540.0)
PAGE_SIZE_EPSILON_PT = 0.02
EMU_PER_POINT = 12_700.0
POPPLER_XHTML_DOCTYPE = (
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
    '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">'
)


@dataclass(frozen=True)
class GeometryError(ValueError):
    code: str
    path: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    sha256: str
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class PdfToken:
    text: str
    normalized_text: str
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    line_index: int
    token_index: int

    @property
    def center(self) -> tuple[float, float]:
        return (
            self.x_min + (self.x_max - self.x_min) / 2,
            self.y_min + (self.y_max - self.y_min) / 2,
        )

    @property
    def bbox_pt(self) -> list[float]:
        return [
            self.x_min,
            self.y_min,
            self.x_max - self.x_min,
            self.y_max - self.y_min,
        ]


def normalize_match_text(value: str) -> str:
    """NFC-normalize text and remove every Unicode whitespace character."""
    normalized = unicodedata.normalize("NFC", value)
    return "".join(character for character in normalized if not character.isspace())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _finite_number(value: Any, path: str) -> float:
    if not isinstance(value, str) or not value.strip():
        raise GeometryError("TEXT_GEOMETRY_XML_INVALID", path, "numeric attribute is missing")
    try:
        number = float(value)
    except ValueError as exc:
        raise GeometryError("TEXT_GEOMETRY_XML_INVALID", path, "numeric attribute is invalid") from exc
    if not math.isfinite(number):
        raise GeometryError("TEXT_GEOMETRY_XML_INVALID", path, "numeric attribute must be finite")
    return number


def _require_finite_xml(values: list[float], path: str) -> None:
    if any(not math.isfinite(value) for value in values):
        raise GeometryError(
            "TEXT_GEOMETRY_XML_INVALID",
            path,
            "derived PDF geometry must be finite",
        )


def parse_bbox_layout(xml_text: str) -> tuple[list[PdfToken], tuple[float, float]]:
    """Safely parse one Poppler bbox-layout page in document order."""
    if not isinstance(xml_text, str):
        raise GeometryError(
            "TEXT_GEOMETRY_XML_INVALID",
            "pdftotext.stdout",
            "DTD and entity declarations are forbidden",
        )
    parse_text = xml_text[1:] if xml_text.startswith("\ufeff") else xml_text
    if parse_text.startswith(POPPLER_XHTML_DOCTYPE):
        parse_text = parse_text[len(POPPLER_XHTML_DOCTYPE) :].lstrip(" \t\r\n")
    if "<!DOCTYPE" in parse_text.upper() or "<!ENTITY" in parse_text.upper():
        raise GeometryError(
            "TEXT_GEOMETRY_XML_INVALID",
            "pdftotext.stdout",
            "DTD and entity declarations are forbidden",
        )
    try:
        root = ET.fromstring(parse_text)
    except (ET.ParseError, ValueError) as exc:
        raise GeometryError(
            "TEXT_GEOMETRY_XML_INVALID", "pdftotext.stdout", f"invalid XML: {exc}"
        ) from exc
    pages = [element for element in root.iter() if _local_name(element.tag) == "page"]
    if len(pages) != 1:
        raise GeometryError(
            "TEXT_GEOMETRY_XML_INVALID",
            "pdftotext.stdout.page",
            f"expected exactly one page, got {len(pages)}",
        )
    page = pages[0]
    width = _finite_number(page.get("width"), "page.width")
    height = _finite_number(page.get("height"), "page.height")
    if width <= 0 or height <= 0:
        raise GeometryError(
            "TEXT_GEOMETRY_XML_INVALID", "page", "page dimensions must be positive"
        )

    word_lines: dict[int, int] = {}
    lines = [element for element in page.iter() if _local_name(element.tag) == "line"]
    for line_index, line in enumerate(lines):
        for word in line.iter():
            if _local_name(word.tag) == "word":
                word_lines[id(word)] = line_index
    words = [element for element in page.iter() if _local_name(element.tag) == "word"]
    if any(id(word) not in word_lines for word in words):
        raise GeometryError(
            "TEXT_GEOMETRY_XML_INVALID",
            "page.word",
            "every word must be contained by a line",
        )

    tokens: list[PdfToken] = []
    for token_index, word in enumerate(words):
        coordinates = [
            _finite_number(word.get(name), f"word[{token_index}].{name}")
            for name in ("xMin", "yMin", "xMax", "yMax")
        ]
        x_min, y_min, x_max, y_max = coordinates
        if x_max <= x_min or y_max <= y_min:
            raise GeometryError(
                "TEXT_GEOMETRY_XML_INVALID",
                f"word[{token_index}]",
                "word bounding box must have positive width and height",
            )
        word_width = x_max - x_min
        word_height = y_max - y_min
        center_x = x_min + word_width / 2
        center_y = y_min + word_height / 2
        _require_finite_xml(
            [word_width, word_height, center_x, center_y], f"word[{token_index}]"
        )
        text = "".join(word.itertext())
        tokens.append(
            PdfToken(
                text=text,
                normalized_text=normalize_match_text(text),
                x_min=x_min,
                y_min=y_min,
                x_max=x_max,
                y_max=y_max,
                line_index=word_lines[id(word)],
                token_index=token_index,
            )
        )
    return tokens, (width, height)


def _base_report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "page_id": None,
        "spec_sha256": None,
        "input_spec_sha256": None,
        "source_sha256": None,
        "spec_file_sha256": None,
        "pptx_sha256": None,
        "build_report_sha256": None,
        "render_report_sha256": None,
        "runtime_sha256": None,
        "pdf_sha256": None,
        "inputs": {
            "spec_file_sha256": None,
            "pptx_sha256": None,
            "build_report_sha256": None,
            "render_report_sha256": None,
            "runtime_sha256": None,
            "pdf_sha256": None,
        },
        "input_paths": {
            "spec": None,
            "pptx": None,
            "build_report": None,
            "render_report": None,
            "runtime": None,
            "pdf": None,
        },
        "pdftotext": {"path": None, "version": None, "sha256": None},
        "page_size_pt": None,
        "constants": {
            "overflow_tolerance_pt": OVERFLOW_TOLERANCE_PT,
            "first_token_search_margin_pt": FIRST_TOKEN_SEARCH_MARGIN_PT,
            "ambiguity_distance_pt": AMBIGUITY_DISTANCE_PT,
        },
        "elements": [],
        "errors": [],
        "valid": False,
        "decision": "failed",
    }


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_snapshot(path: Path, label: str) -> tuple[bytes, FileSnapshot]:
    """Read one stable file instance and bind its bytes to its path identity."""
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            data = stream.read()
            after = os.fstat(stream.fileno())
        path_after = path.stat()
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise GeometryError(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH", label, f"cannot read {label}: {exc}"
        ) from exc
    identities = [_stat_identity(value) for value in (before, after, path_after)]
    if identities[0] != identities[1] or identities[1] != identities[2]:
        raise GeometryError(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH",
            label,
            f"{label} changed while it was being read",
        )
    return data, FileSnapshot(
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        identity=identities[0],
    )


def _snapshot_file(path: Path, label: str) -> FileSnapshot:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        path_after = path.stat()
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise GeometryError(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH", label, f"cannot read {label}: {exc}"
        ) from exc
    identities = [_stat_identity(value) for value in (before, after, path_after)]
    if identities[0] != identities[1] or identities[1] != identities[2]:
        raise GeometryError(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH",
            label,
            f"{label} changed while it was being hashed",
        )
    return FileSnapshot(path=path, sha256=digest.hexdigest(), identity=identities[0])


def _load_json_snapshot(path: Path, label: str) -> tuple[dict[str, Any], FileSnapshot]:
    data, snapshot = _read_snapshot(path, label)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise GeometryError(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH", label, f"cannot decode {label}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise GeometryError(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH", label, f"{label} root must be an object"
        )
    return payload, snapshot


def _verify_snapshot(snapshot: FileSnapshot, label: str) -> None:
    current = _snapshot_file(snapshot.path, label)
    if current.identity != snapshot.identity or current.sha256 != snapshot.sha256:
        raise GeometryError(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH",
            label,
            f"{label} changed after validation",
        )


def _resolved_file(path: str | Path, label: str) -> Path:
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise GeometryError(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH", label, f"invalid {label} path"
        ) from exc
    if not resolved.is_file():
        raise GeometryError(
            "TEXT_GEOMETRY_IDENTITY_MISMATCH", label, f"{label} is not a regular file"
        )
    return resolved


def _expect(condition: bool, path: str, detail: str) -> None:
    if not condition:
        raise GeometryError("TEXT_GEOMETRY_IDENTITY_MISMATCH", path, detail)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _page_size(value: Any, path: str) -> tuple[float, float]:
    _expect(isinstance(value, list) and len(value) == 2, path, "page size must contain two numbers")
    numbers: list[float] = []
    for item in value:
        _expect(type(item) in {int, float} and math.isfinite(float(item)), path, "page size must contain finite numbers")
        numbers.append(float(item))
    return numbers[0], numbers[1]


def _same_page_size(actual: tuple[float, float], expected: tuple[float, float]) -> bool:
    return all(abs(left - right) <= PAGE_SIZE_EPSILON_PT for left, right in zip(actual, expected))


def _validate_source(
    spec: dict[str, Any], report: dict[str, Any], snapshots: list[tuple[FileSnapshot, str]]
) -> None:
    source = spec.get("content_reference")
    _expect(isinstance(source, dict), "spec.content_reference", "content reference is missing")
    path_value = source.get("path")
    declared = source.get("sha256")
    source_path = _resolved_file(path_value, "spec.content_reference.path")
    snapshot = _snapshot_file(source_path, "spec.content_reference.path")
    actual = snapshot.sha256
    _expect(_is_sha256(declared) and declared == actual, "spec.content_reference.sha256", "source identity is stale")
    report["source_sha256"] = actual
    snapshots.append((snapshot, "spec.content_reference.path"))


def _command_error(detail: str, path: str = "pdftotext") -> GeometryError:
    return GeometryError(
        "TEXT_GEOMETRY_COMMAND_FAILED",
        path,
        detail[:COMMAND_ERROR_DETAIL_LIMIT_CHARS],
    )


def _run_pdftotext(executable: Path, pdf: Path) -> str:
    command = [
        str(executable),
        "-bbox-layout",
        "-enc",
        "UTF-8",
        str(pdf),
        "-",
    ]
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {
        "stdout": PDFTOTEXT_STDOUT_LIMIT_BYTES,
        "stderr": PDFTOTEXT_STDERR_LIMIT_BYTES,
    }
    selector: selectors.BaseSelector | None = None
    exceeded: str | None = None
    timed_out = False
    returncode: int | None = None
    lifecycle_error: GeometryError | None = None
    deadline = time.monotonic() + PDFTOTEXT_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise _command_error(f"command failed: {exc}") from exc

    try:
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("pdftotext pipes are unavailable")
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
                try:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                except OSError as exc:
                    process.kill()
                    raise _command_error(f"cannot read {stream_name}: {exc}") from exc
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
    except GeometryError as exc:
        lifecycle_error = exc
    except Exception as exc:
        lifecycle_error = _command_error(
            f"command lifecycle failed: {type(exc).__name__}: {exc}"
        )
    finally:
        try:
            if process.poll() is None:
                process.kill()
            process.wait()
        except Exception as exc:
            if lifecycle_error is None:
                lifecycle_error = _command_error(
                    f"cannot reap command: {type(exc).__name__}: {exc}"
                )
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except Exception as exc:
                    if lifecycle_error is None:
                        lifecycle_error = _command_error(
                            f"cannot close command pipe: {type(exc).__name__}: {exc}"
                        )
        if selector is not None:
            try:
                selector.close()
            except Exception as exc:
                if lifecycle_error is None:
                    lifecycle_error = _command_error(
                        f"cannot close selector: {type(exc).__name__}: {exc}"
                    )

    if lifecycle_error is not None:
        raise lifecycle_error

    if timed_out:
        raise _command_error(
            f"command timed out after {PDFTOTEXT_TIMEOUT_SECONDS} seconds",
        )
    if exceeded is not None:
        raise _command_error(
            f"{exceeded} exceeded {limits[exceeded]} byte limit",
        )
    if returncode != 0:
        detail_bytes = bytes(buffers["stderr"] or buffers["stdout"])
        detail = detail_bytes.decode("utf-8", errors="replace").strip()
        raise _command_error(f"exit={returncode}: {detail}")
    try:
        return bytes(buffers["stdout"]).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _command_error("stdout is not valid UTF-8", "pdftotext.stdout") from exc


def _runtime_tool(
    executables: dict[str, Any], name: str
) -> tuple[Path, str, str, FileSnapshot]:
    entry = executables.get(name)
    _expect(isinstance(entry, dict), f"runtime.executables.{name}", f"{name} identity is missing")
    executable = _resolved_file(entry.get("path"), f"runtime.executables.{name}.path")
    _expect(os.access(executable, os.X_OK), f"runtime.executables.{name}.path", f"{name} is not executable")
    snapshot = _snapshot_file(executable, f"runtime.executables.{name}.path")
    actual_sha256 = snapshot.sha256
    _expect(
        _is_sha256(entry.get("sha256")) and entry.get("sha256") == actual_sha256,
        f"runtime.executables.{name}.sha256",
        f"{name} binary identity is stale",
    )
    version = entry.get("version")
    valid_version = False
    if isinstance(version, str):
        lowered = version.strip().lower()
        valid_version = bool(lowered) and not (
            lowered.startswith("unavailable")
            or lowered.startswith("exit=")
            or "timed out" in lowered
        )
    _expect(
        valid_version,
        f"runtime.executables.{name}.version",
        f"{name} version is missing",
    )
    return executable, version, actual_sha256, snapshot


def _validate_runtime_chain(
    runtime: dict[str, Any],
    render: dict[str, Any],
    report: dict[str, Any],
    snapshots: list[tuple[FileSnapshot, str]],
) -> Path:
    _expect(
        runtime.get("valid") is True and runtime.get("errors") == [],
        "runtime.valid",
        "runtime preflight did not pass",
    )
    executables = runtime.get("executables")
    _expect(
        isinstance(executables, dict),
        "runtime.executables",
        "runtime executable identities are missing",
    )
    soffice, soffice_version, soffice_hash, soffice_snapshot = _runtime_tool(executables, "soffice")
    pdftoppm, pdftoppm_version, pdftoppm_hash, pdftoppm_snapshot = _runtime_tool(
        executables, "pdftoppm"
    )
    _pdffonts, _pdffonts_version, _pdffonts_hash, pdffonts_snapshot = _runtime_tool(executables, "pdffonts")
    pdftotext, pdftotext_version, pdftotext_hash, pdftotext_snapshot = _runtime_tool(
        executables, "pdftotext"
    )
    snapshots.extend(
        [
            (soffice_snapshot, "runtime.executables.soffice.path"),
            (pdftoppm_snapshot, "runtime.executables.pdftoppm.path"),
            (pdffonts_snapshot, "runtime.executables.pdffonts.path"),
            (pdftotext_snapshot, "runtime.executables.pdftotext.path"),
        ]
    )
    fontconfig_entry = runtime.get("fontconfig")
    _expect(
        isinstance(fontconfig_entry, dict),
        "runtime.fontconfig",
        "fontconfig identity is missing",
    )
    fontconfig = _resolved_file(
        fontconfig_entry.get("path"), "runtime.fontconfig.path"
    )
    fontconfig_snapshot = _snapshot_file(fontconfig, "runtime.fontconfig.path")
    fontconfig_hash = fontconfig_snapshot.sha256
    _expect(
        _is_sha256(fontconfig_entry.get("sha256"))
        and fontconfig_entry.get("sha256") == fontconfig_hash,
        "runtime.fontconfig.sha256",
        "fontconfig identity is stale",
    )
    snapshots.append((fontconfig_snapshot, "runtime.fontconfig.path"))

    renderer = render.get("renderer")
    _expect(
        isinstance(renderer, dict),
        "render_report.renderer",
        "renderer identity is missing",
    )
    _expect(
        renderer.get("backend") == "libreoffice"
        and renderer.get("path") == str(soffice)
        and renderer.get("version") == soffice_version
        and renderer.get("executable_sha256") == soffice_hash
        and renderer.get("fontconfig_path") == str(fontconfig)
        and renderer.get("fontconfig_sha256") == fontconfig_hash,
        "render_report.renderer",
        "render report renderer identity differs from runtime",
    )
    rasterizer = render.get("rasterizer")
    _expect(
        isinstance(rasterizer, dict)
        and rasterizer.get("path") == str(pdftoppm)
        and rasterizer.get("version") == pdftoppm_version
        and rasterizer.get("executable_sha256") == pdftoppm_hash,
        "render_report.rasterizer",
        "render report rasterizer identity differs from runtime",
    )
    text_extractor = render.get("text_extractor")
    _expect(
        isinstance(text_extractor, dict)
        and text_extractor.get("path") == str(pdftotext)
        and text_extractor.get("version") == pdftotext_version
        and text_extractor.get("executable_sha256") == pdftotext_hash,
        "render_report.text_extractor",
        "render report text extractor identity differs from runtime",
    )
    report["pdftotext"] = {
        "path": str(pdftotext),
        "version": pdftotext_version,
        "sha256": pdftotext_hash,
    }
    return pdftotext


def _expected_native_text(spec: dict[str, Any], build: dict[str, Any]) -> list[dict[str, Any]]:
    elements_value = spec.get("elements")
    order = spec.get("reading_order")
    modules = spec.get("modules")
    typography_module = modules.get("typography") if isinstance(modules, dict) else None
    typography_items = typography_module.get("items") if isinstance(typography_module, dict) else None
    build_elements = build.get("elements")
    _expect(isinstance(elements_value, list), "spec.elements", "elements must be an array")
    _expect(isinstance(order, list), "spec.reading_order", "reading_order must be an array")
    _expect(
        bool(order)
        and all(isinstance(element_id, str) and element_id for element_id in order),
        "spec.reading_order",
        "reading_order must contain non-empty element IDs",
    )
    _expect(isinstance(typography_items, list), "spec.modules.typography.items", "typography items must be an array")
    _expect(isinstance(build_elements, dict), "build_report.elements", "build elements are missing")
    elements: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(elements_value):
        _expect(
            isinstance(item, dict),
            f"spec.elements[{index}]",
            "element must be an object",
        )
        element_id = item.get("element_id")
        _expect(
            isinstance(element_id, str) and bool(element_id),
            f"spec.elements[{index}].element_id",
            "element_id must be a non-empty string",
        )
        _expect(
            element_id not in elements,
            f"spec.elements[{index}].element_id",
            f"duplicate element_id: {element_id}",
        )
        elements[element_id] = item
    _expect(
        len(order) == len(set(order)),
        "spec.reading_order",
        "reading_order element IDs must be unique",
    )
    order_ids = set(order)
    element_ids = set(elements)
    _expect(
        order_ids <= element_ids,
        "spec.reading_order",
        "reading_order references unknown element IDs",
    )
    _expect(
        order_ids == element_ids,
        "spec.reading_order",
        "reading_order must exactly cover every element ID",
    )
    typography: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(typography_items):
        _expect(
            isinstance(item, dict),
            f"spec.modules.typography.items[{index}]",
            "typography item must be an object",
        )
        element_id = item.get("element_id")
        _expect(
            isinstance(element_id, str) and bool(element_id),
            f"spec.modules.typography.items[{index}].element_id",
            "typography element_id must be a non-empty string",
        )
        _expect(
            element_id not in typography,
            f"spec.modules.typography.items[{index}].element_id",
            f"duplicate typography element_id: {element_id}",
        )
        typography[element_id] = item
    result: list[dict[str, Any]] = []
    for element_id in order:
        element = elements.get(element_id)
        if not isinstance(element, dict) or element.get("kind") not in {"text", "special_text"}:
            continue
        content = element.get("content")
        _expect(
            isinstance(content, dict),
            f"spec.elements.{element_id}.content",
            "native text content must be an object",
        )
        text = content.get("text")
        _expect(
            isinstance(text, str),
            f"spec.elements.{element_id}.content.text",
            "native content.text must be a string",
        )
        if not normalize_match_text(text):
            continue
        build_item = build_elements.get(element_id)
        _expect(isinstance(build_item, dict), f"build_report.elements.{element_id}", "native text build fact is missing")
        _expect(
            build_item.get("selected_mode") == "native"
            and build_item.get("object_type") == "sp"
            and build_item.get("semantic_kind") == element.get("kind"),
            f"build_report.elements.{element_id}",
            "text element was not built as native text",
        )
        contract = typography.get(element_id)
        _expect(isinstance(contract, dict), f"modules.typography.items.{element_id}", "typography contract is missing")
        _expect(contract.get("text") == text, f"modules.typography.items.{element_id}.text", "typography text differs from element text")
        paragraphs = contract.get("paragraphs")
        _expect(
            isinstance(paragraphs, list) and bool(paragraphs),
            f"modules.typography.items.{element_id}.paragraphs",
            "typography paragraphs are missing",
        )
        match_parts: list[str] = []
        has_char_bullet = False
        cursor = 0
        for paragraph_index, paragraph in enumerate(paragraphs):
            paragraph_path = (
                f"modules.typography.items.{element_id}.paragraphs[{paragraph_index}]"
            )
            _expect(
                isinstance(paragraph, dict),
                paragraph_path,
                "typography paragraph must be an object",
            )
            start = paragraph.get("start")
            end = paragraph.get("end")
            _expect(
                type(start) is int
                and type(end) is int
                and start == cursor
                and start < end <= len(text),
                paragraph_path,
                f"expected continuous paragraph range beginning at {cursor}",
            )
            list_contract = paragraph.get("list")
            _expect(
                isinstance(list_contract, dict),
                f"{paragraph_path}.list",
                "paragraph list contract is missing",
            )
            if (
                list_contract.get("is_list") is True
                and list_contract.get("bullet_type") == "char"
            ):
                bullet = list_contract.get("bullet")
                _expect(
                    isinstance(bullet, str) and bool(normalize_match_text(bullet)),
                    f"{paragraph_path}.list.bullet",
                    "char bullet must contain visible text",
                )
                match_parts.append(bullet)
                has_char_bullet = True
            match_parts.append(text[start:end])
            cursor = end
        _expect(
            cursor == len(text),
            f"modules.typography.items.{element_id}.paragraphs",
            f"paragraphs end at {cursor}, text length is {len(text)}",
        )
        match_text = "".join(match_parts)
        text_box = contract.get("text_box")
        _expect(isinstance(text_box, dict), f"modules.typography.items.{element_id}.text_box", "text box is missing")
        values: list[float] = []
        for field in ("x", "y", "w", "h"):
            value = text_box.get(field)
            _expect(type(value) in {int, float} and math.isfinite(float(value)), f"modules.typography.items.{element_id}.text_box.{field}", "text box coordinate is invalid")
            values.append(float(value) / EMU_PER_POINT)
        _expect(values[2] > 0 and values[3] > 0, f"modules.typography.items.{element_id}.text_box", "text box size must be positive")
        expected_derived = [
            values[0] + values[2],
            values[1] + values[3],
            values[0] + values[2] / 2,
            values[1] + values[3] / 2,
        ]
        _expect(
            all(math.isfinite(value) for value in [*values, *expected_derived]),
            f"modules.typography.items.{element_id}.text_box",
            "text box derived geometry must be finite",
        )
        result.append(
            {
                "element_id": element_id,
                "text": text,
                "match_text": match_text,
                "has_char_bullet": has_char_bullet,
                "expected_bbox_pt": values,
            }
        )
    return result


def _inside_first_token_margin(token: PdfToken, expected: list[float]) -> bool:
    x, y = token.center
    left, top, width, height = expected
    margin = FIRST_TOKEN_SEARCH_MARGIN_PT
    return left - margin <= x <= left + width + margin and top - margin <= y <= top + height + margin


def _inside_text_box(token: PdfToken, expected: list[float]) -> bool:
    x, y = token.center
    left, top, width, height = expected
    return left <= x <= left + width and top <= y <= top + height


def _union_bbox(tokens: list[PdfToken]) -> list[float]:
    x_min = min(token.x_min for token in tokens)
    y_min = min(token.y_min for token in tokens)
    x_max = max(token.x_max for token in tokens)
    y_max = max(token.y_max for token in tokens)
    bbox = [x_min, y_min, x_max - x_min, y_max - y_min]
    _require_finite_xml(bbox, "matched_tokens.union_bbox")
    return bbox


def _center_distance(actual: list[float], expected: list[float]) -> float:
    centers = [
        actual[0] + actual[2] / 2,
        actual[1] + actual[3] / 2,
        expected[0] + expected[2] / 2,
        expected[1] + expected[3] / 2,
    ]
    _require_finite_xml(centers, "matched_tokens.center")
    distance = math.hypot(centers[0] - centers[2], centers[1] - centers[3])
    _require_finite_xml([distance], "matched_tokens.distance")
    return distance


def _overflow(actual: list[float], expected: list[float]) -> dict[str, float]:
    overflow = {
        "left": max(0.0, expected[0] - actual[0]),
        "top": max(0.0, expected[1] - actual[1]),
        "right": max(0.0, actual[0] + actual[2] - expected[0] - expected[2]),
        "bottom": max(0.0, actual[1] + actual[3] - expected[1] - expected[3]),
    }
    _require_finite_xml(list(overflow.values()), "matched_tokens.overflow")
    return overflow


def _token_record(token: PdfToken) -> dict[str, Any]:
    return {
        "token_index": token.token_index,
        "line_index": token.line_index,
        "text": token.text,
        "normalized_text": token.normalized_text,
        "bbox_pt": token.bbox_pt,
    }


def _match_element(
    expected_item: dict[str, Any], tokens: list[PdfToken], occupied: set[int]
) -> dict[str, Any]:
    expected_match_text = normalize_match_text(expected_item["match_text"])
    normalized_text = normalize_match_text(expected_item["text"])
    expected_bbox = expected_item["expected_bbox_pt"]
    candidate_tokens = tokens
    if expected_item["has_char_bullet"]:
        spatial_lines: dict[int, list[PdfToken]] = {}
        for token in tokens:
            if (
                token.token_index not in occupied
                and token.normalized_text
                and _inside_text_box(token, expected_bbox)
            ):
                spatial_lines.setdefault(token.line_index, []).append(token)
        ordered_lines = sorted(
            spatial_lines.values(),
            key=lambda line: (
                min(token.y_min for token in line),
                min(token.x_min for token in line),
                min(token.token_index for token in line),
            ),
        )
        candidate_tokens = [
            token
            for line in ordered_lines
            for token in sorted(
                line,
                key=lambda item: (item.x_min, item.y_min, item.token_index),
            )
        ]
    full: list[tuple[float, list[int], list[float]]] = []
    prefix_seen = False
    if expected_item["has_char_bullet"]:
        indices = [token.token_index for token in candidate_tokens]
        combined = "".join(token.normalized_text for token in candidate_tokens)
        prefix_seen = bool(candidate_tokens)
        if combined == expected_match_text:
            actual = _union_bbox(candidate_tokens)
            full.append((_center_distance(actual, expected_bbox), indices, actual))
    else:
        for start, token in enumerate(candidate_tokens):
            if token.token_index in occupied or not _inside_first_token_margin(token, expected_bbox):
                continue
            combined = ""
            indices = []
            for index in range(start, len(candidate_tokens)):
                candidate = candidate_tokens[index]
                if candidate.token_index in occupied:
                    break
                combined += candidate.normalized_text
                indices.append(candidate.token_index)
                if combined == expected_match_text:
                    actual = _union_bbox([tokens[item] for item in indices])
                    full.append((_center_distance(actual, expected_bbox), list(indices), actual))
                    break
                if combined and expected_match_text.startswith(combined):
                    prefix_seen = True
                    continue
                break
        if not full:
            spatial_lines: dict[int, list[PdfToken]] = {}
            for token in tokens:
                if (
                    token.token_index not in occupied
                    and token.normalized_text
                    and _inside_text_box(token, expected_bbox)
                ):
                    spatial_lines.setdefault(token.line_index, []).append(token)
            ordered_lines = sorted(
                spatial_lines.values(),
                key=lambda line: (
                    min(token.y_min for token in line),
                    min(token.x_min for token in line),
                    min(token.token_index for token in line),
                ),
            )
            spatial_tokens = [
                token
                for line in ordered_lines
                for token in sorted(
                    line,
                    key=lambda item: (item.x_min, item.y_min, item.token_index),
                )
            ]
            combined = "".join(token.normalized_text for token in spatial_tokens)
            if spatial_tokens and combined == expected_match_text:
                indices = [token.token_index for token in spatial_tokens]
                actual = _union_bbox(spatial_tokens)
                full.append((_center_distance(actual, expected_bbox), indices, actual))

    result = {
        "element_id": expected_item["element_id"],
        "original_text": expected_item["text"],
        "normalized_text": normalized_text,
        "expected_bbox_pt": list(expected_bbox),
        "actual_bbox_pt": None,
        "matched_tokens": [],
        "line_count": 0,
        "overflow_pt": {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0},
        "status": "missing",
    }
    if not full:
        result["status"] = "incomplete" if prefix_seen else "missing"
        return result
    full.sort(key=lambda candidate: candidate[0])
    if len(full) > 1 and full[1][0] - full[0][0] <= AMBIGUITY_DISTANCE_PT:
        result["status"] = "ambiguous"
        return result
    _distance, selected_indices, actual = full[0]
    occupied.update(selected_indices)
    selected_tokens = [tokens[index] for index in selected_indices]
    overflow = _overflow(actual, expected_bbox)
    result["actual_bbox_pt"] = actual
    result["matched_tokens"] = [_token_record(token) for token in selected_tokens]
    result["line_count"] = len({token.line_index for token in selected_tokens})
    result["overflow_pt"] = overflow
    result["status"] = (
        "overflow"
        if any(amount > OVERFLOW_TOLERANCE_PT for amount in overflow.values())
        else "passed"
    )
    return result


def _publish_json_no_overwrite(path: Path, payload: dict[str, Any]) -> None:
    destination = path.expanduser()
    parent = destination.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(str(destination))
        encoded = (
            json.dumps(payload, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, TypeError, ValueError) as exc:
        raise GeometryError(
            "TEXT_GEOMETRY_OUTPUT_INVALID",
            str(destination),
            "output already exists" if isinstance(exc, FileExistsError) else f"cannot publish output: {exc}",
        ) from exc


def create_rendered_text_geometry(
    spec_path: str | Path,
    pptx_path: str | Path,
    build_report_path: str | Path,
    render_report_path: str | Path,
    runtime_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Validate the full render chain, match native text, and publish evidence."""
    report = _base_report()
    try:
        output = Path(output_path).expanduser()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise GeometryError(
            "TEXT_GEOMETRY_OUTPUT_INVALID", "output", "invalid output path"
        ) from exc
    try:
        snapshots: list[tuple[FileSnapshot, str]] = []
        if output.exists() or output.is_symlink():
            raise GeometryError("TEXT_GEOMETRY_OUTPUT_INVALID", str(output), "output already exists")
        supplied_values = {
            "spec": spec_path,
            "pptx": pptx_path,
            "build_report": build_report_path,
            "render_report": render_report_path,
            "runtime": runtime_path,
        }
        supplied: dict[str, Path] = {}
        for key, value in supplied_values.items():
            supplied[key] = Path(value).expanduser()
            report["input_paths"][key] = str(supplied[key])
        spec_file = _resolved_file(supplied["spec"], "spec")
        pptx = _resolved_file(supplied["pptx"], "pptx")
        build_file = _resolved_file(supplied["build_report"], "build_report")
        render_file = _resolved_file(supplied["render_report"], "render_report")
        runtime_file = _resolved_file(supplied["runtime"], "runtime")
        report["input_paths"].update(
            {
                "spec": str(spec_file),
                "pptx": str(pptx),
                "build_report": str(build_file),
                "render_report": str(render_file),
                "runtime": str(runtime_file),
            }
        )
        spec, spec_snapshot = _load_json_snapshot(spec_file, "spec")
        build, build_snapshot = _load_json_snapshot(build_file, "build_report")
        render, render_snapshot = _load_json_snapshot(render_file, "render_report")
        runtime, runtime_snapshot = _load_json_snapshot(runtime_file, "runtime")
        pptx_snapshot = _snapshot_file(pptx, "pptx")
        snapshots.extend(
            [
                (spec_snapshot, "spec"),
                (pptx_snapshot, "pptx"),
                (build_snapshot, "build_report"),
                (render_snapshot, "render_report"),
                (runtime_snapshot, "runtime"),
            ]
        )
        report["inputs"].update(
            {
                "spec_file_sha256": spec_snapshot.sha256,
                "pptx_sha256": pptx_snapshot.sha256,
                "build_report_sha256": build_snapshot.sha256,
                "render_report_sha256": render_snapshot.sha256,
                "runtime_sha256": runtime_snapshot.sha256,
            }
        )
        report.update(report["inputs"])
        report["page_id"] = spec.get("page_id")
        _expect(
            isinstance(report["page_id"], str) and bool(report["page_id"]),
            "spec.page_id",
            "page_id must be a non-empty string",
        )
        report["spec_sha256"] = content_spec_sha256(spec)
        report["input_spec_sha256"] = input_spec_sha256(spec)
        _validate_source(spec, report, snapshots)
        canvas = spec.get("canvas")
        _expect(isinstance(canvas, dict), "spec.canvas", "canvas must be an object")
        _expect(canvas.get("slide_size_emu") == [12_192_000, 6_858_000], "spec.canvas.slide_size_emu", "slide must be 960 x 540 pt")
        _expect(build.get("valid") is True, "build_report.valid", "build report did not pass")
        _expect(build.get("schema_sha256") == canonical_json_sha256(spec), "build_report.schema_sha256", "build report schema identity is stale")
        _expect(build.get("content_spec_sha256") == report["spec_sha256"], "build_report.content_spec_sha256", "build report content identity is stale")
        _expect(build.get("input_spec_sha256") == report["input_spec_sha256"], "build_report.input_spec_sha256", "build report input identity is stale")
        pptx_hash = report["inputs"]["pptx_sha256"]
        _expect(build.get("pptx_sha256") == pptx_hash, "build_report.pptx_sha256", "build report PPTX identity is stale")

        render_pptx = render.get("pptx")
        _expect(isinstance(render_pptx, dict), "render_report.pptx", "render PPTX identity is missing")
        _expect(render_pptx.get("path") == str(pptx.resolve()), "render_report.pptx.path", "render report points to another PPTX")
        _expect(render_pptx.get("sha256") == pptx_hash, "render_report.pptx.sha256", "render report PPTX identity is stale")
        pdf_record = render.get("pdf")
        _expect(isinstance(pdf_record, dict), "render_report.pdf", "PDF identity is missing")
        pdf = _resolved_file(pdf_record.get("path"), "render_report.pdf.path")
        pdf_snapshot = _snapshot_file(pdf, "render_report.pdf.path")
        pdf_hash = pdf_snapshot.sha256
        snapshots.append((pdf_snapshot, "render_report.pdf.path"))
        report["inputs"]["pdf_sha256"] = pdf_hash
        report["pdf_sha256"] = pdf_hash
        report["input_paths"]["pdf"] = str(pdf)
        _expect(pdf_record.get("sha256") == pdf_hash, "render_report.pdf.sha256", "PDF identity is stale")
        _expect(pdf_record.get("pages") == 1, "render_report.pdf.pages", "PDF must contain exactly one page")
        reported_page_size = _page_size(pdf_record.get("page_size_pt"), "render_report.pdf.page_size_pt")
        _expect(_same_page_size(reported_page_size, EXPECTED_PAGE_SIZE_PT), "render_report.pdf.page_size_pt", "PDF page must be 960 x 540 pt")
        report["page_size_pt"] = list(reported_page_size)

        pdftotext = _validate_runtime_chain(runtime, render, report, snapshots)

        expected_items = _expected_native_text(spec, build)
        post_use_error: Exception | None = None
        try:
            xml_text = _run_pdftotext(pdftotext, pdf)
            tokens, extracted_page_size = parse_bbox_layout(xml_text)
            _expect(_same_page_size(extracted_page_size, EXPECTED_PAGE_SIZE_PT), "pdftotext.page_size", "extracted PDF page must be 960 x 540 pt")
            _expect(_same_page_size(extracted_page_size, reported_page_size), "pdftotext.page_size", "extracted page size differs from render report")
            occupied: set[int] = set()
            report["elements"] = [
                _match_element(item, tokens, occupied) for item in expected_items
            ]
            code_by_status = {
                "missing": "TEXT_GEOMETRY_MISSING",
                "ambiguous": "TEXT_GEOMETRY_AMBIGUOUS",
                "incomplete": "TEXT_GEOMETRY_INCOMPLETE",
                "overflow": "TEXT_GEOMETRY_OVERFLOW",
            }
            for item in report["elements"]:
                if item["status"] != "passed":
                    report["errors"].append(
                        {
                            "code": code_by_status[item["status"]],
                            "path": f"elements.{item['element_id']}",
                            "detail": f"rendered text status is {item['status']}",
                        }
                    )
        except Exception as exc:
            post_use_error = exc
        try:
            for snapshot, label in snapshots:
                _verify_snapshot(snapshot, label)
        except GeometryError:
            report["elements"] = []
            report["errors"] = []
            raise
        if post_use_error is not None:
            raise post_use_error
        report["valid"] = bool(
            all(item["status"] == "passed" for item in report["elements"])
        )
        report["decision"] = "passed" if report["valid"] else "failed"
    except GeometryError as exc:
        if exc.code == "TEXT_GEOMETRY_IDENTITY_MISMATCH":
            report["elements"] = []
        report["errors"].append(exc.as_dict())
        report["valid"] = False
        report["decision"] = "failed"
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, OverflowError) as exc:
        report["errors"].append(
            {
                "code": "TEXT_GEOMETRY_IDENTITY_MISMATCH",
                "path": "inputs",
                "detail": f"invalid input contract: {exc}",
            }
        )
        report["valid"] = False
        report["decision"] = "failed"

    _publish_json_no_overwrite(output, report)
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--pptx", type=Path, required=True)
    parser.add_argument("--build-report", type=Path, required=True)
    parser.add_argument("--render-report", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = create_rendered_text_geometry(
            args.spec,
            args.pptx,
            args.build_report,
            args.render_report,
            args.runtime,
            args.output,
        )
    except GeometryError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
