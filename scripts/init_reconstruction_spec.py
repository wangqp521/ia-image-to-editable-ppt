#!/usr/bin/env python3
"""Initialize an evidence-bound, prebuild-incomplete schema-v2 skeleton."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from create_coordinate_overlay import (  # noqa: E402
    MANIFEST_METADATA_KEY,
    coordinate_overlay_manifest,
)
from lib.error_codes import ToolError  # noqa: E402
from lib.hashing import file_sha256  # noqa: E402
from lib.path_contracts import find_user_controlled_symlink  # noqa: E402
from lib.schema_contracts import (  # noqa: E402
    VERIFICATION_PROFILES,
    construct_record,
    json_schema_document,
    schema_contract_manifest,
    schema_contract_sha256,
)


PAGE_ID_PATTERN = re.compile(r"^page-[0-9]{3}$")
SLIDE_SIZE_EMU = [12_192_000, 6_858_000]
MAX_IMAGE_PIXELS = 100_000_000


def _error(code: str, path: str | Path, detail: str) -> ToolError:
    return ToolError(code, str(path), detail)


def _require_literal_file(path: Path, label: str) -> Path:
    raw = str(path)
    if "\x00" in raw or not path.is_absolute():
        raise _error(
            "INIT_PATH_INVALID", label, f"{label} must be a literal absolute path"
        )
    try:
        if path.is_symlink() or not path.is_file():
            raise _error(
                "INIT_PATH_INVALID",
                label,
                f"{label} must be a readable non-symlink file",
            )
        return path.resolve(strict=True)
    except ToolError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error("INIT_PATH_INVALID", label, f"cannot inspect {label}") from exc


def _image_identity(path: Path, label: str) -> tuple[dict[str, str], list[int]]:
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("image dimensions exceed the supported limit")
            image.load()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise _error("INIT_IMAGE_INVALID", label, f"{label} must be a decodable image") from exc
    identity = construct_record(
        "Reference", path=str(path), sha256=file_sha256(path)
    )
    return identity, [width, height]


def _overlay_evidence(
    overlay: Path,
    visual: Path,
    visual_identity: dict[str, str],
    visual_size: list[int],
) -> dict[str, Any]:
    if overlay.suffix.lower() != ".png":
        raise _error("INIT_OVERLAY_INVALID", "overlay", "overlay must be a PNG")
    expected = coordinate_overlay_manifest(visual)
    try:
        with Image.open(overlay) as image:
            image.load()
            actual_size = list(image.size)
            metadata_hash = image.info.get(MANIFEST_METADATA_KEY)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise _error(
            "INIT_OVERLAY_INVALID", "overlay", "overlay must be a decodable PNG"
        ) from exc
    expected_hash = expected[MANIFEST_METADATA_KEY]
    if actual_size != visual_size or metadata_hash != expected_hash:
        raise _error(
            "INIT_OVERLAY_INVALID",
            "overlay",
            "overlay must bind the current visual using the documented default grid",
        )
    return construct_record(
        "CoordinateOverlayEvidence",
        path=str(overlay),
        sha256=file_sha256(overlay),
        source_sha256=visual_identity["sha256"],
        manifest_sha256=expected_hash,
        grid=expected["grid"],
        inspection="passed",
    )


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback_published_link(path: Path, parent: Path) -> None:
    """Best-effort rollback without replacing the original publication error."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _sync_directory(parent)
    except OSError:
        pass


