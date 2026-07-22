from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolError(ValueError):
    code: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.path}: {self.detail}"

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}
