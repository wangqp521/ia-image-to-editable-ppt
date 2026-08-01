"""Deterministic registration and final output completeness checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.element_contracts import expand_multipart_parts, expected_object_types
from lib.error_codes import ToolError
from lib.geometry import bbox_union, validate_bbox
from lib.schema_io import index_elements


class ObjectRegistry:
    """Collect rendered objects and emit a JSON-only, stable build report."""

    def __init__(self) -> None:
        self.records: dict[str, list[dict[str, Any]]] = {}
        self._names: set[str] = set()

    def register(
        self,
        element_id: str,
        shape: Any,
        object_type: str,
        *,
        semantic_kind: str,
        selected_mode: str,
        part_id: str | None = None,
        media_sha256: str | None = None,
        text_summary: str | None = None,
        font_declarations: tuple[str, ...] = (),
    ) -> None:
        """Assign an IA name and retain the live object for final inspection."""
        if not isinstance(element_id, str) or not element_id:
            self._incomplete("element_id", "element_id must be non-empty")
        if not isinstance(part_id, str | type(None)) or part_id == "":
            self._incomplete(f"elements.{element_id}.part_id", "part_id must be non-empty or null")
        if not isinstance(object_type, str) or not object_type:
            self._incomplete(f"elements.{element_id}.object_type", "object_type must be non-empty")
        if not isinstance(semantic_kind, str) or not semantic_kind:
            self._incomplete(f"elements.{element_id}.semantic_kind", "semantic_kind must be non-empty")
        if not isinstance(selected_mode, str) or not selected_mode:
            self._incomplete(f"elements.{element_id}.selected_mode", "selected_mode must be non-empty")
        if not isinstance(media_sha256, str | type(None)):
            self._incomplete(f"elements.{element_id}.media_sha256", "media_sha256 must be string or null")
        if not isinstance(text_summary, str | type(None)):
            self._incomplete(f"elements.{element_id}.text_summary", "text_summary must be string or null")
        if not isinstance(font_declarations, tuple) or not all(
            isinstance(value, str) for value in font_declarations
        ):
            self._incomplete(f"elements.{element_id}.font_declarations", "font declarations must be a string tuple")

        ooxml_name = f"ia:{element_id}" if part_id is None else f"ia:{element_id}:{part_id}"
        if ooxml_name in self._names:
            raise ToolError(
                "BUILD_OBJECT_NAME_COLLISION",
                f"elements.{element_id}",
                f"duplicate generated object name: {ooxml_name}",
            )
        try:
            shape.name = ooxml_name
            self._shape_bbox(shape, f"elements.{element_id}")
            self._shape_rotation(shape, f"elements.{element_id}")
        except (AttributeError, TypeError) as exc:
            raise ToolError(
                "BUILD_OUTPUT_INCOMPLETE",
                f"elements.{element_id}",
                "registered object does not implement the shape contract",
            ) from exc
        self._names.add(ooxml_name)
        self.records.setdefault(element_id, []).append(
            {
                "ooxml_name": ooxml_name,
                "object_type": object_type,
                "part_id": part_id,
                "media_sha256": media_sha256,
                "text_summary": text_summary,
                "font_declarations": font_declarations,
                "semantic_kind": semantic_kind,
                "selected_mode": selected_mode,
                "_shape": shape,
            }
        )

    def finalize(
        self,
        spec: dict[str, Any],
        representation_modes: Mapping[str, str],
    ) -> dict[str, Any]:
        """Verify all schema elements against live PPTX objects and serialize them."""
        try:
            elements = index_elements(spec)
        except ToolError as exc:
            self._incomplete(exc.path, exc.detail)
        modes = dict(representation_modes)
        if set(modes) != set(elements):
            self._incomplete(
                "modules.representation_plan.items",
                "every element must have exactly one selected mode",
            )
        if set(self.records) != set(elements):
            self._incomplete("elements", "registered elements must exactly match schema elements")

        report: dict[str, Any] = {}
        for element_id in sorted(elements):
            element = elements[element_id]
            kind = element.get("kind")
            records = self.records[element_id]
            expected_mode = modes.get(element_id)
            if not isinstance(kind, str) or expected_mode is None:
                self._incomplete(f"elements.{element_id}", "element kind or representation mode is missing")
            if any(record["semantic_kind"] != kind for record in records):
                self._incomplete(f"elements.{element_id}", "registered semantic kind does not match schema")
            if any(record["selected_mode"] != expected_mode for record in records):
                self._incomplete(f"elements.{element_id}", "registered mode does not match representation plan")
            allowed_types = expected_object_types(kind)
            if not allowed_types or any(record["object_type"] not in allowed_types for record in records):
                self._incomplete(f"elements.{element_id}", "registered object type does not match schema kind")

            expected_parts = self._expected_parts(element, element_id)
            actual_parts = {record["part_id"] for record in records}
            if actual_parts != set(expected_parts) or len(records) != len(expected_parts):
                self._incomplete(f"elements.{element_id}", "registered parts do not match schema parts")
            objects: list[dict[str, Any]] = []
            for record in sorted(records, key=lambda value: value["ooxml_name"]):
                expected_bbox = expected_parts[record["part_id"]]
                bbox = self._shape_bbox(record["_shape"], f"elements.{element_id}")
                if bbox != expected_bbox:
                    self._incomplete(f"elements.{element_id}", "actual object bbox does not match schema")
                if getattr(record["_shape"], "name", None) != record["ooxml_name"]:
                    self._incomplete(f"elements.{element_id}", "actual object name does not match registry")
                objects.append(
                    {
                        "ooxml_name": record["ooxml_name"],
                        "object_type": record["object_type"],
                        "bbox": bbox,
                        "rotation": self._shape_rotation(record["_shape"], f"elements.{element_id}"),
                        "part_id": record["part_id"],
                        "media_sha256": record["media_sha256"],
                        "text_summary": record["text_summary"],
                        "font_declarations": list(record["font_declarations"]),
                    }
                )
            if kind in {"matrix", "status"}:
                parent_bbox = validate_bbox(
                    element.get("slide_bbox"), f"elements.{element_id}.slide_bbox"
                )
                if bbox_union([item["bbox"] for item in objects]) != parent_bbox:
                    self._incomplete(
                        f"elements.{element_id}",
                        "registered multipart objects do not union to the parent bbox",
                    )
            object_types = {record["object_type"] for record in records}
            report[element_id] = {
                "semantic_kind": kind,
                "selected_mode": expected_mode,
                "object_type": next(iter(object_types)) if len(object_types) == 1 else "mixed",
                "objects": objects,
            }
        return report

    def _expected_parts(self, element: dict[str, Any], element_id: str) -> dict[str | None, list[int]]:
        if element.get("kind") not in {"matrix", "status"}:
            return {None: validate_bbox(element.get("slide_bbox"), f"elements.{element_id}.slide_bbox")}
        try:
            parts = expand_multipart_parts(element)
        except ToolError as exc:
            self._incomplete(exc.path, exc.detail)
        return {
            part["part_id"]: validate_bbox(
                part.get("slide_bbox"), f"elements.{element_id}.content.parts"
            )
            for part in parts
        }

    @staticmethod
    def _shape_bbox(shape: Any, path: str) -> list[int]:
        values = [shape.left, shape.top, shape.width, shape.height]
        if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            values = [int(value) for value in values]
        return validate_bbox(values, f"{path}.bbox")

    @staticmethod
    def _shape_rotation(shape: Any, path: str) -> int | float:
        rotation = shape.rotation
        if type(rotation) not in {int, float}:
            raise ToolError("BUILD_OUTPUT_INCOMPLETE", f"{path}.rotation", "rotation must be numeric")
        return rotation

    @staticmethod
    def _incomplete(path: str, detail: str) -> None:
        raise ToolError("BUILD_OUTPUT_INCOMPLETE", path, detail)
