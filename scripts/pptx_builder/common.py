from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from pptx.dml.color import RGBColor

from lib.error_codes import ToolError


def rgb(value: str, path: str = "color") -> RGBColor:
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        raise ToolError("SPEC_INVALID", path, "expected #RRGGBB")
    try:
        return RGBColor.from_string(value[1:])
    except ValueError as exc:
        raise ToolError("SPEC_INVALID", path, "expected #RRGGBB") from exc


def set_shape_name(shape: Any, name: str) -> None:
    shape.name = name


class ObjectRegistry:
    def __init__(self) -> None:
        self.records: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def register(
        self,
        element_id: str,
        shape: Any,
        object_type: str,
        *,
        part: str | None = None,
        media_sha256: str | None = None,
        text_summary: str | None = None,
        font_declarations: list[str] | None = None,
    ) -> None:
        name = f"ia:{element_id}" if part is None else f"ia:{element_id}:{part}"
        set_shape_name(shape, name)
        self.records[element_id].append(
            {
                "ooxml_name": name,
                "object_type": object_type,
                "bbox": [int(shape.left), int(shape.top), int(shape.width), int(shape.height)],
                "media_sha256": media_sha256,
                "text_summary": text_summary,
                "font_declarations": font_declarations or [],
            }
        )

    def report(self) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for element_id, records in self.records.items():
            kinds = {record["object_type"] for record in records}
            output[element_id] = {
                "object_type": next(iter(kinds)) if len(kinds) == 1 else "multipart",
                "object_count": len(records),
                "ooxml_names": [record["ooxml_name"] for record in records],
                "objects": records,
            }
        return output


def require_asset(path_value: Any, hash_value: Any, field_path: str) -> Path:
    from lib.hashing import file_sha256

    if not isinstance(path_value, str) or not isinstance(hash_value, str):
        raise ToolError("MISSING_REQUIRED_FIELD", field_path, "asset path and hash required")
    path = Path(path_value).expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        raise ToolError("SPEC_INVALID", field_path, "asset missing")
    if file_sha256(path).lower() != hash_value.lower():
        raise ToolError("ASSET_HASH_MISMATCH", field_path, "asset hash mismatch")
    return path
