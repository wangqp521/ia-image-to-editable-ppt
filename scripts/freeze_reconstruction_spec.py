#!/usr/bin/env python3
"""Publish one immutable build or pre-review reconstruction-spec snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from lib.atomic_write import publish_json_no_overwrite
from lib.error_codes import ToolError
from lib.schema_io import NonStandardJsonNumberError, reject_nonstandard_json_number
from lib.spec_identity import (
    content_spec_sha256,
    input_spec_sha256,
    review_state_sha256,
)
from validate_reconstruction_spec import validate_spec


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="mutable working reconstruction spec")
    parser.add_argument(
        "--purpose",
        choices=("build", "pre-review"),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def _load_spec(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    payload = json.loads(
        raw.decode("utf-8"),
        parse_constant=reject_nonstandard_json_number,
    )
    if not isinstance(payload, dict):
        raise ToolError("SPEC_SNAPSHOT_INVALID", str(path), "spec root must be an object")
    return payload, raw


def freeze_spec(spec_path: Path, output: Path, purpose: str) -> dict[str, Any]:
    spec, source_bytes = _load_spec(spec_path)
    if purpose == "build":
        validation = validate_spec(spec, stage="prebuild")
        if validation.get("valid") is not True:
            raise ToolError(
                "SPEC_SNAPSHOT_INVALID",
                str(spec_path),
                "build snapshot requires a passing production prebuild validation",
            )
    receipt = publish_json_no_overwrite(output, spec)
    resolved_source = spec_path.expanduser().resolve()
    resolved_output = receipt.destination.resolve()
    return {
        "valid": True,
        "purpose": purpose,
        "source": {
            "path": str(resolved_source),
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
        },
        "snapshot": {
            "path": str(resolved_output),
            "sha256": receipt.sha256,
        },
        "content_spec_sha256": content_spec_sha256(spec),
        "input_spec_sha256": input_spec_sha256(spec),
        "review_state_sha256": review_state_sha256(spec),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = freeze_spec(args.spec, args.output, args.purpose)
    except (
        ToolError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        NonStandardJsonNumberError,
        TypeError,
        ValueError,
    ) as exc:
        error = (
            exc.as_dict()
            if isinstance(exc, ToolError)
            else {
                "code": "SPEC_SNAPSHOT_INVALID",
                "path": str(args.spec),
                "detail": str(exc),
            }
        )
        print(json.dumps({"valid": False, "error": error}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
