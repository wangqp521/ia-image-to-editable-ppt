"""Shared fail-closed path inspection helpers."""

from __future__ import annotations

from pathlib import Path


def find_user_controlled_symlink(candidate: Path) -> Path | None:
    """Return the nearest symlink, tolerating platform aliases only in ancestors."""
    filesystem_root = Path(candidate.anchor) if candidate.is_absolute() else None
    for entry in (candidate, *candidate.parents):
        if entry != candidate and filesystem_root is not None and (
            entry == filesystem_root or entry.parent == filesystem_root
        ):
            continue
        if entry.is_symlink():
            return entry
    return None