def atomic_write_json_no_overwrite(path: Path, payload: Any) -> None:
    """Durably publish JSON by link, so a racing writer cannot be overwritten."""
    if path.exists() or path.is_symlink():
        raise FileExistsError(str(path))
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise OSError("output parent must be an existing real directory")
    encoded = (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.link(temporary_name, path)
        try:
            _sync_directory(parent)
        except OSError:
            _rollback_published_link(path, parent)
            raise
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def initialize_reconstruction_spec(
    *,
    source: Path,
    overlay: Path,
    page_id: str,
    output: Path,
    visual: Path | None = None,
    profile: str = "strict",
) -> dict[str, Any]:
    """Create one real-identity skeleton without inventing page content."""
    if not isinstance(page_id, str) or PAGE_ID_PATTERN.fullmatch(page_id) is None:
        raise _error("INIT_PAGE_ID_INVALID", "page_id", "expected page-NNN")
    if profile not in VERIFICATION_PROFILES:
        raise _error(
            "INIT_PROFILE_INVALID",
            "profile",
            "profile must be rapid, reviewed, or strict",
        )
    source = _require_literal_file(Path(source), "source")
    visual = _require_literal_file(Path(visual) if visual is not None else source, "visual")
    overlay = _require_literal_file(Path(overlay), "overlay")
    output = Path(output)
    if "\x00" in str(output) or not output.is_absolute():
        raise _error(
            "INIT_OUTPUT_INVALID",
            "output",
            "output must be a literal absolute path",
        )
    try:
        symlink = find_user_controlled_symlink(output)
        output_exists = output.exists() or symlink == output
        parent_is_directory = output.parent.is_dir()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "INIT_OUTPUT_INVALID", output, "cannot inspect output path"
        ) from exc
    if output_exists:
        raise _error("INIT_OUTPUT_EXISTS", output, "output path already exists")
    if symlink is not None or not parent_is_directory:
        raise _error(
            "INIT_OUTPUT_INVALID",
            symlink if symlink is not None else output.parent,
            "output parent must be an existing real directory",
        )

    source_identity, source_size = _image_identity(source, "source")
    visual_identity, visual_size = _image_identity(visual, "visual")
    overlay_evidence = _overlay_evidence(
        overlay, visual, visual_identity, visual_size
    )
    overlay_manifest = coordinate_overlay_manifest(visual)
    background_element_id = "background-base"
    spec = construct_record(
        "PageReconstruction",
        schema_version=2,
        page_id=page_id,
        verification_profile=profile,
        delivery_status="pending",
        session_reuse=construct_record(
            "SessionReuse",
            mode="fresh_reconstruction",
            reason="new_session",
            artifacts=[],
        ),
        content_reference=source_identity,
        clean_visual_reference=visual_identity,
        canvas=construct_record(
            "Canvas",
            source_size=source_size,
            visual_size=visual_size,
            page_frame_bbox=[0, 0, *visual_size],
            slide_size_emu=list(SLIDE_SIZE_EMU),
            mapping_mode=overlay_manifest["mapping"]["mode"],
            background="unmeasured",
        ),
        activated_modules=[
            "page_layout",
            "typography",
            "representation_plan",
            "background",
        ],
        modules={
            "page_layout": construct_record(
                "PageLayoutModule",
                anchors=[],
                relationships=[],
                layout_invariants=[],
                density_targets={},
                coordinate_overlay_evidence=overlay_evidence,
            ),
            "typography": construct_record(
                "TypographyModule", slide_coordinate_unit="EMU", items=[]
            ),
            "representation_plan": construct_record(
                "RepresentationPlanModule", items=[]
            ),
            "background": construct_record(
                "BackgroundModule",
                items=[
                    construct_record(
                        "BackgroundItem",
                        background_id="background-001",
                        role="base",
                        source_bbox=[0, 0, *visual_size],
                        selected_mode="native",
                        bound_element_id=background_element_id,
                        source_provenance=construct_record(
                            "BackgroundProvenance",
                            kind="native_measurement",
                            source_path=visual_identity["path"],
                            source_sha256=visual_identity["sha256"],
                        ),
                        reason="initialized measurable native page background",
                        evidence=[visual_identity["path"]],
                        contains_foreground_semantics=False,
                    )
                ],
            ),
        },
        regions=[
            construct_record(
                "Region",
                region_id="region-001",
                source_bbox=[0, 0, *visual_size],
                slide_bbox=[0, 0, *SLIDE_SIZE_EMU],
                layer=10,
                padding={"left": 0, "right": 0, "top": 0, "bottom": 0},
                element_ids=[background_element_id],
            )
        ],
        elements=[
            construct_record(
                "Element",
                element_id=background_element_id,
                kind="shape",
                source_bbox=[0, 0, *visual_size],
                slide_bbox=[0, 0, *SLIDE_SIZE_EMU],
                layer=0,
                editable=True,
                confidence="high",
                style={
                    "shape_type": "rectangle",
                    "fill": {"type": "solid", "color": "#FFFFFF", "opacity": 1},
                    "effects": "none",
                    "rotation": 0,
                },
                content={},
            )
        ],
        reading_order=[background_element_id],
        visual_gate=construct_record(
            "VisualGate", status="pending", evidence=[], tripwire=None
        ),
        editability_gate=construct_record(
            "EditabilityGate", status="pending", evidence=[]
        ),
    )
    try:
        atomic_write_json_no_overwrite(output, spec)
    except FileExistsError as exc:
        raise _error("INIT_OUTPUT_EXISTS", output, "output path already exists") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "INIT_OUTPUT_WRITE_FAILED", output, "could not atomically publish output"
        ) from exc
    return spec


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--describe", action="store_true", help="emit shared machine contracts and JSON Schema")
    parser.add_argument("--source", type=Path, help="absolute content-source image path")
    parser.add_argument("--visual", type=Path, help="absolute clean visual path; defaults to --source")
    parser.add_argument("--overlay", type=Path, help="absolute default-grid coordinate overlay PNG")
    parser.add_argument("--page-id", help="page id in exact page-NNN form")
    parser.add_argument("--profile", default="strict", help="rapid, reviewed, or strict; default strict")
    parser.add_argument("--output", type=Path, help="new page-reconstruction.json path; never overwritten")
    return parser.parse_args(argv)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.describe:
        _emit(
            {
                "contract": schema_contract_manifest(),
                "json_schema": json_schema_document(),
            }
        )
        return 0
    try:
        missing = [
            name
            for name in ("source", "overlay", "page_id", "output")
            if getattr(args, name) is None
        ]
        if missing:
            raise _error(
                "INIT_ARGUMENT_MISSING",
                "$",
                f"missing required arguments: {', '.join(missing)}",
            )
        initialize_reconstruction_spec(
            source=args.source,
            visual=args.visual,
            overlay=args.overlay,
            page_id=args.page_id,
            profile=args.profile,
            output=args.output,
        )
        _emit(
            {
                "ok": True,
                "output": str(args.output.resolve()),
                "schema_contract_sha256": schema_contract_sha256(),
            }
        )
        return 0
    except ToolError as exc:
        _emit({"ok": False, "errors": [exc.as_dict()]})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
