#!/usr/bin/env python3
"""Validate PPTX structure, widescreen layout, and basic editability."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import posixpath
import re
import tempfile
import warnings
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from PIL import Image


NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}
RID = f"{{{NS['r']}}}id"
REMBED = f"{{{NS['r']}}}embed"
REQUIRED_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "ppt/presentation.xml",
    "ppt/_rels/presentation.xml.rels",
}
SLIDE_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
)
MAX_ZIP_MEMBERS = 2048
MAX_MEMBER_UNCOMPRESSED = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_XML_BYTES = 10 * 1024 * 1024
MAX_MEDIA_BYTES = 50 * 1024 * 1024
MAX_MEDIA_DIMENSION = 32768
MAX_MEDIA_PIXELS = 100_000_000
MAX_MEDIA_RGBA_BYTES = 400_000_000
STRICT_TO_TRANSITIONAL = {
    "http://purl.oclc.org/ooxml/presentationml/main": NS["p"],
    "http://purl.oclc.org/ooxml/drawingml/main": NS["a"],
    "http://purl.oclc.org/ooxml/officeDocument/relationships": NS["r"],
    "http://purl.oclc.org/ooxml/package/relationships": NS["pr"],
}


class ValidationError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _result(path: Path) -> dict[str, Any]:
    return {
        "path": str(Path(path).expanduser().resolve()),
        "pptx_sha256": None,
        "valid": False,
        "errors": [],
        "warnings": [],
        "slide_count": 0,
        "width_emu": None,
        "height_emu": None,
        "aspect_ratio": None,
        "editable_object_count": 0,
        "text_shape_count": 0,
        "graphic_frame_count": 0,
        "picture_count": 0,
        "font_declarations": [],
        "font_sizes_pt": [],
        "text_runs": 0,
        "native_list_paragraphs": 0,
        "native_list_contracts_checked": 0,
        "text_objects": [],
        "native_shape_objects": [],
        "picture_objects": [],
        "structure_objects": [],
        "full_slide_picture_risk": False,
        "external_relationships": [],
        "slides": [],
    }


def _canonicalize_namespaces(root: ET.Element) -> ET.Element:
    for element in root.iter():
        if element.tag.startswith("{"):
            uri, local = element.tag[1:].split("}", 1)
            element.tag = f"{{{STRICT_TO_TRANSITIONAL.get(uri, uri)}}}{local}"
        for key, value in list(element.attrib.items()):
            if key.startswith("{"):
                uri, local = key[1:].split("}", 1)
                canonical = f"{{{STRICT_TO_TRANSITIONAL.get(uri, uri)}}}{local}"
                if canonical != key:
                    del element.attrib[key]
                    element.attrib[canonical] = value
    return root


def _xml(archive: zipfile.ZipFile, part: str) -> ET.Element:
    try:
        info = archive.getinfo(part)
        if info.file_size > MAX_XML_BYTES:
            raise ValidationError("PPTX_RESOURCE_LIMIT", f"XML part too large: {part}")
        payload = archive.read(part)
        upper = payload.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise ValidationError("XML_DTD_FORBIDDEN", f"DTD/entity forbidden: {part}")
        return _canonicalize_namespaces(ET.fromstring(payload))
    except KeyError as exc:
        raise ValidationError("PPTX_REQUIRED_PART_MISSING", f"missing XML part: {part}") from exc
    except ET.ParseError as exc:
        raise ValidationError("XML_INVALID", f"invalid XML part: {part}") from exc


def _source_part_for_rels(rels_part: str) -> str:
    if rels_part == "_rels/.rels":
        return ""
    path = PurePosixPath(rels_part)
    if path.parent.name != "_rels" or not path.name.endswith(".rels"):
        raise ValueError(f"invalid relationships part path: {rels_part}")
    source_name = path.name[: -len(".rels")]
    return str(path.parent.parent / source_name)


def _resolve_target(source_part: str, target: str) -> str:
    if not isinstance(target, str) or not target or "\\" in target:
        raise ValidationError("RELATIONSHIP_TARGET_INVALID", f"Invalid relationship Target: {target!r}")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValidationError("RELATIONSHIP_TARGET_INVALID", f"Ambiguous internal Target: {target}")
    decoded = unquote(parsed.path)
    if decoded != parsed.path and ("/" in decoded or "\\" in decoded or ".." in decoded.split("/")):
        raise ValidationError("RELATIONSHIP_TARGET_INVALID", f"Encoded ambiguous Target: {target}")
    if target.startswith("/"):
        candidate = target.lstrip("/")
    else:
        base = posixpath.dirname(source_part)
        candidate = posixpath.join(base, target)
    parts = PurePosixPath(candidate).parts
    if any(part in {"", "."} for part in parts) or candidate.startswith("/"):
        raise ValidationError("RELATIONSHIP_TARGET_INVALID", f"Target escapes package: {target}")
    normalized = posixpath.normpath(candidate)
    if normalized == ".." or normalized.startswith("../"):
        raise ValidationError("RELATIONSHIP_TARGET_INVALID", f"Target escapes package: {target}")
    return normalized


def _relationship_map(
    archive: zipfile.ZipFile, rels_part: str
) -> dict[str, tuple[str, str, bool]]:
    root = _xml(archive, rels_part)
    if root.tag != f"{{{NS['pr']}}}Relationships":
        raise ValidationError(
            "RELATIONSHIP_SEMANTICS_INVALID",
            f"Unexpected Relationships root QName in {rels_part}",
        )
    source = _source_part_for_rels(rels_part)
    relationships: dict[str, tuple[str, str, bool]] = {}
    for rel in list(root):
        if rel.tag != f"{{{NS['pr']}}}Relationship":
            raise ValidationError("RELATIONSHIP_SEMANTICS_INVALID", f"Unknown child in {rels_part}")
        relationship_id = rel.get("Id")
        target = rel.get("Target")
        relationship_type = rel.get("Type", "")
        target_mode = rel.get("TargetMode")
        if (
            not relationship_id or not target or not relationship_type
            or target_mode not in {None, "Internal", "External"}
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", relationship_id)
            or not urlsplit(relationship_type).scheme
        ):
            raise ValidationError(
                "RELATIONSHIP_SEMANTICS_INVALID",
                f"Relationship requires valid Id/Type/Target/TargetMode in {rels_part}",
            )
        external = target_mode == "External"
        if relationship_id in relationships:
            raise ValidationError("DUPLICATE_RELATIONSHIP_ID", f"Duplicate relationship Id in {rels_part}: {relationship_id}")
        resolved = target if external else _resolve_target(source, target)
        relationships[relationship_id] = (resolved, relationship_type, external)
    return relationships


def _slide_rels_part(slide_part: str) -> str:
    path = PurePosixPath(slide_part)
    return str(path.parent / "_rels" / f"{path.name}.rels")


def _int_attr(element: ET.Element | None, name: str) -> int | None:
    if element is None:
        return None
    try:
        return int(element.get(name, ""))
    except ValueError:
        return None




def _archive_sha256(archive: zipfile.ZipFile, part: str) -> str:
    digest = hashlib.sha256()
    with archive.open(part) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _font_properties(slide: ET.Element) -> list[ET.Element]:
    properties: list[ET.Element] = []
    for tag in ("a:rPr", "a:defRPr", "a:endParaRPr"):
        properties.extend(slide.findall(f".//{tag}", NS))
    return properties


def _declared_fonts(properties: list[ET.Element]) -> set[str]:
    fonts: set[str] = set()
    for prop in properties:
        typeface = prop.get("typeface")
        if typeface:
            fonts.add(typeface)
        for child_name in ("latin", "ea", "cs", "sym"):
            child = prop.find(f"a:{child_name}", NS)
            if child is not None and child.get("typeface"):
                fonts.add(child.get("typeface", ""))
    return fonts


def _declared_font_sizes(properties: list[ET.Element]) -> set[float]:
    sizes: set[float] = set()
    for prop in properties:
        size = _int_attr(prop, "sz")
        if size is not None and size > 0:
            sizes.add(size / 100)
    return sizes


def _geometry(element: ET.Element, path: str) -> tuple[int | None, int | None, int | None, int | None]:
    transform = element.find(path, NS)
    offset = transform.find("a:off", NS) if transform is not None else None
    extent = transform.find("a:ext", NS) if transform is not None else None
    return (
        _int_attr(offset, "x"), _int_attr(offset, "y"),
        _int_attr(extent, "cx"), _int_attr(extent, "cy"),
    )


def _object_identity(element: ET.Element, path: str) -> tuple[str | None, str | None, bool]:
    non_visual = element.find(path, NS)
    if non_visual is None:
        return None, None, False
    return non_visual.get("id"), non_visual.get("name"), non_visual.get("hidden") in {"1", "true"}


def _intersects_slide(x: int | None, y: int | None, cx: int | None, cy: int | None,
                      width: int, height: int) -> bool:
    return (
        None not in {x, y, cx, cy}
        and cx > 0 and cy > 0
        and x < width and y < height and x + cx > 0 and y + cy > 0
    )


def _transform_bbox(
    bbox: tuple[int | None, int | None, int | None, int | None],
    transform: tuple[float, float, float, float],
) -> tuple[int | None, int | None, int | None, int | None]:
    x, y, cx, cy = bbox
    if None in {x, y, cx, cy}:
        return None, None, None, None
    sx, sy, tx, ty = transform
    return (
        round(tx + sx * x), round(ty + sy * y),
        round(abs(sx) * cx), round(abs(sy) * cy),
    )


def _group_child_transform(
    group: ET.Element,
    parent: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    xfrm = group.find("p:grpSpPr/a:xfrm", NS)
    off = xfrm.find("a:off", NS) if xfrm is not None else None
    ext = xfrm.find("a:ext", NS) if xfrm is not None else None
    child_off = xfrm.find("a:chOff", NS) if xfrm is not None else None
    child_ext = xfrm.find("a:chExt", NS) if xfrm is not None else None
    values = (
        _int_attr(off, "x"), _int_attr(off, "y"),
        _int_attr(ext, "cx"), _int_attr(ext, "cy"),
        _int_attr(child_off, "x"), _int_attr(child_off, "y"),
        _int_attr(child_ext, "cx"), _int_attr(child_ext, "cy"),
    )
    if None in values or values[6] == 0 or values[7] == 0:
        return None
    x, y, cx, cy, chx, chy, chcx, chcy = values
    psx, psy, ptx, pty = parent
    local_sx, local_sy = cx / chcx, cy / chcy
    return (
        psx * local_sx,
        psy * local_sy,
        ptx + psx * (x - chx * local_sx),
        pty + psy * (y - chy * local_sy),
    )


def _collect_visible_objects(
    nodes: list[ET.Element],
    slide_part: str,
    width: int,
    height: int,
    inheritance: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return every visible presentation object in slide-space preorder."""
    records: list[dict[str, Any]] = []
    layer = 0
    identity_paths = {
        "sp": "p:nvSpPr/p:cNvPr",
        "pic": "p:nvPicPr/p:cNvPr",
        "graphicFrame": "p:nvGraphicFramePr/p:cNvPr",
        "grpSp": "p:nvGrpSpPr/p:cNvPr",
        "cxnSp": "p:nvCxnSpPr/p:cNvPr",
    }
    geometry_paths = {
        "sp": "p:spPr/a:xfrm",
        "pic": "p:spPr/a:xfrm",
        "graphicFrame": "p:xfrm",
        "grpSp": "p:grpSpPr/a:xfrm",
        "cxnSp": "p:spPr/a:xfrm",
    }

    def visit(node: ET.Element, transform: tuple[float, float, float, float], parent_hidden: bool) -> None:
        nonlocal layer
        kind = node.tag.rsplit("}", 1)[-1]
        if kind not in identity_paths:
            return
        layer += 1
        identity = node.find(identity_paths[kind], NS)
        hidden = parent_hidden or (
            identity is not None and identity.get("hidden") in {"1", "true"}
        )
        local_bbox = _geometry(node, geometry_paths[kind])
        if kind == "sp" and None in local_bbox:
            inherited_bbox = _inherited_placeholder_geometry(node, inheritance or {})
            if None not in inherited_bbox:
                local_bbox = inherited_bbox
        bbox = _transform_bbox(local_bbox, transform)
        geometry_known = None not in bbox
        visible = False if hidden else (
            _intersects_slide(*bbox, width, height) if geometry_known else None
        )
        record = {
            "slide_part": slide_part,
            "object_type": kind,
            "object_id": identity.get("id") if identity is not None else None,
            "object_name": identity.get("name") if identity is not None else None,
            "layer": layer,
            "hidden": hidden,
            "x": bbox[0], "y": bbox[1], "cx": bbox[2], "cy": bbox[3],
            "geometry_known": geometry_known,
            "visible": visible,
            "has_text": kind == "sp" and bool(node.findall(".//a:t", NS)),
            "_element": node,
        }
        records.append(record)
        if kind == "grpSp":
            child_transform = _group_child_transform(node, transform)
            for child in list(node):
                if child.tag.rsplit("}", 1)[-1] in identity_paths:
                    visit(child, child_transform or transform, hidden or child_transform is None)

    for node in nodes:
        visit(node, (1.0, 1.0, 0.0, 0.0), False)
    return records


