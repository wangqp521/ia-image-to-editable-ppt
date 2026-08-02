#!/usr/bin/env python3
"""Create a deterministic reviewer prompt from current read-only artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from lib.atomic_write import atomic_write_bytes
from lib.final_identity import prepare_review_context
from lib.reviewer_contracts import render_reviewer_prompt, review_context_sha256


def _issue(code: str, path: str, detail: str) -> dict[str, str]:
    return {"code": code, "path": path, "detail": detail}


def _load_spec(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [_issue("FINAL_IDENTITY_INVALID", str(path), f"cannot load spec: {exc}")]
    if not isinstance(payload, dict):
        return None, [_issue("FINAL_IDENTITY_INVALID", str(path), "spec must be a JSON object")]
    return payload, []


def create_payload(
    spec: dict[str, Any], review_round: int
) -> dict[str, Any]:
    context, errors = prepare_review_context(spec, review_round)
    if context is None:
        return {"valid": False, "review_context_sha256": None, "prompt": None, "errors": errors}
    prompt = render_reviewer_prompt(context)
    return {
        "valid": True,
        "review_context_sha256": review_context_sha256(context),
        "prompt": prompt,
        "errors": [],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--review-round", type=int, required=True, choices=(1, 2))
    parser.add_argument("--prompt-output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    spec, errors = _load_spec(args.spec)
    if spec is None:
        payload = {"valid": False, "review_context_sha256": None, "prompt": None, "errors": errors}
    elif args.prompt_output is not None and spec.get("verification_profile") != "strict":
        payload = {
            "valid": False,
            "review_context_sha256": None,
            "prompt": None,
            "errors": [_issue("PROMPT_OUTPUT_NOT_ALLOWED", "prompt_output", "only strict may persist an audit prompt")],
        }
    else:
        payload = create_payload(spec, args.review_round)
    if payload["valid"] and args.prompt_output is not None:
        atomic_write_bytes(args.prompt_output, payload["prompt"].encode("utf-8"))
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
