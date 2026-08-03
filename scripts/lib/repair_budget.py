"""Deterministic one-repair budget for page reconstruction content."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .atomic_write import atomic_write_json
from .error_codes import ToolError
from .no_replace_transactions import DirectoryLock
from .spec_identity import content_spec_sha256


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_STATE_FIELDS = {
    "state_schema_version",
    "page_id",
    "verification_profile",
    "source_sha256",
    "max_content_versions",
    "candidates",
    "repair_event",
}
_AUTHORIZATION_FIELDS = {
    "schema_version",
    "max_content_versions",
    "repair_batches",
}
_EVENT_FIELDS = {
    "batch_index",
    "source_content_spec_sha256",
    "target_content_spec_sha256",
    "trigger",
    "issue_ids",
    "status",
}
_TRIGGERS = {"deterministic_gate", "rapid_review", "reviewer_round_1"}


def _raise(code: str, path: str, detail: str) -> None:
    raise ToolError(code, path, detail)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _identity(spec: dict[str, Any]) -> dict[str, str]:
    page_id = spec.get("page_id")
    profile = spec.get("verification_profile")
    reference = spec.get("content_reference")
    source_sha256 = reference.get("sha256") if isinstance(reference, dict) else None
    if not isinstance(page_id, str) or not page_id:
        _raise("REPAIR_BUDGET_IDENTITY_INVALID", "page_id", "page_id is required")
    if profile not in {"rapid", "reviewed", "strict"}:
        _raise(
            "REPAIR_BUDGET_IDENTITY_INVALID",
            "verification_profile",
            "verification profile must be rapid, reviewed, or strict",
        )
    if not _is_sha256(source_sha256):
        _raise(
            "REPAIR_BUDGET_IDENTITY_INVALID",
            "content_reference.sha256",
            "source SHA-256 is required",
        )
    return {
        "page_id": page_id,
        "verification_profile": profile,
        "source_sha256": source_sha256.lower(),
    }


def _valid_issue_ids(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
        and len(value) == len(set(value))
    )


def _validate_event(
    value: Any,
    *,
    profile: str,
    source_hash: str,
    target_hash: str,
    path: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _EVENT_FIELDS:
        _raise(
            "REPAIR_BUDGET_AUTHORIZATION_INVALID",
            path,
            "repair event must contain exactly the required fields",
        )
    trigger = value.get("trigger")
    trigger_allowed = (
        trigger in _TRIGGERS
        and (trigger != "rapid_review" or profile == "rapid")
        and (trigger != "reviewer_round_1" or profile in {"reviewed", "strict"})
    )
    if (
        type(value.get("batch_index")) is not int
        or value.get("batch_index") != 1
        or value.get("source_content_spec_sha256") != source_hash
        or value.get("target_content_spec_sha256") != target_hash
        or not trigger_allowed
        or not _valid_issue_ids(value.get("issue_ids"))
        or value.get("status") != "consumed"
    ):
        _raise(
            "REPAIR_BUDGET_AUTHORIZATION_INVALID",
            path,
            "repair event does not authorize this single content transition",
        )
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _authorization(
    spec: dict[str, Any], *, source_hash: str, target_hash: str, profile: str
) -> dict[str, Any]:
    modules = spec.get("modules")
    high_risk = modules.get("high_risk") if isinstance(modules, dict) else None
    value = high_risk.get("repair_budget") if isinstance(high_risk, dict) else None
    if value is None:
        _raise(
            "REPAIR_BUDGET_AUTHORIZATION_REQUIRED",
            "modules.high_risk.repair_budget",
            "a second content version requires one complete repair authorization",
        )
    if not isinstance(value, dict) or set(value) != _AUTHORIZATION_FIELDS:
        _raise(
            "REPAIR_BUDGET_AUTHORIZATION_INVALID",
            "modules.high_risk.repair_budget",
            "repair authorization must contain exactly the required fields",
        )
    batches = value.get("repair_batches")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or type(value.get("max_content_versions")) is not int
        or value.get("max_content_versions") != 2
        or not isinstance(batches, list)
        or len(batches) != 1
    ):
        _raise(
            "REPAIR_BUDGET_AUTHORIZATION_INVALID",
            "modules.high_risk.repair_budget",
            "repair authorization must declare one consumed batch and two versions",
        )
    return _validate_event(
        batches[0],
        profile=profile,
        source_hash=source_hash,
        target_hash=target_hash,
        path="modules.high_risk.repair_budget.repair_batches[0]",
    )


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _raise("REPAIR_BUDGET_INVALID", str(path), str(exc))
    if not isinstance(value, dict) or set(value) != _STATE_FIELDS:
        _raise(
            "REPAIR_BUDGET_INVALID",
            str(path),
            "repair-budget state must contain exactly the required fields",
        )
    candidates = value.get("candidates")
    event = value.get("repair_event")
    if (
        type(value.get("state_schema_version")) is not int
        or value.get("state_schema_version") != 1
        or type(value.get("max_content_versions")) is not int
        or value.get("max_content_versions") != 2
        or not isinstance(value.get("page_id"), str)
        or value.get("verification_profile") not in {"rapid", "reviewed", "strict"}
        or not _is_sha256(value.get("source_sha256"))
        or not isinstance(candidates, list)
        or len(candidates) not in {1, 2}
        or len(candidates) != len(set(candidates))
        or any(not _is_sha256(item) for item in candidates)
        or (len(candidates) == 1 and event is not None)
        or (len(candidates) == 2 and not isinstance(event, dict))
    ):
        _raise(
            "REPAIR_BUDGET_INVALID",
            str(path),
            "repair-budget state is malformed",
        )
    if len(candidates) == 2:
        _validate_event(
            event,
            profile=value["verification_profile"],
            source_hash=candidates[0],
            target_hash=candidates[1],
            path=f"{path}.repair_event",
        )
    value["source_sha256"] = value["source_sha256"].lower()
    return value


def enforce_repair_budget(
    spec: dict[str, Any], state_path: str | Path, *, stage: str
) -> dict[str, Any]:
    """Register one prebuild candidate or check the current final candidate."""
    if stage not in {"prebuild", "final"}:
        raise ValueError("stage must be prebuild or final")
    path = Path(state_path).expanduser().resolve()
    identity = _identity(spec)
    current_hash = content_spec_sha256(spec)
    with DirectoryLock(path.parent):
        if not path.exists():
            if stage == "final":
                _raise(
                    "REPAIR_BUDGET_MISSING",
                    str(path),
                    "final validation requires an existing repair-budget state",
                )
            state: dict[str, Any] = {
                "state_schema_version": 1,
                **identity,
                "max_content_versions": 2,
                "candidates": [current_hash],
                "repair_event": None,
            }
            atomic_write_json(path, state)
            return state

        state = _load_state(path)
        for field, expected in identity.items():
            if state.get(field) != expected:
                _raise(
                    "REPAIR_BUDGET_IDENTITY_MISMATCH",
                    str(path),
                    f"{field} changed; start a new page batch",
                )

        candidates = state["candidates"]
        if current_hash == candidates[-1]:
            if len(candidates) == 2:
                current_event = _authorization(
                    spec,
                    source_hash=candidates[0],
                    target_hash=candidates[1],
                    profile=identity["verification_profile"],
                )
                if current_event != state["repair_event"]:
                    _raise(
                        "REPAIR_BUDGET_AUTHORIZATION_INVALID",
                        "modules.high_risk.repair_budget",
                        "current authorization differs from the registered repair event",
                    )
            return state
        if stage == "final":
            _raise(
                "REPAIR_BUDGET_CURRENT_MISMATCH",
                str(path),
                "final content hash is not the registered current candidate",
            )
        if len(candidates) >= state["max_content_versions"]:
            _raise(
                "REPAIR_BUDGET_EXHAUSTED",
                str(path),
                "the single repair batch is already consumed; a third content version is forbidden",
            )

        event = _authorization(
            spec,
            source_hash=candidates[0],
            target_hash=current_hash,
            profile=identity["verification_profile"],
        )
        updated = {
            **state,
            "candidates": [candidates[0], current_hash],
            "repair_event": event,
        }
        atomic_write_json(path, updated)
        return updated
