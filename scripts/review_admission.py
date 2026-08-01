#!/usr/bin/env python3
"""Issue immutable reviewer admissions and record reviewer invocations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lib.error_codes import ToolError
from lib.review_contracts import (
    AdmissionInputs,
    issue_admission,
    record_invocation,
    validate_response,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    issue = commands.add_parser("issue", help="issue a new immutable admission")
    issue.add_argument("--spec", type=Path, required=True)
    issue.add_argument("--pptx", type=Path, required=True)
    issue.add_argument("--build-report", type=Path, required=True)
    issue.add_argument("--structure-report", type=Path, required=True)
    issue.add_argument("--render-report", type=Path, required=True)
    issue.add_argument("--text-geometry", type=Path, required=True)
    issue.add_argument("--background-report", type=Path, required=True)
    issue.add_argument("--visual-diff", type=Path, required=True)
    issue.add_argument("--review-round", type=int, choices=(1, 2), required=True)
    issue.add_argument("--prior-admission", type=Path)
    issue.add_argument("--prior-invocation", type=Path)
    issue.add_argument("--prior-response-validation", type=Path)
    issue.add_argument("--output-dir", type=Path, required=True)

    invoke = commands.add_parser("invoke", help="consume an admission before review")
    invoke.add_argument("--admission", type=Path, required=True)
    invoke.add_argument("--invocation-dir", type=Path, required=True)

    response = commands.add_parser(
        "validate-response", help="validate an immutable reviewer response"
    )
    response.add_argument("--admission", type=Path, required=True)
    response.add_argument("--invocation", type=Path, required=True)
    response.add_argument("--response", type=Path, required=True)
    response.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "issue":
            result = issue_admission(
                AdmissionInputs(
                    spec=args.spec,
                    pptx=args.pptx,
                    build_report=args.build_report,
                    structure_report=args.structure_report,
                    render_report=args.render_report,
                    text_geometry=args.text_geometry,
                    background_report=args.background_report,
                    visual_diff=args.visual_diff,
                    review_round=args.review_round,
                    prior_admission=args.prior_admission,
                    prior_invocation=args.prior_invocation,
                    prior_response_validation=args.prior_response_validation,
                ),
                args.output_dir,
            )
        elif args.command == "invoke":
            result = record_invocation(args.admission, args.invocation_dir)
        else:
            result = validate_response(
                args.admission, args.invocation, args.response, args.output
            )
    except ToolError as exc:
        print(
            json.dumps({"valid": False, "error": exc.as_dict()}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("valid", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
