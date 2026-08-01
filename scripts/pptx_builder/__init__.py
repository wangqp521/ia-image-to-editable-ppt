"""Schema v2 PPTX compiler building blocks and static renderers."""

from .common import RENDERERS, RenderContext
from .contracts import validate_renderer_contracts
from .lines import LINE_RENDERER
from .multipart import MATRIX_RENDERER, STATUS_RENDERER
from .pictures import ICON_RENDERER, PICTURE_RENDERER
from .registry import ObjectRegistry
from .shapes import SHAPE_RENDERER
from .text import TEXT_RENDERER
from .tables import TABLE_RENDERER

__all__ = [
    "ICON_RENDERER",
    "LINE_RENDERER",
    "MATRIX_RENDERER",
    "ObjectRegistry",
    "PICTURE_RENDERER",
    "RENDERERS",
    "RenderContext",
    "SHAPE_RENDERER",
    "STATUS_RENDERER",
    "TABLE_RENDERER",
    "TEXT_RENDERER",
    "validate_renderer_contracts",
]
