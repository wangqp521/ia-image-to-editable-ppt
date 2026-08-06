"""Small deterministic OOXML helpers for native chart rendering."""

from __future__ import annotations

from typing import Any, Iterable

from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Emu

from lib.geometry import quantize_drawingml_percentage


_DASH_STYLES = {
    "solid": MSO_LINE_DASH_STYLE.SOLID,
    "dash": MSO_LINE_DASH_STYLE.DASH,
    "dot": MSO_LINE_DASH_STYLE.ROUND_DOT,
    "dashDot": MSO_LINE_DASH_STYLE.DASH_DOT,
}
_AXIS_POSITIONS = {"top": "t", "bottom": "b", "left": "l", "right": "r"}


def set_chart_value(owner: Any, tag: str, value: Any) -> Any:
    """Set one c:* value child on a chart XML owner."""
    node = owner.find(qn(tag))
    if node is None:
        node = OxmlElement(tag)
        owner.append(node)
    node.set("val", str(value))
    return node


def set_axis_position(axis: Any, position: str) -> None:
    """Write exact semantic axis position to c:axPos."""
    set_chart_value(axis._element, "c:axPos", _AXIS_POSITIONS[position])


def set_axis_reverse_order(axis: Any, reverse_order: bool) -> None:
    """Write explicit minMax/maxMin orientation for cross-renderer stability."""
    axis.reverse_order = reverse_order
    set_chart_value(
        axis._element.scaling,
        "c:orientation",
        "maxMin" if reverse_order else "minMax",
    )


def set_display_blanks_as(chart: Any, value: str) -> None:
    """Write the chart-level missing-value display contract."""
    set_chart_value(chart._chartSpace.chart, "c:dispBlanksAs", value)


def _remove_children(owner: Any, tags: Iterable[str]) -> None:
    qualified = {qn(tag) for tag in tags}
    for child in list(owner):
        if child.tag in qualified:
            owner.remove(child)


def _append_rgb(parent: Any, color: str, opacity: float = 1) -> None:
    rgb = OxmlElement("a:srgbClr")
    rgb.set("val", color.removeprefix("#").upper())
    if opacity != 1:
        alpha = OxmlElement("a:alpha")
        alpha.set("val", str(quantize_drawingml_percentage(opacity)))
        rgb.append(alpha)
    parent.append(rgb)


def _append_fill(owner: Any, contract: str | dict[str, Any]) -> None:
    if contract == "noFill":
        owner.append(OxmlElement("a:noFill"))
        return
    fill = OxmlElement("a:solidFill")
    _append_rgb(fill, contract["color"], contract["opacity"])
    owner.append(fill)


def _append_line(owner: Any, contract: str | dict[str, Any]) -> None:
    line = OxmlElement("a:ln")
    if contract == "noFill":
        line.append(OxmlElement("a:noFill"))
        owner.append(line)
        return
    line.set("w", str(contract["width"]))
    fill = OxmlElement("a:solidFill")
    _append_rgb(fill, contract["color"], contract["opacity"])
    line.append(fill)
    dash = OxmlElement("a:prstDash")
    dash.set(
        "val",
        {
            "solid": "solid",
            "dash": "dash",
            "dot": "dot",
            "dashDot": "dashDot",
        }[contract["dash"]],
    )
    line.append(dash)
    owner.append(line)


def set_chart_area_style(
    owner: Any,
    contract: dict[str, Any],
    *,
    insert_before: tuple[str, ...] = (),
) -> None:
    """Replace c:spPr for chartSpace or plotArea with an exact area style."""
    current = owner.find(qn("c:spPr"))
    if current is not None:
        owner.remove(current)
    properties = OxmlElement("c:spPr")
    _append_fill(properties, contract["fill"])
    _append_line(properties, contract["line"])
    before = {qn(tag) for tag in insert_before}
    for index, child in enumerate(owner):
        if child.tag in before:
            owner.insert(index, properties)
            return
    owner.append(properties)


def set_chart_format_line(format_object: Any, contract: str | dict[str, Any]) -> None:
    """Apply exact noFill or stroke to a python-pptx ChartFormat."""
    if contract == "noFill":
        format_object.line.fill.background()
    else:
        line = format_object.line
        line.color.rgb = RGBColor.from_string(contract["color"].removeprefix("#"))
        line.width = Emu(contract["width"])
        line.dash_style = _DASH_STYLES[contract["dash"]]
    properties = format_object._element.get_or_add_spPr()
    _remove_children(properties, {"a:ln"})
    _append_line(properties, contract)


def set_series_smooth(series: Any, value: bool) -> None:
    """Write an explicit c:smooth on one line series."""
    set_chart_value(series._element, "c:smooth", 1 if value else 0)
