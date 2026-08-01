"""Shared independent-reviewer field contracts."""

from __future__ import annotations

from typing import Any


VISUAL_REVIEW_COVERAGE_FIELDS = {
    "canvas_and_regions",
    "objects_and_geometry",
    "text_and_typography",
    "tables_and_matrices",
    "graphics_connectors_charts",
    "pictures_crop_layers",
    "high_risk_regions",
}
VISUAL_REVIEW_COVERAGE_RESULTS = {
    "checked",
    "not_applicable",
    "not_reviewable",
}
VISUAL_REVIEW_DECISIONS = {"passed", "changes_required", "not_reviewable"}
VISUAL_REVIEW_FINDING_FIELDS = {
    "severity",
    "category",
    "location",
    "source_fact",
    "observed_difference",
    "evidence",
}


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def valid_visual_review_finding(
    value: Any,
    *,
    evidence_mode: str,
    required_severity: str | None = None,
) -> bool:
    """Validate one finding under the final or raw-response evidence profile."""
    if not isinstance(value, dict) or not VISUAL_REVIEW_FINDING_FIELDS.issubset(value):
        return False
    severity = value.get("severity")
    if (
        not isinstance(severity, str)
        or severity not in {"P0", "P1", "P2"}
        or (required_severity is not None and severity != required_severity)
        or not isinstance(value.get("category"), str)
        or value["category"] not in VISUAL_REVIEW_COVERAGE_FIELDS
        or not all(
            _nonempty_string(value.get(field))
            for field in ("location", "source_fact", "observed_difference")
        )
    ):
        return False
    evidence = value.get("evidence")
    if evidence_mode == "string":
        return _nonempty_string(evidence)
    if evidence_mode == "string_list":
        return (
            isinstance(evidence, list)
            and bool(evidence)
            and all(_nonempty_string(item) for item in evidence)
        )
    raise ValueError(f"unsupported reviewer evidence mode: {evidence_mode}")


def valid_response_p2_disclosures(value: Any) -> bool:
    """Require each raw-response disclosure to be one complete P2 finding."""
    return isinstance(value, list) and all(
        valid_visual_review_finding(
            item,
            evidence_mode="string_list",
            required_severity="P2",
        )
        for item in value
    )
