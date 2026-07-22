#!/usr/bin/env python3
"""Batch-extract explicit schema-v2 raster assets without semantic guessing."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from PIL import Image

from asset_handlers import (
    AssetJob,
    AssetResult,
    extract_background_preserved,
    extract_source_patch,
    extract_with_explicit_mask,
)
from extract_icon_asset import extract_icon_asset
from lib.atomic_write import atomic_write_json
from lib.error_codes import ToolError
from lib.hashing import canonical_json_sha256, file_sha256
from lib.schema_io import load_schema_v2


GENERIC_PROCESSORS = {
    "source_patch": extract_source_patch,
    "background_preserved": extract_background_preserved,
    "explicit_mask": extract_with_explicit_mask,
}


def _int_bbox(value: Any, path: str) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(item) is not int for item in value)
        or value[0] < 0
        or value[1] < 0
        or value[2] <= 0
        or value[3] <= 0
    ):
        raise ToolError("SPEC_INVALID", path, "expected integer XYWH")
    return tuple(value)


def _generic_jobs(
    spec: dict[str, Any], temporary_assets: Path, final_assets: Path
) -> list[tuple[AssetJob, dict[str, Any], Path]]:
    source = Path(spec.get("clean_visual_reference", {}).get("path", "")).expanduser().resolve()
    if not source.is_file():
        raise ToolError("SPEC_INVALID", "clean_visual_reference.path", "source missing")
    jobs: list[tuple[AssetJob, dict[str, Any], Path]] = []
    for index, element in enumerate(spec.get("elements", [])):
        if not isinstance(element, dict):
            continue
        content = element.get("content")
        asset = content.get("asset") if isinstance(content, dict) else None
        if not isinstance(asset, dict):
            continue
        element_id = element.get("element_id")
        kind = element.get("kind")
        path = f"elements[{index}].content.asset"
        if not isinstance(element_id, str) or not isinstance(kind, str):
            raise ToolError("SPEC_INVALID", path, "element_id and kind required")
        processor = asset.get("processor")
        if not isinstance(processor, str):
            raise ToolError("MISSING_REQUIRED_FIELD", f"{path}.processor", "processor required")
        if processor == "alpha_isolation" and kind != "icon":
            raise ToolError(
                "ALPHA_EXTRACTION_UNSAFE",
                f"{path}.processor",
                "non-icon alpha isolation requires explicit_mask",
            )
        if processor not in GENERIC_PROCESSORS:
            raise ToolError("UNSUPPORTED_FEATURE", f"{path}.processor", processor)
        padding = asset.get("padding", 0)
        if type(padding) is not int or padding < 0:
            raise ToolError("SPEC_INVALID", f"{path}.padding", "expected non-negative integer")
        mask_value = asset.get("mask_path")
        mask = Path(mask_value).expanduser().resolve() if isinstance(mask_value, str) else None
        relative = Path("pictures") / f"{element_id}.png"
        job = AssetJob(
            element_id=element_id,
            kind=kind,
            processor=processor,
            source_path=source,
            source_bbox=_int_bbox(element.get("source_bbox"), f"elements[{index}].source_bbox"),
            output_path=temporary_assets / relative,
            field_path=path,
            padding=padding,
            tolerance=asset.get("tolerance"),
            mask_path=mask,
        )
        jobs.append((job, asset, final_assets / relative))
    return jobs


def _icon_jobs(
    spec: dict[str, Any], temporary_assets: Path, final_assets: Path
) -> list[tuple[AssetJob, dict[str, Any], Path]]:
    module = spec.get("modules", {}).get("icons")
    if not isinstance(module, dict):
        return []
    jobs: list[tuple[AssetJob, dict[str, Any], Path]] = []
    for index, icon in enumerate(module.get("icons", [])):
        if not isinstance(icon, dict):
            raise ToolError("SPEC_INVALID", f"modules.icons.icons[{index}]", "expected object")
        element_id = icon.get("element_id")
        crop_mode = icon.get("crop_mode")
        if not isinstance(element_id, str) or crop_mode not in {"alpha_isolation", "background_preserved"}:
            raise ToolError("SPEC_INVALID", f"modules.icons.icons[{index}]", "invalid icon job")
        source = Path(icon.get("source_path", "")).expanduser().resolve()
        padding = icon.get("padding", 0)
        bbox = _int_bbox(icon.get("source_bbox"), f"modules.icons.icons[{index}].source_bbox")
        padded = (bbox[0] - padding, bbox[1] - padding, bbox[2] + padding * 2, bbox[3] + padding * 2)
        if padded[0] < 0 or padded[1] < 0:
            raise ToolError("BBOX_OUT_OF_RANGE", f"modules.icons.icons[{index}].source_bbox", "padding exceeds source")
        relative = Path("icons") / f"{element_id}.png"
        job = AssetJob(
            element_id=element_id,
            kind="icon",
            processor=crop_mode,
            source_path=source,
            source_bbox=padded,
            output_path=temporary_assets / relative,
            field_path=f"modules.icons.icons[{index}]",
            padding=0,
            tolerance=icon.get("tolerance", 24),
        )
        jobs.append((job, icon, final_assets / relative))
    return jobs


def _run_job(job: AssetJob) -> AssetResult:
    if job.kind == "icon":
        try:
            result = extract_icon_asset(
                job.source_path,
                job.output_path,
                job.source_bbox,
                icon_id=job.element_id,
                crop_mode=job.processor,
                tolerance=24 if job.tolerance is None else job.tolerance,
            )
        except ValueError as exc:
            code = "BBOX_OUT_OF_RANGE" if "bbox" in str(exc) else "ALPHA_EXTRACTION_UNSAFE"
            raise ToolError(code, job.field_path, str(exc)) from exc
        return AssetResult(
            element_id=job.element_id,
            processor=job.processor,
            output_path=job.output_path,
            asset_sha256=result["asset_sha256"],
            alpha_mask_sha256=result["alpha_mask_sha256"],
            width=result["size"][0],
            height=result["size"][1],
            source_bbox=job.source_bbox,
            touches_edge=result["touches_edge"],
            source_sha256=result["source_sha256"],
        )
    return GENERIC_PROCESSORS[job.processor](job)


def _job_input_sha256(job: AssetJob) -> str:
    return canonical_json_sha256(
        {
            "element_id": job.element_id,
            "kind": job.kind,
            "processor": job.processor,
            "source_sha256": file_sha256(job.source_path),
            "source_bbox": list(job.source_bbox),
            "padding": job.padding,
            "tolerance": job.tolerance,
            "mask_sha256": file_sha256(job.mask_path) if job.mask_path is not None and job.mask_path.is_file() else None,
        }
    )


def _reuse_result(
    job: AssetJob, record: dict[str, Any], final_path: Path
) -> AssetResult | None:
    declared_path = record.get("asset_path")
    declared_hash = record.get("asset_sha256")
    if (
        declared_path != str(final_path)
        or not isinstance(declared_hash, str)
        or not final_path.is_file()
        or file_sha256(final_path) != declared_hash
        or record.get("asset_input_sha256") != _job_input_sha256(job)
    ):
        return None
    try:
        with Image.open(final_path) as image:
            image.load()
            size = image.size
    except OSError:
        return None
    if [record.get("final_width"), record.get("final_height")] != list(size):
        return None
    return AssetResult(
        element_id=job.element_id,
        processor=job.processor,
        output_path=final_path,
        asset_sha256=declared_hash,
        alpha_mask_sha256=record.get("alpha_mask_sha256"),
        width=size[0],
        height=size[1],
        source_bbox=job.source_bbox,
        touches_edge=record.get(
            "touches_edge",
            {"top": True, "right": True, "bottom": True, "left": True},
        ),
        source_sha256=file_sha256(job.source_path),
    )


def extract_assets(
    spec: dict[str, Any], assets_dir: Path | str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if spec.get("schema_version") != 2:
        raise ToolError("SPEC_SCHEMA_VERSION_UNSUPPORTED", "schema_version", "expected 2")
    final_assets = Path(assets_dir).expanduser().resolve()
    updated = copy.deepcopy(spec)
    before_hash = canonical_json_sha256(updated)
    parent = final_assets.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ia-assets-", dir=parent) as directory:
        temporary_assets = Path(directory) / "assets"
        jobs = _generic_jobs(updated, temporary_assets, final_assets)
        jobs.extend(_icon_jobs(updated, temporary_assets, final_assets))
        completed: list[tuple[AssetResult, dict[str, Any], Path, bool, str]] = []
        for job, record, final_path in jobs:
            input_hash = _job_input_sha256(job)
            reused = _reuse_result(job, record, final_path)
            completed.append(
                (reused if reused is not None else _run_job(job), record, final_path, reused is not None, input_hash)
            )

        for result, record, final_path, reused, input_hash in completed:
            if not reused:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(result.output_path, final_path)
            record["asset_path"] = str(final_path)
            record["asset_sha256"] = file_sha256(final_path)
            record["source_sha256"] = result.source_sha256
            record["asset_input_sha256"] = input_hash
            record["alpha_mask_sha256"] = result.alpha_mask_sha256
            record["final_width"] = result.width
            record["final_height"] = result.height
            record["touches_edge"] = result.touches_edge
            if result.processor == "alpha_isolation":
                record["fallback_reason"] = None

    report_items = []
    for result, _, final_path, reused, _ in completed:
        item = result.as_report()
        item["output"] = str(final_path)
        item["asset_sha256"] = file_sha256(final_path)
        item["reused"] = reused
        report_items.append(item)
    report = {
        "valid": True,
        "schema_version": 2,
        "spec_sha256_before": before_hash,
        "spec_sha256_after": canonical_json_sha256(updated),
        "source_sha256": updated.get("clean_visual_reference", {}).get("sha256"),
        "cache_hit": bool(completed) and all(item[3] for item in completed),
        "items": report_items,
        "errors": [],
    }
    return updated, report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--assets-dir", type=Path, required=True)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--in-place", action="store_true")
    output.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        spec = load_schema_v2(args.spec)
        updated, report = extract_assets(spec, args.assets_dir)
        destination = args.spec if args.in_place else args.output
        if destination is None:
            raise ToolError("SPEC_INVALID", "--output", "output path required")
        atomic_write_json(destination, updated)
        atomic_write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ToolError, OSError, json.JSONDecodeError) as exc:
        error = exc if isinstance(exc, ToolError) else ToolError("SPEC_INVALID", "$", str(exc))
        report = {"valid": False, "errors": [error.as_dict()]}
        atomic_write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