def _round_rect_adjustment(shape: ET.Element) -> tuple[str | None, int | None]:
    geometry = shape.find("p:spPr/a:prstGeom", NS)
    if geometry is None or geometry.get("prst") != "roundRect":
        return None, None
    adjustment = geometry.find("a:avLst/a:gd[@name='adj']", NS)
    if adjustment is None:
        return "missing", None
    match = re.fullmatch(r"val\s+(-?\d+)", adjustment.get("fmla", "").strip())
    if match is None:
        return "invalid", None
    value = int(match.group(1))
    if not 1 <= value <= 50_000:
        return "invalid", value
    return "valid", value


def _scripts_in_text(text: str) -> list[str]:
    scripts: list[str] = []
    for char in text:
        value = ord(char)
        if (
            0x2E80 <= value <= 0x9FFF
            or 0xAC00 <= value <= 0xD7AF
            or 0xF900 <= value <= 0xFAFF
            or 0x3040 <= value <= 0x30FF
            or 0x3100 <= value <= 0x312F
            or 0xFF00 <= value <= 0xFFEF
        ):
            script = "ea"
        elif (
            0x0590 <= value <= 0x08FF
            or 0x0700 <= value <= 0x074F
            or 0x0780 <= value <= 0x07BF
            or 0xFB1D <= value <= 0xFDFF
            or 0xFE70 <= value <= 0xFEFF
        ):
            script = "cs"
        else:
            script = "latin"
        if script not in scripts and (char.isalnum() or script != "latin"):
            scripts.append(script)
    return scripts or ["latin"]


def _font_from_properties(
    prop: ET.Element | None,
    text: str,
    script: str | None = None,
    *,
    allow_generic: bool = True,
) -> str | None:
    if prop is None:
        return None
    preferred = script or _scripts_in_text(text)[0]
    child = prop.find(f"a:{preferred}", NS)
    if child is not None and child.get("typeface"):
        return child.get("typeface")
    return prop.get("typeface") if allow_generic else None


def _resolve_theme_font(value: str | None, theme_fonts: dict[str, str]) -> str | None:
    if value is None:
        return None
    return theme_fonts.get(value, value)


def _font_from_chain(
    properties: list[ET.Element | None], text: str, theme_fonts: dict[str, str], script: str | None = None
) -> str | None:
    for prop in properties:
        value = _font_from_properties(prop, text, script, allow_generic=False)
        if value:
            return _resolve_theme_font(value, theme_fonts)
    for prop in properties:
        value = prop.get("typeface") if prop is not None else None
        if value:
            return _resolve_theme_font(value, theme_fonts)
    preferred = {"ea": "+mj-ea", "cs": "+mj-cs"}.get(script or _scripts_in_text(text)[0], "+mj-lt")
    return theme_fonts.get(preferred)


def _chain_int(properties: list[ET.Element | None], name: str) -> int | None:
    for prop in properties:
        value = _int_attr(prop, name)
        if value is not None:
            return value
    return None


def _chain_attr(properties: list[ET.Element | None], name: str) -> str | None:
    for prop in properties:
        if prop is not None and prop.get(name) is not None:
            return prop.get(name)
    return None


def _run_color(prop: ET.Element | None) -> str | None:
    if prop is None:
        return None
    color = prop.find("a:solidFill/a:srgbClr", NS)
    return f"#{color.get('val')}" if color is not None and color.get("val") else None


def _paragraph_spacing(
    properties: list[ET.Element | None], tag: str
) -> float | None:
    for ppr in properties:
        if ppr is None:
            continue
        holder = ppr.find(f"a:{tag}", NS)
        pct = holder.find("a:spcPct", NS) if holder is not None else None
        pts = holder.find("a:spcPts", NS) if holder is not None else None
        if pct is not None:
            value = _int_attr(pct, "val")
            return value / 100000 if value is not None else None
        if pts is not None:
            value = _int_attr(pts, "val")
            return value / 100 if value is not None else None
    return None


