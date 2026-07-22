from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .error_codes import ToolError
from .hashing import file_sha256


def image_identity(path: Path | str) -> dict[str, Any]:
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise ToolError("SPEC_INVALID", str(raw), "symbolic links are not allowed")
    resolved = raw.resolve()
    if not resolved.is_file():
        raise ToolError("SPEC_INVALID", str(resolved), "image does not exist")
    try:
        with Image.open(resolved) as image:
            image.load()
            size = [image.width, image.height]
            mode = image.mode
    except (OSError, UnidentifiedImageError) as exc:
        raise ToolError("SPEC_INVALID", str(resolved), "unreadable image") from exc
    return {
        "path": str(resolved),
        "sha256": file_sha256(resolved),
        "size": size,
        "mode": mode,
    }
