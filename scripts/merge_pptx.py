#!/usr/bin/env python3
"""Merge validated single-slide PPTX files while preserving OOXML relationships."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import posixpath
import secrets
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from lib.spec_identity import content_spec_sha256


NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}
VERIFICATION_PROFILES = {"rapid", "reviewed", "strict"}
PROFILE_DELIVERY_STATUSES = {
    "rapid": {"rapid_validated", "rapid_validation_failed"},
    "reviewed": {"reviewed_passed", "reviewed_failed"},
    "strict": {"strict_gate_passed", "strict_gate_failed"},
}
PROFILE_SUCCESS_STATUSES = {
    "rapid": "rapid_validated",
    "reviewed": "reviewed_passed",
    "strict": "strict_gate_passed",
}
RID = f"{{{NS['r']}}}id"
SLIDE_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)
SLIDE_MASTER_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"
)
VALIDATOR_PATH = Path(__file__).with_name("validate_pptx.py")

for prefix, uri in NS.items():
    if prefix not in {"ct", "pr"}:
        ET.register_namespace(prefix, uri)
ET.register_namespace("", NS["pr"])


class MergeError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"ok": False, "code": self.code, "message": self.message}
        if self.details:
            value["details"] = self.details
        return value


def _load_validator():
    spec = importlib.util.spec_from_file_location("ia_validate_pptx_runtime", VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise MergeError("VALIDATOR_UNAVAILABLE", f"Cannot load validator: {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_page_spec(path: Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise MergeError("SPEC_NOT_FOUND", f"Page spec does not exist: {resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MergeError("SPEC_INVALID", f"Page spec is not valid JSON: {resolved}") from exc
    if not isinstance(payload, dict):
        raise MergeError("SPEC_INVALID", f"Page spec root must be an object: {resolved}")
    return payload


def _load_final_report(path: Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise MergeError("FINAL_REPORT_NOT_FOUND", f"Final report does not exist: {resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MergeError(
            "FINAL_REPORT_INVALID", f"Final report is not valid JSON: {resolved}"
        ) from exc
    if not isinstance(payload, dict):
        raise MergeError(
            "FINAL_REPORT_INVALID", f"Final report root must be an object: {resolved}"
        )
    return payload


def _artifact_identity(value: Any, code: str) -> tuple[Path, str]:
    if not isinstance(value, dict):
        raise MergeError(code, "Required artifact identity is missing")
    raw_path = value.get("path")
    digest = value.get("sha256")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise MergeError(code, "Artifact path must be absolute")
    path = Path(raw_path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise MergeError(code, f"Artifact does not exist: {path}")
    actual = _file_sha256(path.resolve())
    if not isinstance(digest, str) or actual.lower() != digest.lower():
        raise MergeError(code, f"Artifact hash mismatch: {path}")
    return path.resolve(), actual.lower()


def _render_identity(
    spec: dict[str, Any],
) -> tuple[str, str, str, str, tuple[int, int]]:
    visual_gate = spec.get("visual_gate")
    artifact = (
        visual_gate.get("render_report") if isinstance(visual_gate, dict) else None
    )
    report_path, _ = _artifact_identity(artifact, "RENDER_REPORT_INVALID")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MergeError(
            "RENDER_REPORT_INVALID",
            f"Render report is not valid JSON: {report_path}",
        ) from exc
    renderer = report.get("renderer") if isinstance(report, dict) else None
    rasterizer = report.get("rasterizer") if isinstance(report, dict) else None
    output_size = rasterizer.get("output_size") if isinstance(rasterizer, dict) else None
    identity = (
        renderer.get("backend") if isinstance(renderer, dict) else None,
        renderer.get("version") if isinstance(renderer, dict) else None,
        renderer.get("executable_sha256") if isinstance(renderer, dict) else None,
        renderer.get("fontconfig_sha256") if isinstance(renderer, dict) else None,
        tuple(output_size) if isinstance(output_size, list) else (),
    )
    if (
        identity[0] != "libreoffice"
        or not all(isinstance(item, str) and item for item in identity[:4])
        or identity[4] != (1920, 1080)
    ):
        raise MergeError(
            "RENDER_REPORT_INVALID",
            f"Render report identity is incomplete: {report_path}",
        )
    return identity  # type: ignore[return-value]


def _validate_page_binding(
    input_path: Path,
    spec: dict[str, Any],
    final_report: dict[str, Any],
) -> dict[str, Any]:
    page_id = spec.get("page_id")
    if not isinstance(page_id, str) or not page_id:
        raise MergeError("PAGE_ID_INVALID", "Every page spec requires a non-empty page_id")
    input_hash = _file_sha256(input_path)
    verification_profile = spec.get("verification_profile")
    if verification_profile not in VERIFICATION_PROFILES:
        raise MergeError(
            "VERIFICATION_PROFILE_INVALID",
            f"Every page spec requires rapid, reviewed, or strict verification: {page_id}",
        )
    delivery_status = spec.get("delivery_status")
    if delivery_status not in PROFILE_DELIVERY_STATUSES[verification_profile]:
        raise MergeError(
            "DELIVERY_STATUS_INVALID",
            f"Delivery status does not match {verification_profile}: {page_id}",
        )
    if delivery_status != PROFILE_SUCCESS_STATUSES[verification_profile]:
        raise MergeError(
            "FINAL_REPORT_INVALID",
            f"Only a successfully finalized page can be merged: {page_id}",
        )
    gate_hashes: list[str] = []
    for gate_name in ("visual_gate", "editability_gate"):
        gate = spec.get(gate_name)
        identity = gate.get("pptx") if isinstance(gate, dict) else None
        if isinstance(identity, dict) and isinstance(identity.get("sha256"), str):
            gate_hashes.append(identity["sha256"].lower())
    if not gate_hashes or any(value != input_hash for value in gate_hashes):
        raise MergeError(
            "INPUT_SPEC_HASH_MISMATCH",
            f"Input PPTX is not the page currently bound by the spec: {input_path}",
            page_id=page_id,
            actual=input_hash,
            declared=gate_hashes,
        )
    editability_gate = spec.get("editability_gate")
    validator_identity = (
        editability_gate.get("validator") if isinstance(editability_gate, dict) else None
    )
    validator_path, _ = _artifact_identity(
        validator_identity, "VALIDATOR_ARTIFACT_INVALID"
    )
    try:
        validator_report = json.loads(validator_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MergeError("VALIDATOR_ARTIFACT_INVALID", "Validator report is not valid JSON") from exc
    if (
        not isinstance(validator_report, dict)
        or validator_report.get("valid") is not True
        or validator_report.get("errors") != []
        or validator_report.get("pptx_sha256") != input_hash
    ):
        raise MergeError(
            "VALIDATOR_ARTIFACT_INVALID",
            f"Validator report does not bind a valid current PPTX: {input_path}",
        )
    final_pptx = final_report.get("current_pptx")
    if (
        final_report.get("valid") is not True
        or final_report.get("errors") != []
        or final_report.get("stage") != "final"
        or final_report.get("verification_profile") != verification_profile
        or final_report.get("delivery_status") != delivery_status
        or final_report.get("content_spec_sha256") != content_spec_sha256(spec)
        or not isinstance(final_pptx, dict)
        or final_pptx.get("sha256") != input_hash
        or final_pptx.get("path") != str(input_path.resolve())
    ):
        raise MergeError(
            "FINAL_REPORT_INVALID",
            f"Final report does not bind the current page, spec, profile, and PPTX: {page_id}",
        )
    spec_runtime_path, spec_runtime_hash = _artifact_identity(
        spec.get("runtime_preflight"), "RUNTIME_PREFLIGHT_INVALID"
    )
    final_runtime = final_report.get("runtime_preflight")
    if (
        not isinstance(final_runtime, dict)
        or final_runtime.get("path") != str(spec_runtime_path)
        or final_runtime.get("sha256") != spec_runtime_hash
    ):
        raise MergeError(
            "FINAL_REPORT_INVALID",
            f"Final report runtime identity does not match the current spec: {page_id}",
        )
    capability_hash = final_report.get("capability_manifest_sha256")
    if (
        not isinstance(capability_hash, str)
        or len(capability_hash) != 64
        or any(character not in "0123456789abcdef" for character in capability_hash)
    ):
        raise MergeError(
            "FINAL_REPORT_INVALID",
            f"Final report capability identity is invalid: {page_id}",
        )
    if (
        not isinstance(validator_report, dict)
        or validator_report.get("valid") is not True
        or validator_report.get("errors") != []
        or validator_report.get("pptx_sha256") != input_hash
        or validator_report.get("slide_count") != 1
        or type(validator_report.get("width_emu")) is not int
        or validator_report["width_emu"] <= 0
        or type(validator_report.get("height_emu")) is not int
        or validator_report["height_emu"] <= 0
    ):
        raise MergeError(
            "VALIDATOR_ARTIFACT_INVALID",
            f"Structure report does not bind one valid current slide: {input_path}",
        )
    return {
        "page_id": page_id,
        "verification_profile": verification_profile,
        "delivery_status": delivery_status,
        "profile_passed": True,
        "runtime_sha256": spec_runtime_hash,
        "capability_manifest_sha256": capability_hash,
        "validation": validator_report,
    }


def _read_package(path: Path) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise MergeError("INPUT_ZIP_CORRUPT", f"Corrupt PPTX member: {bad}")
            return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    except zipfile.BadZipFile as exc:
        raise MergeError("INPUT_ZIP_INVALID", f"Input is not a PPTX ZIP: {path}") from exc


def _rels_part(part: str) -> str:
    path = PurePosixPath(part)
    return str(path.parent / "_rels" / f"{path.name}.rels")


def _resolve_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def _relative_target(source_part: str, target_part: str) -> str:
    base = posixpath.dirname(source_part) or "."
    return posixpath.relpath(target_part, base)


def _relationship_map(entries: dict[str, bytes], rels_part: str, source_part: str):
    try:
        root = ET.fromstring(entries[rels_part])
    except (KeyError, ET.ParseError) as exc:
        raise MergeError("RELATIONSHIPS_INVALID", f"Invalid relationships: {rels_part}") from exc
    values = {}
    for rel in root.findall("pr:Relationship", NS):
        relationship_id = rel.get("Id")
        target = rel.get("Target")
        if not relationship_id or not target:
            continue
        external = rel.get("TargetMode") == "External"
        values[relationship_id] = (
            target if external else _resolve_target(source_part, target),
            rel.get("Type", ""),
            external,
        )
    return values


def _single_slide_part(entries: dict[str, bytes]) -> str:
    try:
        presentation = ET.fromstring(entries["ppt/presentation.xml"])
    except (KeyError, ET.ParseError) as exc:
        raise MergeError("PRESENTATION_INVALID", "Cannot parse source presentation") from exc
    slide_ids = presentation.findall("p:sldIdLst/p:sldId", NS)
    if len(slide_ids) != 1:
        raise MergeError("INPUT_NOT_SINGLE_SLIDE", "Every merge input must contain exactly one slide")
    relationships = _relationship_map(
        entries, "ppt/_rels/presentation.xml.rels", "ppt/presentation.xml"
    )
    relationship = relationships.get(slide_ids[0].get(RID, ""))
    if not relationship or relationship[2] or not relationship[1].endswith("/slide"):
        raise MergeError("SLIDE_RELATIONSHIP_INVALID", "Source slide relationship is invalid")
    return relationship[0]


class PartImporter:
    def __init__(
        self,
        source_entries: dict[str, bytes],
        destination_entries: dict[str, bytes],
        destination_content_types: ET.Element,
    ):
        self.source = source_entries
        self.destination = destination_entries
        self.destination_content_types = destination_content_types
        self.mapping: dict[str, str] = {}
        self.source_content_types = self._parse_content_types(source_entries)
        self.source_overrides = {
            item.get("PartName", "").lstrip("/"): item.get("ContentType", "")
            for item in self.source_content_types.findall("ct:Override", NS)
        }
        self.source_defaults = {
            item.get("Extension", "").lower(): item.get("ContentType", "")
            for item in self.source_content_types.findall("ct:Default", NS)
        }

    @staticmethod
    def _parse_content_types(entries: dict[str, bytes]) -> ET.Element:
        try:
            return ET.fromstring(entries["[Content_Types].xml"])
        except (KeyError, ET.ParseError) as exc:
            raise MergeError("CONTENT_TYPES_INVALID", "Cannot parse [Content_Types].xml") from exc

    def _allocate(self, source_part: str) -> str:
        if source_part not in self.destination:
            return source_part
        path = PurePosixPath(source_part)
        suffixes = "".join(path.suffixes)
        base = path.name[: -len(suffixes)] if suffixes else path.name
        counter = 2
        while True:
            name = f"{base}-{counter}{suffixes}"
            candidate = str(path.parent / name)
            if candidate not in self.destination:
                return candidate
            counter += 1

    def _add_content_type(self, source_part: str, destination_part: str) -> None:
        existing_overrides = {
            item.get("PartName")
            for item in self.destination_content_types.findall("ct:Override", NS)
        }
        content_type = self.source_overrides.get(source_part)
        if content_type:
            part_name = f"/{destination_part}"
            if part_name not in existing_overrides:
                ET.SubElement(
                    self.destination_content_types,
                    f"{{{NS['ct']}}}Override",
                    {"PartName": part_name, "ContentType": content_type},
                )
            return
        extension = PurePosixPath(source_part).suffix.lstrip(".").lower()
        default_type = self.source_defaults.get(extension)
        if not extension or not default_type:
            return
        existing_defaults = {
            item.get("Extension", "").lower()
            for item in self.destination_content_types.findall("ct:Default", NS)
        }
        if extension not in existing_defaults:
            ET.SubElement(
                self.destination_content_types,
                f"{{{NS['ct']}}}Default",
                {"Extension": extension, "ContentType": default_type},
            )

    def import_part(self, source_part: str, forced_destination: str | None = None) -> str:
        if source_part in self.mapping:
            return self.mapping[source_part]
        if source_part not in self.source:
            raise MergeError(
                "RELATIONSHIP_TARGET_MISSING",
                f"Source relationship target does not exist: {source_part}",
            )
        destination_part = forced_destination or self._allocate(source_part)
        if destination_part in self.destination and forced_destination:
            raise MergeError("DESTINATION_PART_EXISTS", f"Part already exists: {destination_part}")
        self.mapping[source_part] = destination_part
        self.destination[destination_part] = self.source[source_part]
        self._add_content_type(source_part, destination_part)

        source_rels = _rels_part(source_part)
        if source_rels in self.source:
            try:
                root = ET.fromstring(self.source[source_rels])
            except ET.ParseError as exc:
                raise MergeError("RELATIONSHIPS_INVALID", f"Invalid relationships: {source_rels}") from exc
            for relationship in root.findall("pr:Relationship", NS):
                target = relationship.get("Target")
                if not target or relationship.get("TargetMode") == "External":
                    continue
                source_target = _resolve_target(source_part, target)
                destination_target = self.import_part(source_target)
                relationship.set("Target", _relative_target(destination_part, destination_target))
            destination_rels = _rels_part(destination_part)
            self.destination[destination_rels] = ET.tostring(
                root, encoding="utf-8", xml_declaration=True
            )
        return destination_part


def _next_numeric_id(values: list[str], minimum: int) -> int:
    numeric = []
    for value in values:
        digits = "".join(character for character in value if character.isdigit())
        if digits:
            numeric.append(int(digits))
    return max(numeric, default=minimum - 1) + 1


def _append_slide(
    destination_entries: dict[str, bytes],
    source_entries: dict[str, bytes],
    destination_content_types: ET.Element,
) -> str:
    source_slide = _single_slide_part(source_entries)
    existing_slides = [
        name
        for name in destination_entries
        if name.startswith("ppt/slides/slide") and name.endswith(".xml")
    ]
    slide_numbers = []
    for name in existing_slides:
        stem = PurePosixPath(name).stem
        suffix = stem.removeprefix("slide")
        if suffix.isdigit():
            slide_numbers.append(int(suffix))
    destination_slide = f"ppt/slides/slide{max(slide_numbers, default=0) + 1}.xml"
    importer = PartImporter(source_entries, destination_entries, destination_content_types)
    importer.import_part(source_slide, destination_slide)

    try:
        presentation = ET.fromstring(destination_entries["ppt/presentation.xml"])
        presentation_rels = ET.fromstring(
            destination_entries["ppt/_rels/presentation.xml.rels"]
        )
    except (KeyError, ET.ParseError) as exc:
        raise MergeError("DESTINATION_PRESENTATION_INVALID", "Cannot update destination") from exc

    def add_presentation_relationship(relationship_type: str, target_part: str) -> str:
        relationship_ids = [
            rel.get("Id", "")
            for rel in presentation_rels.findall("pr:Relationship", NS)
        ]
        relationship_id = f"rId{_next_numeric_id(relationship_ids, 1)}"
        ET.SubElement(
            presentation_rels,
            f"{{{NS['pr']}}}Relationship",
            {
                "Id": relationship_id,
                "Type": relationship_type,
                "Target": _relative_target("ppt/presentation.xml", target_part),
            },
        )
        return relationship_id

    source_presentation_relationships = _relationship_map(
        source_entries, "ppt/_rels/presentation.xml.rels", "ppt/presentation.xml"
    )
    master_list = presentation.find("p:sldMasterIdLst", NS)
    for source_master, relationship_type, external in source_presentation_relationships.values():
        if external or not relationship_type.endswith("/slideMaster"):
            continue
        destination_master = importer.mapping.get(source_master)
        if not destination_master:
            continue
        already_registered = False
        for existing in presentation_rels.findall("pr:Relationship", NS):
            if not existing.get("Type", "").endswith("/slideMaster"):
                continue
            existing_target = existing.get("Target", "")
            if _resolve_target("ppt/presentation.xml", existing_target) == destination_master:
                already_registered = True
                break
        if already_registered:
            continue
        master_relationship_id = add_presentation_relationship(
            SLIDE_MASTER_REL_TYPE, destination_master
        )
        if master_list is None:
            master_list = ET.Element(f"{{{NS['p']}}}sldMasterIdLst")
            slide_list_anchor = presentation.find("p:sldIdLst", NS)
            insert_at = list(presentation).index(slide_list_anchor) if slide_list_anchor is not None else 0
            presentation.insert(insert_at, master_list)
        master_ids = [item.get("id", "") for item in master_list.findall("p:sldMasterId", NS)]
        ET.SubElement(
            master_list,
            f"{{{NS['p']}}}sldMasterId",
            {"id": str(_next_numeric_id(master_ids, 2147483648)), RID: master_relationship_id},
        )

    relationship_id = add_presentation_relationship(SLIDE_REL_TYPE, destination_slide)

    slide_list = presentation.find("p:sldIdLst", NS)
    if slide_list is None:
        slide_list = ET.SubElement(presentation, f"{{{NS['p']}}}sldIdLst")
    slide_ids = [item.get("id", "") for item in slide_list.findall("p:sldId", NS)]
    slide_id = str(_next_numeric_id(slide_ids, 256))
    ET.SubElement(
        slide_list,
        f"{{{NS['p']}}}sldId",
        {"id": slide_id, RID: relationship_id},
    )

    destination_entries["ppt/presentation.xml"] = ET.tostring(
        presentation, encoding="utf-8", xml_declaration=True
    )
    destination_entries["ppt/_rels/presentation.xml.rels"] = ET.tostring(
        presentation_rels, encoding="utf-8", xml_declaration=True
    )
    return destination_slide


def _write_package(entries: dict[str, bytes], path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            archive.writestr(name, entries[name])


def merge_presentations(
    inputs: list[Path],
    specs: list[Path],
    final_reports: list[Path],
    output: Path,
) -> dict[str, Any]:
    if not inputs:
        raise MergeError("INPUTS_REQUIRED", "At least one single-slide PPTX is required")
    if len(inputs) != len(specs):
        raise MergeError(
            "SPEC_COUNT_MISMATCH",
            "Every merge input requires one reconstruction spec",
            input_count=len(inputs),
            spec_count=len(specs),
        )
    if len(inputs) != len(final_reports):
        raise MergeError(
            "FINAL_REPORT_COUNT_MISMATCH",
            "Every merge input requires one final validation report",
            input_count=len(inputs),
            final_report_count=len(final_reports),
        )
    paths = [Path(item).expanduser().resolve() for item in inputs]
    spec_paths = [Path(item).expanduser().resolve() for item in specs]
    final_report_paths = [Path(item).expanduser().resolve() for item in final_reports]
    page_specs = [_load_page_spec(path) for path in spec_paths]
    page_final_reports = [_load_final_report(path) for path in final_report_paths]
    verification_profiles = [spec.get("verification_profile") for spec in page_specs]
    if any(profile not in VERIFICATION_PROFILES for profile in verification_profiles):
        raise MergeError(
            "VERIFICATION_PROFILE_INVALID",
            "Every page spec must use rapid, reviewed, or strict verification",
            profiles=verification_profiles,
        )
    if len(set(verification_profiles)) != 1:
        raise MergeError(
            "VERIFICATION_PROFILE_MISMATCH",
            "All merge inputs must use the same fixed verification profile",
            profiles=verification_profiles,
        )
    verification_profile = verification_profiles[0]
    render_identities = [_render_identity(spec) for spec in page_specs]
    if len(set(render_identities)) != 1:
        raise MergeError(
            "RENDER_RUNTIME_MIXED",
            "All merge inputs must use the same LibreOffice, fontconfig, and preview size",
            identities=render_identities,
        )
    page_ids = [spec.get("page_id") for spec in page_specs]
    if any(not isinstance(page_id, str) or not page_id for page_id in page_ids):
        raise MergeError("PAGE_ID_INVALID", "Every page spec requires a non-empty page_id")
    if len(page_ids) != len(set(page_ids)):
        raise MergeError("PAGE_ID_DUPLICATE", "Merge page_id values must be unique")
    validations = []
    page_bindings = []
    for path, page_spec, final_report in zip(paths, page_specs, page_final_reports):
        if not path.is_file():
            raise MergeError("INPUT_NOT_FOUND", f"Merge input does not exist: {path}")
        binding = _validate_page_binding(path, page_spec, final_report)
        page_bindings.append(binding)
        validation = binding["validation"]
        validations.append(validation)
        if validation.get("slide_count") != 1:
            raise MergeError(
                "INPUT_NOT_SINGLE_SLIDE", f"Merge input must contain one slide: {path}"
            )
    runtime_hashes = {binding["runtime_sha256"] for binding in page_bindings}
    if len(runtime_hashes) != 1:
        raise MergeError(
            "RUNTIME_PREFLIGHT_MIXED",
            "All merge inputs must use the same runtime preflight identity",
        )
    capability_hashes = {
        binding["capability_manifest_sha256"] for binding in page_bindings
    }
    if len(capability_hashes) != 1:
        raise MergeError(
            "CAPABILITY_MANIFEST_MIXED",
            "All merge inputs must use the same capability manifest identity",
        )
        if not validation.get("valid"):
            raise MergeError(
                "INPUT_VALIDATION_FAILED",
                f"Merge input did not pass validation: {path}",
                errors=validation.get("errors", []),
            )
    expected_size = (
        validations[0].get("width_emu"),
        validations[0].get("height_emu"),
    )
    for path, validation in zip(paths[1:], validations[1:]):
        actual_size = (validation.get("width_emu"), validation.get("height_emu"))
        if actual_size != expected_size:
            raise MergeError(
                "SLIDE_SIZE_MISMATCH",
                f"All input slide dimensions must match exactly: {path}",
                expected=expected_size,
                actual=actual_size,
            )

    destination_entries = _read_package(paths[0])
    try:
        destination_content_types = ET.fromstring(destination_entries["[Content_Types].xml"])
    except (KeyError, ET.ParseError) as exc:
        raise MergeError("CONTENT_TYPES_INVALID", "Cannot parse destination content types") from exc

    imported_parts = []
    for path in paths[1:]:
        source_entries = _read_package(path)
        imported_parts.append(
            _append_slide(destination_entries, source_entries, destination_content_types)
        )
    destination_entries["[Content_Types].xml"] = ET.tostring(
        destination_content_types, encoding="utf-8", xml_declaration=True
    )

    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{secrets.token_hex(6)}.tmp")
    try:
        _write_package(destination_entries, temporary)
        validator = _load_validator()
        validation = validator.validate_pptx(temporary, expected_slides=len(paths))
        if not validation.get("valid"):
            raise MergeError(
                "OUTPUT_VALIDATION_FAILED",
                "Merged presentation did not pass validation",
                errors=validation.get("errors", []),
                warnings=validation.get("warnings", []),
            )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)

    all_passed = all(binding["profile_passed"] for binding in page_bindings)
    delivery_labels = {
        "rapid": ("快速校验版", "快速校验未通过版"),
        "reviewed": ("独立复核通过版", "独立复核未通过版"),
        "strict": ("完整视觉门禁通过版", "完整视觉门禁未通过版"),
    }
    return {
        "output": str(output),
        "slide_count": len(paths),
        "inputs": [str(path) for path in paths],
        "specs": [str(path) for path in spec_paths],
        "final_reports": [str(path) for path in final_report_paths],
        "page_ids": page_ids,
        "verification_profile": verification_profile,
        "render_identity": {
            "backend": render_identities[0][0],
            "version": render_identities[0][1],
            "executable_sha256": render_identities[0][2],
            "fontconfig_sha256": render_identities[0][3],
            "preview_size": list(render_identities[0][4]),
        },
        "delivery_label": delivery_labels[verification_profile][0 if all_passed else 1],
        "imported_slide_parts": imported_parts,
        "validation": validation,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--spec", action="append", default=[], type=Path)
    parser.add_argument("--final-report", action="append", default=[], type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = merge_presentations(
            args.input, args.spec, args.final_report, args.output
        )
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except MergeError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (OSError, ImportError, ValueError) as exc:
        payload = {"ok": False, "code": "MERGE_IO_ERROR", "message": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