def _native_bullet_contract(
    properties: list[ET.Element | None],
    level: int,
) -> dict[str, Any]:
    bullet_type = None
    bullet = None
    for owner in properties:
        if owner is None:
            continue
        if owner.find("a:buNone", NS) is not None:
            return {"is_list": False, "level": level, "bullet": None}
        char = owner.find("a:buChar", NS)
        auto = owner.find("a:buAutoNum", NS)
        blip = owner.find("a:buBlip", NS)
        if char is not None:
            bullet_type, bullet = "char", char.get("char")
        elif auto is not None:
            bullet_type, bullet = "auto_number", auto.get("type")
        elif blip is not None:
            bullet_type, bullet = "picture", "blip"
        if bullet_type is not None:
            break
    if bullet_type is None:
        return {"is_list": False, "level": level, "bullet": None}

    bullet_font = "follow_text"
    for owner in properties:
        if owner is None:
            continue
        if owner.find("a:buFontTx", NS) is not None:
            break
        font = owner.find("a:buFont", NS)
        if font is not None and font.get("typeface"):
            bullet_font = font.get("typeface", "")
            break

    bullet_size_mode = "follow_text"
    bullet_size_value: float | None = None
    for owner in properties:
        if owner is None:
            continue
        if owner.find("a:buSzTx", NS) is not None:
            break
        percent = owner.find("a:buSzPct", NS)
        points = owner.find("a:buSzPts", NS)
        if percent is not None:
            raw = _int_attr(percent, "val")
            if raw is not None:
                bullet_size_mode, bullet_size_value = "percent", raw / 1000
            break
        if points is not None:
            raw = _int_attr(points, "val")
            if raw is not None:
                bullet_size_mode, bullet_size_value = "points", raw / 100
            break

    bullet_color = "follow_text"
    for owner in properties:
        if owner is None:
            continue
        if owner.find("a:buClrTx", NS) is not None:
            break
        holder = owner.find("a:buClr", NS)
        if holder is None:
            continue
        rgb = holder.find("a:srgbClr", NS)
        scheme = holder.find("a:schemeClr", NS)
        if rgb is not None and rgb.get("val"):
            bullet_color = f"#{rgb.get('val')}"
        elif scheme is not None and scheme.get("val"):
            bullet_color = f"scheme:{scheme.get('val')}"
        break

    return {
        "is_list": True,
        "level": level,
        "bullet_type": bullet_type,
        "bullet": bullet,
        "bullet_font": bullet_font,
        "bullet_size_mode": bullet_size_mode,
        "bullet_size_value": bullet_size_value,
        "bullet_color": bullet_color,
    }


def _alignment(value: str | None) -> str | None:
    return {
        "l": "left", "ctr": "center", "r": "right",
        "just": "justify", "justLow": "justify", "dist": "distributed",
    }.get(value, value)


def _vertical_alignment(value: str | None) -> str | None:
    return {"t": "top", "ctr": "middle", "b": "bottom"}.get(value, value)


def _text_object(
    shape: ET.Element,
    slide_part: str,
    layer: int,
    inheritance: dict[str, Any] | None = None,
    theme_fonts: dict[str, str] | None = None,
    slide_bbox: tuple[int | None, int | None, int | None, int | None] | None = None,
) -> dict[str, Any]:
    theme_fonts = theme_fonts or {}
    inheritance = inheritance or {}
    object_id, name, hidden = _object_identity(shape, "p:nvSpPr/p:cNvPr")
    x, y, cx, cy = slide_bbox or _geometry(shape, "p:spPr/a:xfrm")
    body = shape.find("p:txBody", NS)
    body_pr = body.find("a:bodyPr", NS) if body is not None else None
    body_properties = [body_pr, *_inherited_body_properties(shape, inheritance)]
    paragraphs_out: list[dict[str, Any]] = []
    runs_out: list[dict[str, Any]] = []
    text_parts: list[str] = []
    soft_breaks: list[int] = []
    cursor = 0
    list_style = body.find("a:lstStyle", NS) if body is not None else None
    for paragraph in body.findall("a:p", NS) if body is not None else []:
        p_start = cursor
        ppr = paragraph.find("a:pPr", NS)
        level = _int_attr(ppr, "lvl") or 0
        fallback_pprs = _inherited_paragraph_properties(shape, level, inheritance)
        fallback_rprs = [
            fallback.find("a:defRPr", NS) if fallback is not None else None
            for fallback in fallback_pprs
        ]
        inherited_ppr = (
            list_style.find(f"a:lvl{level + 1}pPr", NS) if list_style is not None else None
        )
        paragraph_properties = [ppr, inherited_ppr, *fallback_pprs]
        default_rprs = [
            ppr.find("a:defRPr", NS) if ppr is not None else None,
            inherited_ppr.find("a:defRPr", NS) if inherited_ppr is not None else None,
        ]
        for child in list(paragraph):
            local = child.tag.rsplit("}", 1)[-1]
            if local == "br":
                soft_breaks.append(cursor)
                continue
            if local not in {"r", "fld"}:
                continue
            text_node = child.find("a:t", NS)
            text = text_node.text if text_node is not None and text_node.text is not None else ""
            prop = child.find("a:rPr", NS)
            properties = [prop, *default_rprs, *fallback_rprs]
            start = cursor
            cursor += len(text)
            text_parts.append(text)
            size = _chain_int(properties, "sz")
            bold = _chain_attr(properties, "b")
            underline = _chain_attr(properties, "u")
            spacing = _chain_int(properties, "spc")
            color = next(
                (
                    resolved for candidate in properties
                    if (resolved := _run_color(candidate)) is not None
                ),
                None,
            )
            fonts_by_script = {
                script: _font_from_chain(properties, text, theme_fonts, script)
                for script in _scripts_in_text(text)
            }
            runs_out.append({
                "start": start, "end": cursor, "text": text,
                "font": fonts_by_script[_scripts_in_text(text)[0]],
                "fonts_by_script": fonts_by_script,
                "font_size": size / 100 if size else None,
                "font_weight": 700 if bold in {"1", "true"} else 400,
                "color": color,
                "decoration": "underline" if underline not in {None, "none"} else "none",
                "letter_spacing": spacing / 100 if spacing is not None else None,
            })
        list_contract = _native_bullet_contract(paragraph_properties, level)
        paragraphs_out.append({
            "start": p_start, "end": cursor,
            "alignment": _alignment(_chain_attr(paragraph_properties, "algn")),
            "line_spacing": _paragraph_spacing(paragraph_properties, "lnSpc"),
            "space_before": _paragraph_spacing(paragraph_properties, "spcBef"),
            "space_after": _paragraph_spacing(paragraph_properties, "spcAft"),
            "margin_left": _chain_int(paragraph_properties, "marL"),
            "indent": _chain_int(paragraph_properties, "indent"),
            "list": list_contract,
        })
    paragraph_breaks = [paragraph["end"] for paragraph in paragraphs_out[:-1]]
    horizontal = paragraphs_out[0]["alignment"] if paragraphs_out else "left"
    margins = {
        "left": _chain_int(body_properties, "lIns"),
        "right": _chain_int(body_properties, "rIns"),
        "top": _chain_int(body_properties, "tIns"),
        "bottom": _chain_int(body_properties, "bIns"),
    }
    anchor = _chain_attr(body_properties, "anchor")
    wrap_value = _chain_attr(body_properties, "wrap")
    vertical_overflow = _chain_attr(body_properties, "vertOverflow")
    horizontal_overflow = _chain_attr(body_properties, "horzOverflow")
    overflow = (
        None
        if vertical_overflow is None and horizontal_overflow is None
        else "overflow" in {vertical_overflow, horizontal_overflow}
    )
    return {
        "slide_part": slide_part, "object_id": object_id, "object_name": name,
        "layer": layer, "hidden": hidden, "x": x, "y": y, "cx": cx, "cy": cy,
        "text": "".join(text_parts), "paragraphs": paragraphs_out, "runs": runs_out,
        "text_box": {
            "margins": margins,
            "alignment": horizontal,
            "vertical_alignment": _vertical_alignment(anchor),
            "wrap": None if wrap_value is None else wrap_value != "none",
            "overflow": overflow,
            "soft_breaks": soft_breaks,
            "paragraph_breaks": paragraph_breaks,
        },
    }


