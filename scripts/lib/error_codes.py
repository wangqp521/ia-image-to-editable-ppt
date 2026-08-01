"""Stable machine-readable errors for compiler contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolError(ValueError):
    """A user-correctable tool failure with a stable error code."""

    code: str
    path: str
    detail: str
    capability: str | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {"code": self.code, "path": self.path, "detail": self.detail}
        if self.capability is not None:
            payload["capability"] = self.capability
        return payload


@dataclass(frozen=True)
class ContractIssue:
    """A non-throwing validation issue returned by a contract checker."""

    code: str
    path: str
    detail: str
    capability: str | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {"code": self.code, "path": self.path, "detail": self.detail}
        if self.capability is not None:
            payload["capability"] = self.capability
        return payload
