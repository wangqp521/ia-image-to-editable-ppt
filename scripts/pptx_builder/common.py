"""Static renderer contracts and explicit registration."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from lib.capabilities import (
    ATOMIC_CAPABILITY_METADATA,
    BUILDABLE_KINDS,
    CANONICAL_VALUES,
    CAPABILITY_METADATA,
)
from lib.element_contracts import KIND_CONTENT_FIELDS, KIND_STYLE_FIELDS
from lib.error_codes import ContractIssue

if TYPE_CHECKING:
    from .registry import ObjectRegistry


@dataclass(frozen=True)
class RenderContext:
    """Inputs shared by a renderer during one slide build."""

    slide: Any
    spec: dict[str, Any]
    representation_modes: Mapping[str, str]
    typography: Mapping[str, dict[str, Any]]
    registry: ObjectRegistry

    def __post_init__(self) -> None:
        object.__setattr__(self, "typography", MappingProxyType(dict(self.typography)))


class Renderer(Protocol):
    """The deliberately small, statically registered renderer surface."""

    kind: str
    supported_fields: frozenset[str]
    supported_values: Mapping[str, frozenset[str]]
    required_fields: frozenset[str]
    capability_ids: frozenset[str]

    def validate_contract(
        self, element: dict[str, Any], context: RenderContext
    ) -> list[ContractIssue]: ...

    def render(self, element: dict[str, Any], context: RenderContext) -> None: ...


RENDERERS: dict[str, Renderer] = {}


def _known_capability_ids() -> frozenset[str]:
    enumerated = frozenset(
        f"{CAPABILITY_METADATA[group][0]}.{value}"
        for group, values in CANONICAL_VALUES.items()
        for value in values
    )
    return enumerated | frozenset(ATOMIC_CAPABILITY_METADATA)


def _enumerated_capability_ids() -> frozenset[str]:
    return frozenset(
        f"{CAPABILITY_METADATA[group][0]}.{value}"
        for group, values in CANONICAL_VALUES.items()
        for value in values
    )


def _validate_renderer_metadata(kind: str, renderer: Renderer) -> None:
    if not isinstance(kind, str) or kind not in BUILDABLE_KINDS:
        raise ValueError(f"unsupported renderer kind: {kind}")
    if renderer.kind != kind:
        raise ValueError(f"renderer kind mismatch: {renderer.kind}")
    if not isinstance(renderer.supported_fields, frozenset):
        raise ValueError(f"renderer supported_fields must be a frozenset: {kind}")
    if not isinstance(renderer.supported_values, Mapping):
        raise ValueError(f"renderer supported_values must be a mapping: {kind}")
    if not isinstance(renderer.required_fields, frozenset):
        raise ValueError(f"renderer required_fields must be a frozenset: {kind}")
    if not isinstance(renderer.capability_ids, frozenset):
        raise ValueError(f"renderer capability_ids must be a frozenset: {kind}")
    allowed_fields = KIND_STYLE_FIELDS[kind] | KIND_CONTENT_FIELDS[kind]
    if not renderer.supported_fields <= allowed_fields:
        raise ValueError(f"renderer fields outside capability registry: {kind}")
    if not renderer.required_fields <= renderer.supported_fields:
        raise ValueError(f"renderer required fields are not supported: {kind}")
    declared_capabilities: set[str] = set()
    for group, values in renderer.supported_values.items():
        allowed_values = CANONICAL_VALUES.get(group)
        metadata = CAPABILITY_METADATA.get(group)
        if (
            not isinstance(group, str)
            or not isinstance(values, frozenset)
            or allowed_values is None
            or metadata is None
            or not values <= allowed_values
        ):
            raise ValueError(f"renderer values outside capability registry: {kind}")
        if metadata[1] not in renderer.supported_fields:
            raise ValueError(f"renderer values lack declared field: {kind}")
        declared_capabilities.update(
            f"{metadata[0]}.{value}" for value in values
        )
    if renderer.capability_ids & _enumerated_capability_ids() != frozenset(
        declared_capabilities
    ):
        raise ValueError(f"renderer capabilities do not match declared values: {kind}")
    if not renderer.capability_ids <= _known_capability_ids():
        raise ValueError(f"renderer capabilities outside capability registry: {kind}")
    for capability_id in renderer.capability_ids & frozenset(
        ATOMIC_CAPABILITY_METADATA
    ):
        if ATOMIC_CAPABILITY_METADATA[capability_id] not in renderer.supported_fields:
            raise ValueError(f"renderer atomic capability lacks declared field: {kind}")


def register_renderer(kind: str, renderer: Renderer) -> None:
    """Register one module-level renderer without discovery or replacement."""
    _validate_renderer_metadata(kind, renderer)
    if kind in RENDERERS:
        raise RuntimeError(f"duplicate renderer: {kind}")
    RENDERERS[kind] = renderer