def _load_reconstruction_spec(value: dict[str, Any] | Path | str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValidationError("RECONSTRUCTION_SPEC_INVALID", f"spec not found: {path}")
    if path.stat().st_size > MAX_XML_BYTES:
        raise ValidationError("RECONSTRUCTION_SPEC_INVALID", f"spec too large: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("RECONSTRUCTION_SPEC_INVALID", f"invalid spec: {path}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("RECONSTRUCTION_SPEC_INVALID", "spec root must be an object")
    return payload


def _bound_element_id(name: Any, element_ids: set[str]) -> str | None:
    if not isinstance(name, str):
        return None
    matches = [
        element_id
        for element_id in element_ids
        if name == f"ia:{element_id}" or name.startswith(f"ia:{element_id}:")
    ]
    return max(matches, key=len) if matches else None


def _bbox_matches_element(
    record: dict[str, Any],
    expected: Any,
    width: int,
    height: int,
) -> bool:
    if not isinstance(expected, list) or len(expected) != 4:
        return True
    actual = [record.get(key) for key in ("x", "y", "cx", "cy")]
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in actual + expected
    ):
        return False
    return all(
        abs(left - right) / scale <= 0.01
        for left, right, scale in zip(actual, expected, (width, height, width, height))
    )


def _expected_media_sha256(value: Any, element_id: str) -> str | None:
    if isinstance(value, dict):
        if value.get("element_id") == element_id:
            for key in ("asset_sha256", "source_sha256", "sha256"):
                digest = value.get(key)
                if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                    return digest.lower()
        for child in value.values():
            found = _expected_media_sha256(child, element_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _expected_media_sha256(child, element_id)
            if found:
                return found
    return None


def _validate_element_bindings(
    result: dict[str, Any],
    spec: dict[str, Any],
    width: int,
    height: int,
) -> None:
    elements = spec.get("elements")
    if not isinstance(elements, list) or not elements:
        return
    element_map = {
        item.get("element_id"): item
        for item in elements
        if isinstance(item, dict) and isinstance(item.get("element_id"), str)
    }
    element_ids = set(element_map)
    structures = [
        item
        for item in result.get("structure_objects", [])
        if item.get("visible") is True
        and item.get("geometry_known") is True
        and _bound_element_id(item.get("object_name"), element_ids) is not None
    ]
    text_objects = [
        item
        for item in result.get("text_objects", [])
        if item.get("visible") is True
        and _bound_element_id(item.get("object_name"), element_ids) is not None
    ]
    pictures = [
        item
        for item in result.get("picture_objects", [])
        if item.get("visible") is True
        and item.get("geometry_known") is True
        and _bound_element_id(item.get("object_name"), element_ids) is not None
    ]
    type_map = {
        "text": {"sp"},
        "special_text": {"sp"},
        "icon": {"pic"},
        "picture": {"pic"},
        "shape": {"sp"},
        "status": {"sp"},
        "line": {"cxnSp", "sp"},
        "table": {"graphicFrame", "sp"},
        "matrix": {"graphicFrame", "sp"},
        "chart": {"graphicFrame", "sp"},
        "diagram": {"graphicFrame", "sp"},
    }
    for element_id, element in element_map.items():
        candidates = [
            item
            for item in structures
            if _bound_element_id(item.get("object_name"), element_ids) == element_id
        ]
        if not candidates:
            result["errors"].append("ELEMENT_OBJECT_MISSING")
            result["warnings"].append(f"{element_id}: no visible object named ia:{element_id}[:part]")
            continue
        kind = element.get("kind")
        allowed_types = type_map.get(kind)
        if allowed_types and not any(item.get("object_type") in allowed_types for item in candidates):
            result["errors"].append("ELEMENT_OBJECT_TYPE_MISMATCH")
            result["warnings"].append(f"{element_id}: bound object type does not match {kind}")
        if not any(
            _bbox_matches_element(item, element.get("slide_bbox"), width, height)
            for item in candidates
        ):
            result["errors"].append("ELEMENT_BBOX_MISMATCH")
            result["warnings"].append(f"{element_id}: bound object bbox does not match the spec")
        if kind in {"text", "special_text"}:
            expected_text = element.get("content", {}).get("text")
            bound_text = [
                item
                for item in text_objects
                if _bound_element_id(item.get("object_name"), element_ids) == element_id
            ]
            if not bound_text:
                result["errors"].append("ELEMENT_OBJECT_TYPE_MISMATCH")
            elif isinstance(expected_text, str) and not any(
                item.get("text") == expected_text for item in bound_text
            ):
                result["errors"].append("ELEMENT_TEXT_MISMATCH")
                result["warnings"].append(f"{element_id}: editable text differs from the spec")
        if kind in {"icon", "picture"}:
            bound_pictures = [
                item
                for item in pictures
                if _bound_element_id(item.get("object_name"), element_ids) == element_id
            ]
            if not bound_pictures:
                result["errors"].append("ELEMENT_OBJECT_TYPE_MISMATCH")
            expected_hash = _expected_media_sha256(spec.get("modules"), element_id)
            if expected_hash and not any(
                item.get("media_sha256") == expected_hash for item in bound_pictures
            ):
                result["errors"].append("ELEMENT_MEDIA_HASH_MISMATCH")
                result["warnings"].append(f"{element_id}: embedded media does not match the declared asset")
        result["element_bindings_checked"] = result.get("element_bindings_checked", 0) + 1


def _expected_native_list_items(spec: dict[str, Any]) -> list[dict[str, Any]]:
    modules = spec.get("modules")
    typography = modules.get("typography") if isinstance(modules, dict) else None
    items = typography.get("items") if isinstance(typography, dict) else None
    if not isinstance(items, list):
        return []
    return [
        item for item in items
        if isinstance(item, dict)
        and isinstance(item.get("paragraphs"), list)
        and any(
            isinstance(paragraph, dict)
            and isinstance(paragraph.get("list"), dict)
            and paragraph["list"].get("is_list") is True
            for paragraph in item["paragraphs"]
        )
    ]


def _expected_text_run_items(spec: dict[str, Any]) -> list[dict[str, Any]]:
    modules = spec.get("modules")
    typography = modules.get("typography") if isinstance(modules, dict) else None
    items = typography.get("items") if isinstance(typography, dict) else None
    if not isinstance(items, list):
        return []
    elements = spec.get("elements")
    kinds = {
        element.get("element_id"): element.get("kind")
        for element in elements
        if isinstance(element, dict) and isinstance(element.get("element_id"), str)
    } if isinstance(elements, list) else {}
    return [
        item for item in items
        if isinstance(item, dict)
        and isinstance(item.get("runs"), list)
        and bool(item["runs"])
        and (not kinds or kinds.get(item.get("element_id")) == "text")
    ]


def _text_box_matches(
    actual: dict[str, Any],
    expected: Any,
    width: int,
    height: int,
) -> bool:
    if not isinstance(expected, dict):
        return False
    actual_values = [actual.get(key) for key in ("x", "y", "cx", "cy")]
    expected_values = [expected.get(key) for key in ("x", "y", "w", "h")]
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in actual_values + expected_values):
        return False
    scales = [width, height, width, height]
    return all(abs(left - right) / scale <= 0.01 for left, right, scale in zip(actual_values, expected_values, scales))


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= 0.01
    return left == right


def _bold_vector(length: int, runs: Any) -> list[bool] | None:
    if not isinstance(runs, list) or not runs:
        return None
    values: list[bool | None] = [None] * length
    for run in runs:
        if not isinstance(run, dict):
            return None
        start, end, weight = run.get("start"), run.get("end"), run.get("font_weight")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or start < 0
            or end <= start
            or end > length
        ):
            return None
        for offset in range(start, end):
            if values[offset] is not None:
                return None
            values[offset] = weight >= 600
    if any(value is None for value in values):
        return None
    return [bool(value) for value in values]


def _validate_text_run_contracts(
    result: dict[str, Any],
    spec: dict[str, Any],
    width: int,
    height: int,
) -> None:
    used_objects: set[tuple[Any, Any]] = set()
    for item in _expected_text_run_items(spec):
        element_id = item.get("element_id", "unknown")
        candidates = [
            text_object
            for text_object in result.get("text_objects", [])
            if text_object.get("text") == item.get("text")
            and _text_box_matches(text_object, item.get("text_box"), width, height)
            and (
                text_object.get("object_name") == f"ia:{element_id}"
                or str(text_object.get("object_name", "")).startswith(f"ia:{element_id}:")
            )
        ]
        available = [
            candidate for candidate in candidates
            if (candidate.get("slide_part"), candidate.get("object_id")) not in used_objects
        ]
        if not available:
            result["errors"].append("TYPOGRAPHY_TEXTBOX_MISSING")
            result["warnings"].append(f"{element_id}: expected TextBox for typography run validation")
            continue
        if len(available) > 1:
            result["errors"].append("TYPOGRAPHY_TEXTBOX_AMBIGUOUS")
            result["warnings"].append(f"{element_id}: multiple matching TextBox objects")
            continue
        actual_object = available[0]
        used_objects.add((actual_object.get("slide_part"), actual_object.get("object_id")))
        text = item.get("text")
        if not isinstance(text, str):
            continue
        expected_bold = _bold_vector(len(text), item.get("runs"))
        actual_bold = _bold_vector(len(text), actual_object.get("runs"))
        if expected_bold is None or actual_bold is None or expected_bold != actual_bold:
            result["errors"].append("TEXT_RUN_FONT_WEIGHT_MISMATCH")
            result["warnings"].append(f"{element_id}: Text Run bold ranges do not match the reconstruction spec")
        selected_font = item.get("selected_font")
        declared_font = item.get("internal_font_declaration")
        if isinstance(selected_font, str) and selected_font:
            if declared_font != selected_font:
                result["errors"].append("TEXT_RUN_FONT_DECLARATION_MISMATCH")
                result["warnings"].append(
                    f"{element_id}: internal font declaration does not match selected_font"
                )
            actual_fonts: set[str] = set()
            for run in actual_object.get("runs", []):
                if not isinstance(run, dict):
                    continue
                fonts_by_script = run.get("fonts_by_script")
                if isinstance(fonts_by_script, dict):
                    actual_fonts.update(
                        font
                        for font in fonts_by_script.values()
                        if isinstance(font, str) and font
                    )
                font = run.get("font")
                if isinstance(font, str) and font:
                    actual_fonts.add(font)
            normalized_fonts = {font.casefold() for font in actual_fonts}
            if normalized_fonts != {selected_font.casefold()}:
                result["errors"].append("TEXT_RUN_FONT_DECLARATION_MISMATCH")
                result["warnings"].append(
                    f"{element_id}: PPTX font declaration does not match {selected_font}"
                )


