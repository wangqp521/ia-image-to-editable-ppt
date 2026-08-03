"""Native simple 2D single-series pie and doughnut renderer."""

from __future__ import annotations

from typing import Any

from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Emu, Pt

from lib.capabilities import ATOMIC_CAPABILITY_METADATA, CANONICAL_VALUES
from lib.element_contracts import validate_chart_contract
from lib.error_codes import ContractIssue

from .common import RenderContext, register_renderer


_CHART_TYPES = {
    "pie": XL_CHART_TYPE.PIE,
    "doughnut": XL_CHART_TYPE.DOUGHNUT,
}
_LABEL_POSITIONS = {
    "best_fit": XL_LABEL_POSITION.BEST_FIT,
    "center": XL_LABEL_POSITION.CENTER,
    "inside_end": XL_LABEL_POSITION.INSIDE_END,
    "outside_end": XL_LABEL_POSITION.OUTSIDE_END,
}
_CHART_FIELDS = frozenset(
    {"chart_type", "slices", "data_labels", "first_slice_angle", "hole_size"}
)


def _set_chart_integer(plot: Any, tag: str, value: int) -> None:
    node = plot._element.find(qn(tag))
    if node is None:
        node = OxmlElement(tag)
        plot._element.append(node)
    node.set("val", str(value))


class ChartRenderer:
    kind = "chart"
    supported_fields = _CHART_FIELDS
    supported_values = {"chart_type": CANONICAL_VALUES["chart_type"]}
    required_fields = frozenset(
        {"chart_type", "slices", "data_labels", "first_slice_angle"}
    )
    capability_ids = frozenset(
        f"chart.{value}" for value in CANONICAL_VALUES["chart_type"]
    ) | frozenset(
        capability
        for capability, field in ATOMIC_CAPABILITY_METADATA.items()
        if capability.startswith("chart.") and field in _CHART_FIELDS
    )

    def validate_contract(
        self, element: dict[str, Any], context: RenderContext
    ) -> list[ContractIssue]:
        return validate_chart_contract(element)

    def render(self, element: dict[str, Any], context: RenderContext) -> None:
        content = element["content"]
        style = element["style"]
        slices = content["slices"]
        chart_data = CategoryChartData()
        chart_data.categories = [
            "" if item["category"] is None else item["category"] for item in slices
        ]
        chart_data.add_series("", [item["value"] for item in slices])

        x, y, width, height = element["slide_bbox"]
        frame = context.slide.shapes.add_chart(
            _CHART_TYPES[content["chart_type"]],
            Emu(x),
            Emu(y),
            Emu(width),
            Emu(height),
            chart_data,
        )
        chart = frame.chart
        chart.has_title = False
        chart.has_legend = False
        plot = chart.plots[0]
        plot.vary_by_categories = True
        _set_chart_integer(plot, "c:firstSliceAng", style["first_slice_angle"])
        if content["chart_type"] == "doughnut":
            _set_chart_integer(plot, "c:holeSize", style["hole_size"])

        for point, item in zip(plot.series[0].points, slices, strict=True):
            point.format.fill.solid()
            point.format.fill.fore_color.rgb = RGBColor.from_string(item["color"][1:])
            point.format.line.fill.background()

        labels = content["data_labels"]
        plot.has_data_labels = labels["enabled"]
        if labels["enabled"]:
            data_labels = plot.data_labels
            data_labels.show_category_name = labels["show_category"]
            data_labels.show_value = labels["show_value"]
            data_labels.show_percentage = labels["show_percentage"]
            data_labels.position = _LABEL_POSITIONS[labels["position"]]
            data_labels.number_format = labels["number_format"]
            data_labels.font.size = Pt(labels["font_size"])
            data_labels.font.bold = labels["font_weight"] >= 600
            data_labels.font.color.rgb = RGBColor.from_string(labels["color"][1:])

        context.registry.register(
            element["element_id"],
            frame,
            "graphicFrame",
            semantic_kind="chart",
            selected_mode=context.representation_modes[element["element_id"]],
        )


CHART_RENDERER = ChartRenderer()
register_renderer("chart", CHART_RENDERER)
