#!/usr/bin/env python3
"""Normalize native-list font, size, and color OOXML against one page spec."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)

SLIDE_PART_PATTERN = re.compile(r"^ppt/slides/slide\d+\.xml$")
RGB_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
STYLE_TAGS = {
    "buFontTx",
    "buFont",
    "buSzTx",
    "buSzPct",
    "buSzPts",
    "buClrTx",
    "buClr",
}
IDENTITY_TAGS = {"buChar", "buAutoNum", "buBlip"}


class NormalizeError(RuntimeError):
    def __init__(self, code: str, path: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.path = path
        self.detail = detail


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _qualified(local_name: str) -> str:
    return f"{{{NS['a']}}}{local_name}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


def _load_spec(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NormalizeError(
            "NORMALIZE_SPEC_INVALID",
            str(path),
            f"cannot read schema v2 spec: {exc}",
        ) from exc
    if not isinstance(spec, dict):
        raise NormalizeError(
            "NORMALIZE_SPEC_INVALID",
            str(path),
            "spec root must be an object",
        )
    typography = spec.get("modules", {}).get("typography")
    items = typography.get("items") if isinstance(typography, dict) else []
    if not isinstance(items, list):
        raise NormalizeError(
            "NORMALIZE_SPEC_INVALID",
            "modules.typography.items",
            "typography items must be an array",
        )

    targets: list[dict[str, Any]] = []
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        paragraphs = item.get("paragraphs")
        if not isinstance(paragraphs, list):
            continue
        list_indexes = [
            index
            for index, paragraph in enumerate(paragraphs)
            if isinstance(paragraph, dict)
            and isinstance(paragraph.get("list"), dict)
            and paragraph["list"].get("is_list") is True
        ]
        if not list_indexes:
            continue
        element_id = item.get("element_id")
        if not isinstance(element_id, str) or not element_id:
            raise NormalizeError(
                "NORMALIZE_SPEC_INVALID",
                f"modules.typography.items[{item_index}].element_id",
                "native-list typography item requires a non-empty element_id",
            )
        for paragraph_index in list_indexes:
            _validate_list_style(
                paragraphs[paragraph_index]["list"],
                f"modules.typography.items[{item_index}].paragraphs[{paragraph_index}].list",
            )
        targets.append(
            {
                "element_id": element_id,
                "paragraphs": paragraphs,
                "list_indexes": list_indexes,
            }
        )
    return spec, targets


def _validate_list_style(contract: Any, path: str) -> None:
    if not isinstance(contract, dict):
        raise NormalizeError(
            "NORMALIZE_LIST_STYLE_INVALID",
            path,
            "list contract must be an object",
        )
    font = contract.get("bullet_font")
    size_mode = contract.get("bullet_size_mode")
    size_value = contract.get("bullet_size_value")
    color = contract.get("bullet_color")
    valid = isinstance(font, str) and bool(font.strip())
    valid = valid and size_mode in {"follow_text", "percent", "points"}
    valid = valid and (
        (size_mode == "follow_text" and size_value is None)
        or (size_mode in {"percent", "points"} and _positive_number(size_value))
    )
    valid = valid and (
        color == "follow_text"
        or (isinstance(color, str) and RGB_PATTERN.fullmatch(color) is not None)
    )
    if not valid:
        raise NormalizeError(
            "NORMALIZE_LIST_STYLE_INVALID",
            path,
            "bullet font, size, or color contract is invalid",
        )


def _shape_targets(slide_root: ET.Element) -> dict[str, list[ET.Element]]:
    targets: dict[str, list[ET.Element]] = {}
    for shape in slide_root.findall(".//p:sp", NS):
        identity = shape.find("p:nvSpPr/p:cNvPr", NS)
        text_body = shape.find("p:txBody", NS)
        name = identity.get("name") if identity is not None else None
        if isinstance(name, str) and name and text_body is not None:
            targets.setdefault(name, []).append(text_body)
    return targets


def _style_repr(elements: list[ET.Element]) -> str:
    if not elements:
        return "missing"
    rendered: list[str] = []
    for element in elements:
        name = _local_name(element)
        if name == "buFontTx":
            rendered.append("buFontTx")
        elif name == "buFont":
            rendered.append(f"buFont:{element.get('typeface', '')}")
        elif name == "buSzTx":
            rendered.append("buSzTx")
        elif name in {"buSzPct", "buSzPts"}:
            rendered.append(f"{name}:{element.get('val', '')}")
        elif name == "buClrTx":
            rendered.append("buClrTx")
        elif name == "buClr":
            rgb = element.find("a:srgbClr", NS)
            rendered.append(f"buClr:#{rgb.get('val', '') if rgb is not None else ''}")
    return ",".join(rendered)


def _desired_style(
    contract: dict[str, Any],
) -> tuple[list[ET.Element], dict[str, str]]:
    color_value = contract["bullet_color"]
    if color_value == "follow_text":
        color = ET.Element(_qualified("buClrTx"))
        color_repr = "buClrTx"
    else:
        color = ET.Element(_qualified("buClr"))
        rgb = ET.SubElement(color, _qualified("srgbClr"))
        rgb.set("val", color_value[1:].upper())
        color_repr = f"buClr:{color_value.upper()}"

    size_mode = contract["bullet_size_mode"]
    if size_mode == "follow_text":
        size = ET.Element(_qualified("buSzTx"))
        size_repr = "buSzTx"
    elif size_mode == "percent":
        size = ET.Element(_qualified("buSzPct"))
        size.set("val", str(round(float(contract["bullet_size_value"]) * 1000)))
        size_repr = f"buSzPct:{size.get('val')}"
    else:
        size = ET.Element(_qualified("buSzPts"))
        size.set("val", str(round(float(contract["bullet_size_value"]) * 100)))
        size_repr = f"buSzPts:{size.get('val')}"

    font_value = contract["bullet_font"]
    if font_value == "follow_text":
        font = ET.Element(_qualified("buFontTx"))
        font_repr = "buFontTx"
    else:
        font = ET.Element(_qualified("buFont"))
        font.set("typeface", font_value)
        font_repr = f"buFont:{font_value}"

    return [color, size, font], {
        "bullet_color": color_repr,
        "bullet_size": size_repr,
        "bullet_font": font_repr,
    }


def _normalize_paragraph(
    p_pr: ET.Element,
    contract: dict[str, Any],
    *,
    element_id: str,
    paragraph_index: int,
) -> list[dict[str, Any]]:
    children = list(p_pr)
    identities = [
        child for child in children if _local_name(child) in IDENTITY_TAGS
    ]
    if len(identities) != 1:
        raise NormalizeError(
            "NORMALIZE_BULLET_IDENTITY_INVALID",
            f"{element_id}.paragraphs[{paragraph_index}]",
            "native-list paragraph requires exactly one local bullet identity node",
        )
    identity = identities[0]
    identity_index = children.index(identity)
    style_elements = [
        child for child in children if _local_name(child) in STYLE_TAGS
    ]
    before_groups = {
        "bullet_color": [
            child
            for child in style_elements
            if _local_name(child) in {"buClrTx", "buClr"}
        ],
        "bullet_size": [
            child
            for child in style_elements
            if _local_name(child) in {"buSzTx", "buSzPct", "buSzPts"}
        ],
        "bullet_font": [
            child
            for child in style_elements
            if _local_name(child) in {"buFontTx", "buFont"}
        ],
    }
    desired_elements, desired_repr = _desired_style(contract)
    before_repr = {
        field: _style_repr(elements) for field, elements in before_groups.items()
    }
    current_order = [
        _local_name(child)
        for child in children
        if child in style_elements or child is identity
    ]
    desired_order = [
        _local_name(desired_elements[0]),
        _local_name(desired_elements[1]),
        _local_name(desired_elements[2]),
        _local_name(identity),
    ]
    semantic_match = all(
        before_repr[field] == desired_repr[field] for field in desired_repr
    )
    unique_match = all(len(before_groups[field]) == 1 for field in before_groups)
    if semantic_match and unique_match and current_order == desired_order:
        return []

    changes = [
        {
            "element_id": element_id,
            "paragraph_index": paragraph_index,
            "field": field,
            "before": before_repr[field],
            "after": desired_repr[field],
        }
        for field in ("bullet_color", "bullet_size", "bullet_font")
        if before_repr[field] != desired_repr[field]
        or len(before_groups[field]) != 1
    ]
    if not changes and current_order != desired_order:
        changes.append(
            {
                "element_id": element_id,
                "paragraph_index": paragraph_index,
                "field": "bullet_style_order",
                "before": ",".join(current_order),
                "after": ",".join(desired_order),
            }
        )

    for child in style_elements:
        p_pr.remove(child)
    identity_index = list(p_pr).index(identity)
    for offset, element in enumerate(desired_elements):
        p_pr.insert(identity_index + offset, element)
    return changes


def _serialize_xml(root: ET.Element) -> bytes:
    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    )


def _write_pptx(
    source: Path,
    destination: Path,
    replacements: dict[str, bytes],
) -> None:
    if not replacements:
        shutil.copyfile(source, destination)
        return
    try:
        with zipfile.ZipFile(source, "r") as archive_in:
            with zipfile.ZipFile(destination, "w") as archive_out:
                for info in archive_in.infolist():
                    payload = replacements.get(info.filename, archive_in.read(info.filename))
                    archive_out.writestr(info, payload)
        with zipfile.ZipFile(destination, "r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise NormalizeError(
                    "NORMALIZE_OUTPUT_INVALID",
                    str(destination),
                    f"corrupt ZIP member: {bad_member}",
                )
    except NormalizeError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise NormalizeError(
            "NORMALIZE_OUTPUT_INVALID",
            str(destination),
            str(exc),
        ) from exc


def normalize_pptx(
    input_pptx: Path,
    spec_path: Path,
    output_pptx: Path,
    report_path: Path,
) -> dict[str, Any]:
    input_pptx = input_pptx.expanduser().resolve()
    spec_path = spec_path.expanduser().resolve()
    output_pptx = output_pptx.expanduser().resolve()
    report_path = report_path.expanduser().resolve()
    if not input_pptx.is_file():
        raise NormalizeError(
            "NORMALIZE_INPUT_INVALID",
            str(input_pptx),
            "input PPTX does not exist",
        )
    if input_pptx == output_pptx or output_pptx == report_path:
        raise NormalizeError(
            "NORMALIZE_INPUT_INVALID",
            str(output_pptx),
            "input, output, and report paths must be distinct",
        )
    if output_pptx.exists() or report_path.exists():
        raise NormalizeError(
            "NORMALIZE_OUTPUT_EXISTS",
            str(output_pptx if output_pptx.exists() else report_path),
            "output and report paths must not already exist",
        )

    spec, targets = _load_spec(spec_path)
    del spec
    try:
        with zipfile.ZipFile(input_pptx, "r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise NormalizeError(
                    "NORMALIZE_INPUT_INVALID",
                    str(input_pptx),
                    f"corrupt ZIP member: {bad_member}",
                )
            slide_parts = [
                name for name in archive.namelist() if SLIDE_PART_PATTERN.fullmatch(name)
            ]
            if not slide_parts:
                raise NormalizeError(
                    "NORMALIZE_INPUT_INVALID",
                    str(input_pptx),
                    "PPTX contains no slide XML",
                )
            roots = {
                part: ET.fromstring(archive.read(part)) for part in slide_parts
            }
    except NormalizeError:
        raise
    except (OSError, ET.ParseError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise NormalizeError(
            "NORMALIZE_INPUT_INVALID",
            str(input_pptx),
            str(exc),
        ) from exc

    name_index: dict[str, list[tuple[str, ET.Element]]] = {}
    for part, root in roots.items():
        for name, text_bodies in _shape_targets(root).items():
            for text_body in text_bodies:
                name_index.setdefault(name, []).append((part, text_body))

    changes: list[dict[str, Any]] = []
    changed_parts: set[str] = set()
    paragraphs_checked = 0
    for target in targets:
        element_id = target["element_id"]
        matches = [
            value
            for name, values in name_index.items()
            if name == f"ia:{element_id}" or name.startswith(f"ia:{element_id}:")
            for value in values
        ]
        if not matches:
            raise NormalizeError(
                "NORMALIZE_TEXTBOX_MISSING",
                element_id,
                "cannot find the named native-list TextBox",
            )
        if len(matches) != 1:
            raise NormalizeError(
                "NORMALIZE_TEXTBOX_AMBIGUOUS",
                element_id,
                f"expected one named TextBox, got {len(matches)}",
            )
        part, text_body = matches[0]
        paragraphs = text_body.findall("a:p", NS)
        expected_paragraphs = target["paragraphs"]
        if len(paragraphs) != len(expected_paragraphs):
            raise NormalizeError(
                "NORMALIZE_PARAGRAPH_COUNT_MISMATCH",
                element_id,
                f"expected {len(expected_paragraphs)} paragraphs, got {len(paragraphs)}",
            )
        for paragraph_index in target["list_indexes"]:
            paragraphs_checked += 1
            p_pr = paragraphs[paragraph_index].find("a:pPr", NS)
            if p_pr is None:
                raise NormalizeError(
                    "NORMALIZE_BULLET_IDENTITY_INVALID",
                    f"{element_id}.paragraphs[{paragraph_index}]",
                    "native-list paragraph has no local pPr",
                )
            paragraph_changes = _normalize_paragraph(
                p_pr,
                expected_paragraphs[paragraph_index]["list"],
                element_id=element_id,
                paragraph_index=paragraph_index,
            )
            if paragraph_changes:
                changed_parts.add(part)
                changes.extend(paragraph_changes)

    replacements = {
        part: _serialize_xml(roots[part]) for part in changed_parts
    }
    output_pptx.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output_handle = tempfile.NamedTemporaryFile(
        dir=output_pptx.parent,
        prefix=f".{output_pptx.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_output = Path(temp_output_handle.name)
    temp_output_handle.close()
    temp_output.unlink()
    temp_report: Path | None = None
    try:
        _write_pptx(input_pptx, temp_output, replacements)
        changed_paragraphs = {
            (change["element_id"], change["paragraph_index"]) for change in changes
        }
        report = {
            "schema_version": 1,
            "valid": True,
            "input": {"path": str(input_pptx), "sha256": _sha256(input_pptx)},
            "output": {"path": str(output_pptx), "sha256": _sha256(temp_output)},
            "spec": {"path": str(spec_path), "sha256": _sha256(spec_path)},
            "textboxes_checked": len(targets),
            "paragraphs_checked": paragraphs_checked,
            "paragraphs_changed": len(changed_paragraphs),
            "changes": changes,
            "errors": [],
        }
        report_handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=report_path.parent,
            prefix=f".{report_path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp_report = Path(report_handle.name)
        with report_handle:
            json.dump(report, report_handle, ensure_ascii=False, indent=2)
            report_handle.write("\n")
            report_handle.flush()
            os.fsync(report_handle.fileno())
        os.replace(temp_output, output_pptx)
        try:
            os.replace(temp_report, report_path)
        except BaseException:
            output_pptx.unlink(missing_ok=True)
            raise
        return report
    except BaseException:
        temp_output.unlink(missing_ok=True)
        if temp_report is not None:
            temp_report.unlink(missing_ok=True)
        raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = normalize_pptx(
            args.pptx,
            args.spec,
            args.output,
            args.report,
        )
    except NormalizeError as exc:
        report = {
            "schema_version": 1,
            "valid": False,
            "errors": [
                {"code": exc.code, "path": exc.path, "detail": exc.detail}
            ],
        }
        report_path = args.report.expanduser().resolve()
        if not report_path.exists():
            try:
                _atomic_write_json(report_path, report)
            except OSError:
                pass
        print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