def _validate_native_list_contracts(
    result: dict[str, Any],
    spec: dict[str, Any],
    width: int,
    height: int,
) -> None:
    used_objects: set[tuple[Any, Any]] = set()
    for item in _expected_native_list_items(spec):
        result["native_list_contracts_checked"] += 1
        element_id = item.get("element_id", "unknown")
        candidates = [
            text_object
            for text_object in result.get("text_objects", [])
            if text_object.get("text") == item.get("text")
            and _text_box_matches(text_object, item.get("text_box"), width, height)
            and (
                text_object.get("object_name") == f"ia:{element_id}"
                or str(text_object.get("object_name", "")).startswith(f"ia:{element_id}:")
            )
        ]
        available = [
            candidate for candidate in candidates
            if (candidate.get("slide_part"), candidate.get("object_id")) not in used_objects
        ]
        if not available:
            result["errors"].append("NATIVE_LIST_TEXTBOX_MISSING")
            result["warnings"].append(
                f"{element_id}: expected one TextBox containing the complete native list"
            )
            continue
        if len(available) > 1:
            result["errors"].append("NATIVE_LIST_TEXTBOX_AMBIGUOUS")
            result["warnings"].append(f"{element_id}: multiple matching TextBox objects")
            continue
        actual_object = available[0]
        used_objects.add((actual_object.get("slide_part"), actual_object.get("object_id")))
        expected_paragraphs = item.get("paragraphs")
        actual_paragraphs = actual_object.get("paragraphs")
        if not isinstance(expected_paragraphs, list) or not isinstance(actual_paragraphs, list):
            result["errors"].append("NATIVE_LIST_STRUCTURE_MISMATCH")
            continue
        if len(expected_paragraphs) != len(actual_paragraphs):
            result["errors"].append("NATIVE_LIST_PARAGRAPH_COUNT_MISMATCH")
            result["warnings"].append(
                f"{element_id}: expected {len(expected_paragraphs)} paragraphs, got {len(actual_paragraphs)}"
            )
            continue
        for index, (expected, actual) in enumerate(zip(expected_paragraphs, actual_paragraphs)):
            if not isinstance(expected, dict) or not isinstance(actual, dict):
                result["errors"].append("NATIVE_LIST_STRUCTURE_MISMATCH")
                continue
            if (expected.get("start"), expected.get("end")) != (actual.get("start"), actual.get("end")):
                result["errors"].append("NATIVE_LIST_PARAGRAPH_RANGE_MISMATCH")
            expected_list = expected.get("list")
            actual_list = actual.get("list")
            if not isinstance(expected_list, dict) or not isinstance(actual_list, dict):
                result["errors"].append("NATIVE_LIST_STRUCTURE_MISMATCH")
                continue
            if expected_list.get("is_list") != actual_list.get("is_list"):
                result["errors"].append("NATIVE_LIST_STRUCTURE_MISMATCH")
                continue
            if expected_list.get("is_list") is not True:
                continue
            if any(
                not _same_value(expected_list.get(key), actual_list.get(key))
                for key in ("level", "bullet_type", "bullet")
            ):
                result["errors"].append("NATIVE_LIST_STRUCTURE_MISMATCH")
                result["warnings"].append(f"{element_id} paragraph {index}: bullet identity mismatch")
            if any(
                not _same_value(expected.get(key), actual.get(key))
                for key in ("margin_left", "indent")
            ):
                result["errors"].append("NATIVE_LIST_INDENT_MISMATCH")
                result["warnings"].append(f"{element_id} paragraph {index}: list indentation mismatch")
            if any(
                not _same_value(expected_list.get(key), actual_list.get(key))
                for key in (
                    "bullet_font",
                    "bullet_size_mode",
                    "bullet_size_value",
                    "bullet_color",
                )
            ):
                result["errors"].append("NATIVE_LIST_STYLE_MISMATCH")
                result["warnings"].append(f"{element_id} paragraph {index}: bullet style mismatch")


def _slide_inheritance(
    archive: zipfile.ZipFile,
    names: set[str],
    slide_relationships: dict[str, tuple[str, str, bool]],
) -> tuple[dict[str, Any], dict[str, str]]:
    layout_part = next((target for target, kind, external in slide_relationships.values()
                        if not external and kind.endswith("/slideLayout")), None)
    if not layout_part or layout_part not in names:
        return {}, {}
    layout = _xml(archive, layout_part)
    layout_rels_part = _slide_rels_part(layout_part)
    layout_rels = _relationship_map(archive, layout_rels_part) if layout_rels_part in names else {}
    master_part = next((target for target, kind, external in layout_rels.values()
                        if not external and kind.endswith("/slideMaster")), None)
    if not master_part or master_part not in names:
        return {"layout": layout}, {}
    master = _xml(archive, master_part)
    theme_fonts: dict[str, str] = {}
    master_rels_part = _slide_rels_part(master_part)
    master_rels = _relationship_map(archive, master_rels_part) if master_rels_part in names else {}
    theme_part = next((target for target, kind, external in master_rels.values()
                       if not external and kind.endswith("/theme")), None)
    if theme_part and theme_part in names:
        theme = _xml(archive, theme_part)
        for prefix, path in (("+mj", "a:themeElements/a:fontScheme/a:majorFont"),
                             ("+mn", "a:themeElements/a:fontScheme/a:minorFont")):
            family = theme.find(path, NS)
            if family is None:
                continue
            for suffix, tag in (("lt", "latin"), ("ea", "ea"), ("cs", "cs")):
                node = family.find(f"a:{tag}", NS)
                if node is not None and node.get("typeface"):
                    theme_fonts[f"{prefix}-{suffix}"] = node.get("typeface", "")
    return {"layout": layout, "master": master}, theme_fonts


def _placeholder_identity(shape: ET.Element) -> tuple[str | None, str | None]:
    placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
    return (
        placeholder.get("type") if placeholder is not None else None,
        placeholder.get("idx") if placeholder is not None else None,
    )


def _placeholder_shape(
    root: ET.Element | None, kind: str | None, idx: str | None
) -> ET.Element | None:
    if root is None:
        return None
    candidates: list[tuple[int, ET.Element]] = []
    for shape in root.findall("p:cSld/p:spTree/p:sp", NS):
        other_kind, other_idx = _placeholder_identity(shape)
        if idx is not None and other_idx == idx:
            candidates.append((2, shape))
        elif kind is not None and other_kind == kind:
            candidates.append((1, shape))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _inherited_placeholder_geometry(
    shape: ET.Element, inheritance: dict[str, Any]
) -> tuple[int | None, int | None, int | None, int | None]:
    kind, idx = _placeholder_identity(shape)
    if kind is None and idx is None:
        return None, None, None, None
    for root_name in ("layout", "master"):
        matched = _placeholder_shape(inheritance.get(root_name), kind, idx)
        if matched is None:
            continue
        bbox = _geometry(matched, "p:spPr/a:xfrm")
        if None not in bbox:
            return bbox
    return None, None, None, None


def _inherited_body_properties(
    shape: ET.Element, inheritance: dict[str, Any]
) -> list[ET.Element | None]:
    kind, idx = _placeholder_identity(shape)
    properties: list[ET.Element | None] = []
    for root_name in ("layout", "master"):
        matched = _placeholder_shape(inheritance.get(root_name), kind, idx)
        body = matched.find("p:txBody/a:bodyPr", NS) if matched is not None else None
        properties.append(body)
    return properties


def _placeholder_level(root: ET.Element | None, kind: str | None, idx: str | None, level: int) -> ET.Element | None:
    shape = _placeholder_shape(root, kind, idx)
    return (
        shape.find(f"p:txBody/a:lstStyle/a:lvl{level + 1}pPr", NS)
        if shape is not None else None
    )


def _inherited_paragraph_properties(
    shape: ET.Element, level: int, inheritance: dict[str, Any]
) -> list[ET.Element | None]:
    kind, idx = _placeholder_identity(shape)
    properties: list[ET.Element | None] = []
    for root_name in ("layout", "master"):
        properties.append(
            _placeholder_level(inheritance.get(root_name), kind, idx, level)
        )
    master = inheritance.get("master")
    if master is None:
        return properties
    style = "titleStyle" if kind in {"title", "ctrTitle"} else "bodyStyle" if kind in {"body", "obj", "subTitle"} else "otherStyle"
    properties.append(master.find(f"p:txStyles/p:{style}/a:lvl{level + 1}pPr", NS))
    return properties


