from __future__ import annotations

from typing import Any

from pptx.oxml.xmlchemy import OxmlElement
from pptx.oxml.ns import qn

from lib.error_codes import ToolError


BASELINES = {"normal": None, "superscript": 30000, "subscript": -25000}
ARROW_TYPES = {"none", "triangle", "stealth", "diamond", "oval", "arrow"}


def set_run_character_properties(run: Any, contract: dict[str, Any], path: str) -> None:
    r_pr = run._r.get_or_add_rPr()
    r_pr.set("i", "1" if contract.get("italic", False) else "0")
    strike = contract.get("strike", False)
    if not isinstance(strike, bool):
        raise ToolError("SPEC_INVALID", f"{path}.strike", "expected boolean")
    r_pr.set("strike", "sngStrike" if strike else "noStrike")
    baseline = contract.get("baseline", "normal")
    if baseline not in BASELINES:
        raise ToolError("UNSUPPORTED_FEATURE", f"{path}.baseline", str(baseline))
    if BASELINES[baseline] is None:
        r_pr.attrib.pop("baseline", None)
    else:
        r_pr.set("baseline", str(BASELINES[baseline]))
    spacing = contract.get("letter_spacing", 0)
    if not isinstance(spacing, int) or isinstance(spacing, bool) or not -4000 <= spacing <= 4000:
        raise ToolError("SPEC_INVALID", f"{path}.letter_spacing", "expected integer -4000..4000")
    r_pr.set("spc", str(spacing))


def set_line_arrowheads(line: Any, contract: dict[str, Any], path: str) -> None:
    ln = line._get_or_add_ln()
    for field, tag in (("begin_arrow", "a:headEnd"), ("end_arrow", "a:tailEnd")):
        value = contract.get(field, "none")
        if value not in ARROW_TYPES:
            raise ToolError("UNSUPPORTED_FEATURE", f"{path}.{field}", str(value))
        existing = ln.find(qn(tag))
        if existing is not None:
            ln.remove(existing)
        if value != "none":
            arrow = OxmlElement(tag)
            arrow.set("type", value)
            ln.append(arrow)


def set_table_cell_border(tc_pr: Any, side: str, contract: Any, path: str) -> None:
    tags = {"left": "a:lnL", "right": "a:lnR", "top": "a:lnT", "bottom": "a:lnB"}
    if side not in tags:
        raise ToolError("SPEC_INVALID", path, "unknown border side")
    tag = tags[side]
    existing = tc_pr.find(qn(tag))
    if existing is not None:
        tc_pr.remove(existing)
    line = OxmlElement(tag)
    if contract is None:
        line.append(OxmlElement("a:noFill"))
        tc_pr.append(line)
        return
    if not isinstance(contract, dict):
        raise ToolError("SPEC_INVALID", path, "border must be object or null")
    allowed = {"color", "width_emu", "dash"}
    unknown = set(contract) - allowed
    if unknown:
        raise ToolError("UNSUPPORTED_FEATURE", path, f"unsupported border fields: {', '.join(sorted(unknown))}")
    color = contract.get("color")
    if not isinstance(color, str) or len(color) != 7 or not color.startswith("#"):
        raise ToolError("SPEC_INVALID", f"{path}.color", "expected #RRGGBB")
    line.set("w", str(int(contract.get("width_emu", 12700))))
    solid = OxmlElement("a:solidFill")
    srgb = OxmlElement("a:srgbClr")
    srgb.set("val", color[1:])
    solid.append(srgb)
    line.append(solid)
    dash = contract.get("dash", "solid")
    dash_values = {"solid": "solid", "dash": "dash", "dot": "dot", "dash_dot": "dashDot"}
    if dash not in dash_values:
        raise ToolError("UNSUPPORTED_FEATURE", f"{path}.dash", str(dash))
    prst_dash = OxmlElement("a:prstDash")
    prst_dash.set("val", dash_values[dash])
    line.append(prst_dash)
    tc_pr.append(line)


def set_native_bullet(paragraph: Any, contract: dict[str, Any], path: str) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    for tag in ("a:buNone", "a:buChar", "a:buAutoNum"):
        child = p_pr.find(qn(tag))
        if child is not None:
            p_pr.remove(child)
    if contract.get("is_list") is not True:
        p_pr.insert(0, OxmlElement("a:buNone"))
        return
    bullet_type = contract.get("bullet_type", "char")
    if bullet_type == "char":
        value = contract.get("bullet")
        if not isinstance(value, str) or not value:
            raise ToolError("SPEC_INVALID", path, "bullet character required")
        bullet = OxmlElement("a:buChar")
        bullet.set("char", value)
        p_pr.append(bullet)
    elif bullet_type == "auto_number":
        value = contract.get("bullet")
        if not isinstance(value, str) or not value:
            raise ToolError("SPEC_INVALID", path, "auto-number scheme required")
        bullet = OxmlElement("a:buAutoNum")
        bullet.set("type", value)
        p_pr.append(bullet)
    else:
        raise ToolError("UNSUPPORTED_FEATURE", path, f"bullet type {bullet_type}")


def set_round_rect_adjustment(shape: Any, values: Any, path: str) -> None:
    if not isinstance(values, list) or len(values) != 1:
        raise ToolError("MISSING_REQUIRED_FIELD", path, "roundRect needs one adjustment")
    value = values[0]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 0.5:
        raise ToolError("SPEC_INVALID", path, "roundRect adjustment must be 0..0.5")
    shape.adjustments[0] = float(value)
