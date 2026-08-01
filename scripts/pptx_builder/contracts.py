"""Shared fail-closed contract gate for statically registered renderers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.element_contracts import validate_element_contract
from lib.error_codes import ContractIssue, ToolError

from .common import RENDERERS, RenderContext
from .registry import ObjectRegistry


def validate_renderer_contracts(
    spec: dict[str, Any],
    elements: Mapping[str, dict[str, Any]],
    representation_modes: Mapping[str, str],
    typography: Mapping[str, dict[str, Any]],
) -> list[ContractIssue]:
    """Return deterministic element and renderer issues without rendering."""
    resolved_modes = dict(representation_modes)
    if set(resolved_modes) != set(elements):
        return [
            ContractIssue(
                "REPRESENTATION_INCOMPLETE",
                "modules.representation_plan.items",
                "every element must have exactly one selected mode",
            )
        ]
    context = RenderContext(
        slide=None,
        spec=spec,
        representation_modes=resolved_modes,
        typography=typography,
        registry=ObjectRegistry(),
    )
    issues: list[ContractIssue] = []
    for element_id, element in elements.items():
        element_issues = validate_element_contract(element)
        if element_issues:
            issues.extend(element_issues)
            continue
        kind = element["kind"]
        renderer = RENDERERS.get(kind)
        if renderer is None:
            issues.append(
                ContractIssue(
                    "UNSUPPORTED_KIND",
                    f"elements.{element_id}.kind",
                    f"no renderer is registered for {kind}",
                )
            )
            continue
        try:
            renderer_issues = renderer.validate_contract(element, context)
            if not isinstance(renderer_issues, list) or any(
                not isinstance(issue, ContractIssue) for issue in renderer_issues
            ):
                raise TypeError("renderer returned an invalid contract result")
        except ToolError as exc:
            issues.append(
                ContractIssue(exc.code, exc.path, exc.detail, exc.capability)
            )
        except (KeyError, OSError, OverflowError, RuntimeError, TypeError, ValueError):
            issues.append(
                ContractIssue(
                    "BUILD_OUTPUT_INCOMPLETE",
                    f"elements.{element_id}",
                    "renderer contract validation failed",
                )
            )
        else:
            issues.extend(renderer_issues)
    return issues