def _picture_covers_slide(picture: ET.Element, width: int, height: int) -> bool:
    transform = picture.find("p:spPr/a:xfrm", NS)
    if transform is None:
        return False
    offset = transform.find("a:off", NS)
    extent = transform.find("a:ext", NS)
    x = _int_attr(offset, "x")
    y = _int_attr(offset, "y")
    cx = _int_attr(extent, "cx")
    cy = _int_attr(extent, "cy")
    if None in {x, y, cx, cy} or width <= 0 or height <= 0:
        return False
    assert x is not None and y is not None and cx is not None and cy is not None
    area_ratio = (cx * cy) / (width * height)
    near_origin = x <= width * 0.01 and y <= height * 0.01
    reaches_edges = x + cx >= width * 0.99 and y + cy >= height * 0.99
    return area_ratio >= 0.98 and near_origin and reaches_edges


def _check_relationship_targets(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    missing: list[str] = []
    for rels_part in sorted(name for name in names if name.endswith(".rels")):
        try:
            relationships = _relationship_map(archive, rels_part)
        except ValidationError as exc:
            missing.append(f"{exc.code}:{rels_part}:{exc.detail}")
            continue
        for relationship_id, (target, _kind, external) in relationships.items():
            if not external and target not in names:
                missing.append(f"{rels_part}#{relationship_id}->{target}")
    return missing


def _audit_relationships(
    archive: zipfile.ZipFile, names: set[str], result: dict[str, Any]
) -> None:
    missing: list[str] = []
    for rels_part in sorted(name for name in names if name.endswith(".rels")):
        try:
            relationships = _relationship_map(archive, rels_part)
        except ValidationError as exc:
            result["errors"].append(
                "RELATIONSHIPS_XML_INVALID" if exc.code == "XML_INVALID" else exc.code
            )
            result["warnings"].append(f"{rels_part}: {exc.detail}")
            continue
        for relationship_id, (target, kind, external) in relationships.items():
            if external:
                result["external_relationships"].append({
                    "source_rels_part": rels_part,
                    "relationship_id": relationship_id,
                    "relationship_type": kind,
                    "target": target,
                })
                result["errors"].append("EXTERNAL_RELATIONSHIP_FORBIDDEN")
            elif target not in names:
                missing.append(f"{rels_part}#{relationship_id}->{target}")
    if missing:
        result["errors"].append("MISSING_RELATIONSHIP_TARGET")
        result["warnings"].extend(missing)


def _validate_archive_inventory(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ValidationError("PPTX_RESOURCE_LIMIT", "ZIP member count exceeds limit")
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if name in seen:
            raise ValidationError("DUPLICATE_ZIP_PART", f"Duplicate ZIP part: {name}")
        seen.add(name)
        if (
            not name or "\\" in name or name.startswith("/")
            or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts)
        ):
            raise ValidationError("ZIP_PART_NAME_INVALID", f"Invalid ZIP part name: {name}")
        if info.file_size > MAX_MEMBER_UNCOMPRESSED:
            raise ValidationError("PPTX_RESOURCE_LIMIT", f"ZIP member too large: {name}")
        if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) and info.file_size > MAX_MEDIA_BYTES:
            raise ValidationError("PPTX_RESOURCE_LIMIT", f"Media member too large: {name}")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise ValidationError("PPTX_RESOURCE_LIMIT", "Total expanded package size exceeds limit")
        if info.file_size and info.compress_size == 0:
            raise ValidationError("PPTX_RESOURCE_LIMIT", f"Invalid compression size: {name}")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise ValidationError("PPTX_RESOURCE_LIMIT", f"Compression ratio exceeds limit: {name}")
        if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")):
            try:
                payload = archive.read(info)
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(payload)) as image:
                        width, height = image.size
                pixels = width * height
                if (
                    width <= 0 or height <= 0
                    or width > MAX_MEDIA_DIMENSION or height > MAX_MEDIA_DIMENSION
                    or pixels > MAX_MEDIA_PIXELS or pixels * 4 > MAX_MEDIA_RGBA_BYTES
                ):
                    raise ValidationError("PPTX_RESOURCE_LIMIT", f"Media pixel budget exceeded: {name}")
            except ValidationError:
                raise
            except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                raise ValidationError("PPTX_RESOURCE_LIMIT", f"Media decompression bomb: {name}") from exc
            except Exception as exc:
                raise ValidationError("MEDIA_IMAGE_INVALID", f"Invalid image media: {name}") from exc


def _content_type_maps(archive: zipfile.ZipFile):
    root = _xml(archive, "[Content_Types].xml")
    content_ns = {"ct": "http://schemas.openxmlformats.org/package/2006/content-types"}
    content_uri = content_ns["ct"]
    if root.tag != f"{{{content_uri}}}Types":
        raise ValidationError("CONTENT_TYPES_INVALID", "Unexpected Types root QName")
    allowed = {f"{{{content_uri}}}Override", f"{{{content_uri}}}Default"}
    if any(child.tag not in allowed for child in list(root)):
        raise ValidationError("CONTENT_TYPES_INVALID", "Unknown direct child in [Content_Types].xml")
    overrides: dict[str, str] = {}
    defaults: dict[str, str] = {}
    mime_pattern = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:\s*;.*)?$")
    for item in root.findall("ct:Override", content_ns):
        raw = item.get("PartName", "")
        content_type = item.get("ContentType", "")
        decoded = unquote(raw)
        part = raw.lstrip("/")
        if (
            not raw.startswith("/") or raw.startswith("//") or "\\" in raw
            or decoded != raw or any(segment in {"", ".", ".."} for segment in PurePosixPath(part).parts)
            or part in overrides or not mime_pattern.match(content_type)
        ):
            raise ValidationError("CONTENT_TYPES_INVALID", f"Invalid content type Override: {raw}")
        overrides[part] = content_type
    for item in root.findall("ct:Default", content_ns):
        extension = item.get("Extension", "").lower()
        content_type = item.get("ContentType", "")
        if (
            not extension or extension.startswith(".") or "/" in extension or "\\" in extension
            or extension in defaults or not mime_pattern.match(content_type)
        ):
            raise ValidationError("CONTENT_TYPES_INVALID", f"Invalid content type Default: {extension}")
        defaults[extension] = content_type
    return overrides, defaults


def _validate_image_payload(archive: zipfile.ZipFile, part: str) -> None:
    info = archive.getinfo(part)
    if info.file_size > MAX_MEDIA_BYTES:
        raise ValidationError("PPTX_RESOURCE_LIMIT", f"Image part exceeds byte budget: {part}")
    try:
        payload = archive.read(info)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.size
                pixels = width * height
                if (
                    width <= 0 or height <= 0
                    or width > MAX_MEDIA_DIMENSION or height > MAX_MEDIA_DIMENSION
                    or pixels > MAX_MEDIA_PIXELS or pixels * 4 > MAX_MEDIA_RGBA_BYTES
                ):
                    raise ValidationError("PPTX_RESOURCE_LIMIT", f"Image pixel budget exceeded: {part}")
                image.verify()
    except ValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError("PPTX_RESOURCE_LIMIT", f"Image decompression bomb: {part}") from exc
    except Exception as exc:
        raise ValidationError("MEDIA_IMAGE_INVALID", f"Invalid image part: {part}") from exc


def _validate_image_parts(
    archive: zipfile.ZipFile,
    names: set[str],
    overrides: dict[str, str],
    defaults: dict[str, str],
) -> None:
    image_parts = {
        part for part in names
        if (overrides.get(part) or defaults.get(PurePosixPath(part).suffix.lstrip(".").lower()) or "")
        .startswith("image/")
    }
    for rels_part in sorted(name for name in names if name.endswith(".rels")):
        for target, relation_type, external in _relationship_map(archive, rels_part).values():
            if not external and relation_type.endswith("/image"):
                image_parts.add(target)
    for part in sorted(image_parts):
        if part not in names:
            raise ValidationError("MISSING_RELATIONSHIP_TARGET", f"Missing image part: {part}")
        content_type = overrides.get(part) or defaults.get(
            PurePosixPath(part).suffix.lstrip(".").lower()
        )
        if not (content_type or "").startswith("image/"):
            raise ValidationError("RELATIONSHIP_ROLE_MISMATCH", f"Image part lacks image content type: {part}")
        _validate_image_payload(archive, part)


def _validate_content_type_coverage(
    archive: zipfile.ZipFile,
    names: set[str],
    overrides: dict[str, str],
    defaults: dict[str, str],
) -> None:
    for part in overrides:
        if part not in names:
            raise ValidationError("CONTENT_TYPES_INVALID", f"Override targets missing part: {part}")
    for part in names:
        if part == "[Content_Types].xml" or part.endswith(".rels"):
            continue
        extension = PurePosixPath(part).suffix.lstrip(".").lower()
        if part.startswith("ppt/slides/") and part.endswith(".xml") and part not in overrides:
            raise ValidationError("CONTENT_TYPE_MISSING", f"Slide requires Override: {part}")
        content_type = overrides.get(part) or defaults.get(extension)
        if not content_type:
            raise ValidationError("CONTENT_TYPE_MISSING", f"No content type for part: {part}")
        if part == "ppt/presentation.xml" and "presentation.main+xml" not in content_type:
            raise ValidationError("CONTENT_TYPES_INVALID", "Presentation content type role mismatch")
        if part.startswith("ppt/slides/") and part.endswith(".xml") and content_type != SLIDE_CONTENT_TYPE:
            raise ValidationError("CONTENT_TYPES_INVALID", f"Slide content type role mismatch: {part}")
        if part.startswith("ppt/media/") and not content_type.startswith("image/"):
            raise ValidationError("CONTENT_TYPES_INVALID", f"Media content type role mismatch: {part}")


