"""Fixed-font runtime contracts shared by preflight, build, and render stages."""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from typing import Any


SUPPORTED_FAMILIES = frozenset(
    {"Hiragino Sans GB", "Microsoft YaHei", "STKaiti"}
)
SUPPORTED_PROVIDERS = frozenset({"fontconfig", "windows-system"})
REQUIRED_BOLD_FAMILIES = frozenset({"Hiragino Sans GB", "Microsoft YaHei"})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PDF_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


def _fail(code: str, detail: str) -> None:
    raise ValueError(f"{code}: {detail}")


def _absolute_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("RUNTIME_FIXED_FONT_INVALID", f"{field}.path must be non-empty")
    if not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        _fail("RUNTIME_FIXED_FONT_INVALID", f"{field}.path must be absolute")
    return value


def _font_file(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("RUNTIME_FIXED_FONT_INVALID", f"{field} must be an object")
    path = _absolute_path(value.get("path"), field)
    sha256 = value.get("sha256")
    if not isinstance(sha256, str) or _SHA256_PATTERN.fullmatch(sha256) is None:
        _fail("RUNTIME_FIXED_FONT_INVALID", f"{field}.sha256 must be lowercase SHA-256")
    face_index = value.get("face_index")
    if isinstance(face_index, bool) or not isinstance(face_index, int) or face_index < 0:
        _fail(
            "RUNTIME_FIXED_FONT_INVALID",
            f"{field}.face_index must be a non-negative integer",
        )
    return {"path": path, "sha256": sha256, "face_index": face_index}


def validate_font_runtime(value: Any) -> dict[str, Any]:
    """Return a normalized, defensive copy of one fixed-font runtime."""
    if not isinstance(value, dict):
        _fail("RUNTIME_FIXED_FONT_INVALID", "font_runtime must be an object")
    if value.get("policy") != "fixed":
        _fail("RUNTIME_FIXED_FONT_INVALID", "policy must be fixed")
    if value.get("allow_substitution") is not False:
        _fail("RUNTIME_FIXED_FONT_INVALID", "allow_substitution must be false")

    family = value.get("family")
    if family not in SUPPORTED_FAMILIES:
        _fail("RUNTIME_FIXED_FONT_UNSUPPORTED", f"unsupported family {family!r}")
    provider = value.get("provider")
    if provider not in SUPPORTED_PROVIDERS:
        _fail("RUNTIME_FONT_PROVIDER_UNSUPPORTED", f"unsupported provider {provider!r}")

    regular = _font_file(value.get("regular"), "regular")
    bold_value = value.get("bold")
    bold = None if bold_value is None else _font_file(bold_value, "bold")
    bold_available = value.get("bold_available")
    if not isinstance(bold_available, bool) or bold_available != (bold is not None):
        _fail(
            "RUNTIME_FIXED_FONT_INVALID",
            "bold_available must exactly match the bold artifact",
        )
    if bold is not None and (
        bold["path"],
        bold["face_index"],
    ) == (
        regular["path"],
        regular["face_index"],
    ):
        _fail(
            "RUNTIME_FIXED_FONT_BOLD_MISSING",
            f"{family} Regular and Bold resolve to the same font face",
        )
    if family in REQUIRED_BOLD_FAMILIES and bold is None:
        _fail(
            "RUNTIME_FIXED_FONT_BOLD_MISSING",
            f"{family} requires a true Bold font face",
        )

    return {
        "policy": "fixed",
        "family": family,
        "provider": provider,
        "allow_substitution": False,
        "regular": regular,
        "bold": bold,
        "bold_available": bold_available,
    }


def font_runtime_identity(value: Any) -> tuple[Any, ...]:
    """Return the fields that identify font bytes and collection faces."""
    runtime = validate_font_runtime(value)
    return (
        runtime["family"],
        runtime["provider"],
        runtime["regular"]["sha256"],
        runtime["regular"]["face_index"],
        runtime["bold"]["sha256"] if runtime["bold"] else None,
        runtime["bold"]["face_index"] if runtime["bold"] else None,
    )


def pdf_font_name_matches(family: str, resolved_name: str) -> bool:
    """Check a pdffonts family name without treating a fallback as equivalent."""
    if family not in SUPPORTED_FAMILIES or not isinstance(resolved_name, str):
        return False
    without_subset = _PDF_SUBSET_PREFIX.sub("", resolved_name.strip())
    normalized = "".join(
        character for character in without_subset if character.isalnum()
    ).casefold()
    allowed = {
        "Hiragino Sans GB": {
            "hiraginosansgbw3",
            "hiraginosansgbw6",
        },
        "Microsoft YaHei": {"microsoftyahei", "microsoftyaheibold"},
        "STKaiti": {"stkaiti"},
    }
    return normalized in allowed[family]