def _validate_critical_relationship_roles(
    archive: zipfile.ZipFile,
    names: set[str],
    overrides: dict[str, str],
    defaults: dict[str, str],
) -> None:
    root = _relationship_map(archive, "_rels/.rels")
    office = [
        relation for relation in root.values()
        if relation[1].endswith("/officeDocument") and not relation[2]
    ]
    if len(office) != 1 or office[0][0] != "ppt/presentation.xml":
        raise ValidationError("RELATIONSHIP_ROLE_MISMATCH", "Root must target one presentation officeDocument")
    for rels_part in sorted(name for name in names if name.endswith(".rels")):
        for target, relation_type, external in _relationship_map(archive, rels_part).values():
            if external:
                continue
            extension = PurePosixPath(target).suffix.lstrip(".").lower()
            content_type = overrides.get(target) or defaults.get(extension)
            expected_fragment = None
            if relation_type.endswith("/slide"):
                expected_fragment = ".slide+xml"
            elif relation_type.endswith("/slideLayout"):
                expected_fragment = ".slideLayout+xml"
            elif relation_type.endswith("/slideMaster"):
                expected_fragment = ".slideMaster+xml"
            elif relation_type.endswith("/theme"):
                expected_fragment = "officedocument.theme+xml"
            elif relation_type.endswith("/image"):
                if not (content_type or "").startswith("image/"):
                    raise ValidationError("RELATIONSHIP_ROLE_MISMATCH", f"Image relationship targets non-image: {target}")
            if expected_fragment and expected_fragment not in (content_type or ""):
                raise ValidationError("RELATIONSHIP_ROLE_MISMATCH", f"Relationship role mismatch: {target}")


def _validate_slide_object_ids(slide: ET.Element) -> None:
    object_paths = (
        (".//p:sp", "p:nvSpPr/p:cNvPr"),
        (".//p:pic", "p:nvPicPr/p:cNvPr"),
        (".//p:graphicFrame", "p:nvGraphicFramePr/p:cNvPr"),
        (".//p:grpSp", "p:nvGrpSpPr/p:cNvPr"),
        (".//p:cxnSp", "p:nvCxnSpPr/p:cNvPr"),
    )
    seen: set[int] = set()
    root_group = slide.find("p:cSld/p:spTree/p:nvGrpSpPr/p:cNvPr", NS)
    nodes: list[ET.Element | None] = [root_group]
    for object_path, identity_path in object_paths:
        for obj in slide.findall(object_path, NS):
            nodes.append(obj.find(identity_path, NS))
    for node in nodes:
        if node is None:
            raise ValidationError("SLIDE_OBJECT_ID_INVALID", "Visible object lacks cNvPr identity")
        raw = node.get("id")
        try:
            value = int(raw or "")
        except ValueError as exc:
            raise ValidationError("SLIDE_OBJECT_ID_INVALID", f"Invalid cNvPr id: {raw!r}") from exc
        if value <= 0 or value in seen:
            raise ValidationError("SLIDE_OBJECT_ID_INVALID", f"Duplicate/invalid cNvPr id: {value}")
        seen.add(value)


def _xml_relationship_ids(root: ET.Element) -> set[str]:
    values: set[str] = set()
    prefix = f"{{{NS['r']}}}"
    for element in root.iter():
        for attribute, value in element.attrib.items():
            if attribute.startswith(prefix) and value:
                values.add(value)
    return values


def validate_pptx(
    path: Path,
    expected_slides: int | None = None,
    reconstruction_spec: dict[str, Any] | Path | str | None = None,
) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    result = _result(path)
    if not path.is_file():
        result["errors"].append("PPTX_NOT_FOUND")
        return result
    try:
        result["pptx_sha256"] = _file_sha256(path)
    except OSError:
        result["errors"].append("PPTX_ZIP_INVALID")
        return result
    spec = None
    if reconstruction_spec is not None:
        try:
            spec = _load_reconstruction_spec(reconstruction_spec)
        except ValidationError as exc:
            result["errors"].append(exc.code)
            result["warnings"].append(exc.detail)
            return result

    try:
        with zipfile.ZipFile(path) as archive:
            try:
                _validate_archive_inventory(archive)
            except ValidationError as exc:
                result["errors"].append(exc.code)
                result["warnings"].append(exc.detail)
                return result
            bad_member = archive.testzip()
            if bad_member:
                result["errors"].append("PPTX_ZIP_CORRUPT")
                result["warnings"].append(f"Corrupt member: {bad_member}")
                return result
            names = set(archive.namelist())
            missing_parts = sorted(REQUIRED_PARTS - names)
            if missing_parts:
                result["errors"].append("PPTX_REQUIRED_PART_MISSING")
                result["warnings"].append("Missing: " + ", ".join(missing_parts))
                return result

            _audit_relationships(archive, names, result)

            try:
                content_overrides, content_defaults = _content_type_maps(archive)
                _validate_content_type_coverage(
                    archive, names, content_overrides, content_defaults
                )
                _validate_critical_relationship_roles(
                    archive, names, content_overrides, content_defaults
                )
                _validate_image_parts(
                    archive, names, content_overrides, content_defaults
                )
            except ValidationError as exc:
                result["errors"].append(exc.code)
                result["warnings"].append(exc.detail)
                return result

            try:
                presentation = _xml(archive, "ppt/presentation.xml")
                presentation_rels = _relationship_map(
                    archive, "ppt/_rels/presentation.xml.rels"
                )
            except ValidationError as exc:
                result["errors"].append(
                    "RELATIONSHIPS_XML_INVALID"
                    if exc.code == "XML_INVALID"
                    else "PRESENTATION_XML_INVALID"
                )
                result["warnings"].append(exc.detail)
                return result

            size = presentation.find("p:sldSz", NS)
            width = _int_attr(size, "cx")
            height = _int_attr(size, "cy")
            result["width_emu"] = width
            result["height_emu"] = height
            if not width or not height:
                result["errors"].append("SLIDE_SIZE_MISSING")
                return result
            ratio = width / height
            result["aspect_ratio"] = ratio
            if abs(ratio - (16 / 9)) > 0.002:
                result["errors"].append("ASPECT_RATIO_NOT_16_9")

            slide_ids = presentation.findall("p:sldIdLst/p:sldId", NS)
            result["slide_count"] = len(slide_ids)
            if expected_slides is not None and len(slide_ids) != expected_slides:
                result["errors"].append("SLIDE_COUNT_MISMATCH")

            any_full_slide_picture = False
            spec_element_ids = {
                item.get("element_id")
                for item in spec.get("elements", [])
                if isinstance(item, dict) and isinstance(item.get("element_id"), str)
            } if isinstance(spec, dict) and isinstance(spec.get("elements"), list) else set()
            fonts: set[str] = set()
            font_sizes: set[float] = set()
            for position, slide_id in enumerate(slide_ids, start=1):
                rid = slide_id.get(RID)
                relationship = presentation_rels.get(rid or "")
                if not relationship or relationship[2] or not relationship[1].endswith("/slide"):
                    result["errors"].append("SLIDE_RELATIONSHIP_INVALID")
                    continue
                slide_part = relationship[0]
                if slide_part not in names:
                    result["errors"].append("SLIDE_PART_MISSING")
                    continue
                if content_overrides.get(slide_part) != SLIDE_CONTENT_TYPE:
                    result["errors"].append("CONTENT_TYPE_MISSING")
                    result["warnings"].append(
                        f"Slide part lacks the required content type override: {slide_part}"
                    )
                try:
                    slide = _xml(archive, slide_part)
                    _validate_slide_object_ids(slide)
                except ValidationError as exc:
                    result["errors"].append(
                        exc.code if exc.code == "SLIDE_OBJECT_ID_INVALID" else "SLIDE_XML_INVALID"
                    )
                    result["warnings"].append(exc.detail)
                    continue

                rels_part = _slide_rels_part(slide_part)
                try:
                    slide_relationships = (
                        _relationship_map(archive, rels_part) if rels_part in names else {}
                    )
                except ValidationError as exc:
                    result["errors"].append(
                        "RELATIONSHIPS_XML_INVALID" if exc.code == "XML_INVALID" else exc.code
                    )
                    result["warnings"].append(f"{rels_part}: {exc.detail}")
                    slide_relationships = {}
                try:
                    inheritance, theme_fonts = _slide_inheritance(
                        archive, names, slide_relationships
                    )
                except ValidationError as exc:
                    result["errors"].append(exc.code)
                    result["warnings"].append(exc.detail)
                    inheritance, theme_fonts = {}, {}
                sp_tree = slide.find("p:cSld/p:spTree", NS)
                object_records = _collect_visible_objects(
                    list(sp_tree) if sp_tree is not None else [], slide_part, width, height,
                    inheritance,
                )
                shapes = [item for item in object_records if item["object_type"] == "sp"]
                pictures = [item for item in object_records if item["object_type"] == "pic"]
                graphic_frames = [item for item in object_records if item["object_type"] == "graphicFrame"]
                result["structure_objects"].extend({
                    key: value for key, value in item.items() if key != "_element"
                } for item in object_records)
                text_shapes = sum(
                    1 for item in shapes if item["has_text"] and item["visible"] is True
                )
                font_properties = _font_properties(slide)
                fonts.update(_declared_fonts(font_properties))
                font_sizes.update(_declared_font_sizes(font_properties))
                slide_text_runs = len(slide.findall(".//a:r", NS)) + len(
                    slide.findall(".//a:fld", NS)
                )
                meaningful_editable = [
                    item
                    for item in object_records
                    if item["object_type"] in {"sp", "graphicFrame", "cxnSp"}
                    and item["visible"] is True
                    and item["geometry_known"] is True
                ]
                if spec_element_ids:
                    meaningful_editable = [
                        item
                        for item in meaningful_editable
                        if _bound_element_id(item.get("object_name"), spec_element_ids)
                        is not None
                    ]
                editable = len(meaningful_editable)
                full_pictures = sum(
                    1 for picture in pictures
                    if picture["geometry_known"] and picture["visible"] is True
                    and picture["x"] <= 0 and picture["y"] <= 0
                    and picture["x"] + picture["cx"] >= width
                    and picture["y"] + picture["cy"] >= height
                )
                slide_picture_objects: list[dict[str, Any]] = []
                slide_text_objects: list[dict[str, Any]] = []
                for shape_record in shapes:
                    shape = shape_record["_element"]
                    native = {
                        key: shape_record[key] for key in (
                            "slide_part", "object_id", "object_name", "layer", "hidden",
                            "x", "y", "cx", "cy", "has_text", "geometry_known", "visible",
                        )
                    }
                    round_rect_status, round_rect_adjustment = _round_rect_adjustment(shape)
                    if round_rect_status is not None:
                        native["preset_geometry"] = "roundRect"
                        native["corner_adjustment"] = round_rect_adjustment
                    if round_rect_status == "missing":
                        result["errors"].append("ROUND_RECT_ADJUSTMENT_MISSING")
                        result["warnings"].append(
                            f"{slide_part} shape {shape_record['object_id']} uses default roundRect adjustment"
                        )
                    elif round_rect_status == "invalid":
                        result["errors"].append("ROUND_RECT_ADJUSTMENT_INVALID")
                        result["warnings"].append(
                            f"{slide_part} shape {shape_record['object_id']} has invalid roundRect adjustment"
                        )
                    result["native_shape_objects"].append(native)
                    if shape_record["has_text"]:
                        text_object = _text_object(
                            shape, slide_part, shape_record["layer"],
                            inheritance, theme_fonts,
                            (shape_record["x"], shape_record["y"], shape_record["cx"], shape_record["cy"]),
                        )
                        text_object["visible"] = shape_record["visible"]
                        text_object["geometry_known"] = shape_record["geometry_known"]
                        slide_text_objects.append(text_object)
                        result["text_objects"].append(text_object)
                slide_list_paragraphs = sum(
                    1
                    for text_object in slide_text_objects
                    for paragraph in text_object.get("paragraphs", [])
                    if isinstance(paragraph.get("list"), dict)
                    and paragraph["list"].get("is_list") is True
                )
                for picture_index, picture_record in enumerate(pictures, start=1):
                    picture = picture_record["_element"]
                    blip = picture.find("p:blipFill/a:blip", NS)
                    picture_rid = blip.get(REMBED) if blip is not None else None
                    relationship = slide_relationships.get(picture_rid or "")
                    media_part = None
                    media_hash = None
                    if (
                        relationship is None
                        or relationship[2]
                        or not relationship[1].endswith("/image")
                    ):
                        result["errors"].append("PICTURE_RELATIONSHIP_INVALID")
                    else:
                        media_part = relationship[0]
                        if media_part in names:
                            media_hash = _archive_sha256(archive, media_part)
                    record = {
                        "object_key": f"{slide_part}#picture-{picture_index}",
                        "slide_position": position,
                        "slide_part": slide_part,
                        "object_id": picture_record["object_id"],
                        "object_name": picture_record["object_name"],
                        "layer": picture_record["layer"],
                        "hidden": picture_record["hidden"],
                        "relationship_id": picture_rid,
                        "media_part": media_part,
                        "media_basename": PurePosixPath(media_part).name if media_part else None,
                        "media_sha256": media_hash,
                        "x": picture_record["x"], "y": picture_record["y"],
                        "cx": picture_record["cx"], "cy": picture_record["cy"],
                        "geometry_known": picture_record["geometry_known"],
                        "full_slide": picture_record["geometry_known"]
                        and picture_record["x"] <= 0 and picture_record["y"] <= 0
                        and picture_record["x"] + picture_record["cx"] >= width
                        and picture_record["y"] + picture_record["cy"] >= height,
                    }
                    record["visible"] = picture_record["visible"]
                    slide_picture_objects.append(record)
                    result["picture_objects"].append(record)
                missing_xml_relationships = sorted(
                    _xml_relationship_ids(slide) - set(slide_relationships)
                )
                if missing_xml_relationships:
                    result["errors"].append("MISSING_XML_RELATIONSHIP")
                    result["warnings"].append(
                        f"{slide_part} references missing ids: {', '.join(missing_xml_relationships)}"
                    )
                has_full_slide_picture = bool(full_pictures)
                picture_only = has_full_slide_picture and editable == 0
                any_full_slide_picture = any_full_slide_picture or has_full_slide_picture
                if editable == 0:
                    result["errors"].append("NO_EDITABLE_OBJECTS")
                if picture_only:
                    result["errors"].append("FULL_SLIDE_PICTURE_ONLY")
                elif has_full_slide_picture:
                    result["warnings"].append("FULL_SLIDE_PICTURE_WITH_EDITABLE_OBJECTS")
                result["editable_object_count"] += editable
                result["text_shape_count"] += text_shapes
                visible_graphic_frames = sum(
                    1 for item in graphic_frames if item["visible"] is True
                )
                result["graphic_frame_count"] += visible_graphic_frames
                result["picture_count"] += len(pictures)
                result["text_runs"] += slide_text_runs
                result["native_list_paragraphs"] += slide_list_paragraphs
                result["slides"].append(
                    {
                        "position": position,
                        "part": slide_part,
                        "editable_object_count": editable,
                        "text_shape_count": text_shapes,
                        "graphic_frame_count": visible_graphic_frames,
                        "picture_count": len(pictures),
                        "text_runs": slide_text_runs,
                        "native_list_paragraphs": slide_list_paragraphs,
                        "picture_objects": slide_picture_objects,
                        "full_slide_picture_count": full_pictures,
                        "full_slide_picture_risk": has_full_slide_picture,
                        "missing_xml_relationships": missing_xml_relationships,
                    }
                )

            result["font_declarations"] = sorted(fonts)
            result["font_sizes_pt"] = sorted(font_sizes)
            result["full_slide_picture_risk"] = any_full_slide_picture
            if spec is not None:
                _validate_native_list_contracts(result, spec, width, height)
                _validate_text_run_contracts(result, spec, width, height)
                _validate_element_bindings(result, spec, width, height)
            if result["slide_count"] == 0:
                result["errors"].append("NO_SLIDES")

    except ValidationError as exc:
        result["errors"].append(exc.code)
        result["warnings"].append(exc.detail)
        return result
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, ValueError):
        result["errors"].append("PPTX_ZIP_INVALID")
        return result

    result["errors"] = list(dict.fromkeys(result["errors"]))
    result["valid"] = not result["errors"]
    return result


def summary_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return the CLI-friendly validation summary without per-object payloads."""
    verbose_keys = {
        "text_objects",
        "native_shape_objects",
        "picture_objects",
        "structure_objects",
    }
    summary = {key: value for key, value in result.items() if key not in verbose_keys}
    summary["slides"] = [
        {key: value for key, value in slide.items() if key != "picture_objects"}
        for slide in result.get("slides", [])
    ]
    return summary


def _emit_json(payload: dict[str, Any], output: Path | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, output)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    print(text, end="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--expected-slides", type=int)
    parser.add_argument(
        "--spec",
        type=Path,
        help="validate native list/TextBox structure against page-reconstruction.json",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="omit per-object arrays from CLI JSON output",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="atomically save the same JSON emitted to stdout",
    )
    args = parser.parse_args(argv)
    result = validate_pptx(args.pptx, args.expected_slides, args.spec)
    _emit_json(summary_result(result) if args.summary else result, args.output)
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
