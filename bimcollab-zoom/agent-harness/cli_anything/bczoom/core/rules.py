"""Build Smart View rules, Smart Views and Clash Rules in Zoom's native schema.

Enumerations below were extracted from BIMcollab.Zoom.Core.dll (Zoom 9.5) and
cross-checked against real libraries.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from cli_anything.bczoom.core import zoomxml as zx
from cli_anything.bczoom.core.library import LibraryError, new_guid, now_iso

CONDITIONS = [
    "Is", "IsNot", "Contains", "DoesNotContain", "StartsWith", "EndsWith", "Or", "Nor",
    "StringIsDefined", "StringIsNotDefined",
    "Equals", "NotEquals", "Less", "Greater", "LessEqual", "GreaterEqual",
    "NumericIsDefined", "NumericIsNotDefined",
    "BooleanTrue", "BooleanFalse", "BooleanIsDefined", "BooleanIsNotDefined",
    "LogicalTrue", "LogicalFalse", "LogicalUnknown", "LogicalIsDefined", "LogicalIsNotDefined",
]
# Conditions that take no user value (Zoom still writes an empty <VALUE/>).
VALUELESS_CONDITIONS = {
    "StringIsDefined", "StringIsNotDefined", "BooleanTrue", "BooleanFalse",
    "BooleanIsDefined", "BooleanIsNotDefined", "LogicalTrue", "LogicalFalse",
    "LogicalUnknown", "LogicalIsDefined", "LogicalIsNotDefined",
}
# Zoom writes these with <STATE> instead of <VALUE>.
STATE_CONDITIONS = {"NumericIsDefined", "NumericIsNotDefined"}

ACTIONS = ["Add", "Remove", "RemoveOthers", "AddSetTransparent", "AddSetColored",
           "SetTransparent", "SetOpaque", "SetColored", "SetAutoColored"]
COLOR_ACTIONS = {"AddSetColored", "SetColored", "SetAutoColored"}
EXPLODE_MODES = ["KeepParentsAndChildren", "KeepOnlyParents", "KeepOnlyChildren"]
PROPERTY_TYPES = ["None", "Summary", "PropertySet", "Location"]
VALUE_TYPES = ["None", "StringValue", "DoubleValue", "BoolValue"]
UNITS = ["None", "Ampere", "Becquerel", "Candela", "Coulomb", "CubicMetre", "DegreeCelsius",
         "Farad", "Gram", "Gray", "Henry", "Hertz", "Joule", "Kelvin", "Lumen", "Lux", "Metre",
         "Mole", "Newton", "Ohm", "Pascal", "Radian", "Second", "Siemens", "Sievert",
         "SquareMetre", "Steradian", "Tesla", "Volt", "Watt", "Weber"]

NAMED_COLORS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "orange": (255, 128, 0), "purple": (128, 0, 255), "cyan": (0, 255, 255),
    "magenta": (255, 0, 255), "grey": (128, 128, 128), "gray": (128, 128, 128),
    "white": (255, 255, 255), "black": (0, 0, 0),
}


def _check(value: str, allowed: list[str], what: str) -> str:
    for a in allowed:
        if a.lower() == str(value).lower():
            return a
    raise LibraryError(f"Invalid {what} '{value}'. Allowed: {', '.join(allowed)}")


def parse_color(color: str | None) -> tuple[int, int, int, int]:
    """Accept 'red', '#FF8800', '#FF8800CC', '255,128,0' or '255,128,0,255'."""
    if not color:
        return (255, 0, 0, 255)
    c = color.strip().lower()
    if c in NAMED_COLORS:
        return (*NAMED_COLORS[c], 255)
    if c.startswith("#") and len(c) in (7, 9):
        try:
            vals = [int(c[i:i + 2], 16) for i in range(1, len(c), 2)]
        except ValueError:
            raise LibraryError(f"Invalid hex color '{color}'")
        return tuple(vals + [255] * (4 - len(vals)))  # type: ignore[return-value]
    parts = [p.strip() for p in c.split(",")]
    if len(parts) in (3, 4) and all(p.isdigit() for p in parts):
        vals = [int(p) for p in parts]
        if all(0 <= v <= 255 for v in vals):
            return tuple(vals + [255] * (4 - len(vals)))  # type: ignore[return-value]
    raise LibraryError(f"Invalid color '{color}'. Use a name ({', '.join(NAMED_COLORS)}), "
                       "#RRGGBB[AA] or R,G,B[,A]")


def infer_value_type(condition: str) -> str:
    if condition.startswith("Boolean"):
        return "BoolValue"
    if condition in ("Equals", "NotEquals", "Less", "Greater", "LessEqual", "GreaterEqual",
                     "NumericIsDefined", "NumericIsNotDefined"):
        return "DoubleValue"
    return "StringValue"


def build_rule(ifc_type: str = "Any", prop: str | None = None, pset: str | None = None,
               prop_type: str | None = None, value_type: str | None = None, unit: str = "None",
               condition: str | None = None, value: str | None = None,
               action: str = "Add", color: str | None = None) -> ET.Element:
    """Build a <RULE>. Without --prop it is a pure type filter (Zoom 'None' property)."""
    action = _check(action, ACTIONS, "action")
    unit = _check(unit, UNITS, "unit")
    rule = ET.Element("RULE")
    zx.sub(rule, "IFCTYPE", ifc_type or "Any")
    p = zx.sub(rule, "PROPERTY")
    cond = ET.Element("CONDITION")
    if not prop:
        for tag in ("NAME", "PROPERTYSETNAME", "TYPE", "VALUETYPE", "UNIT"):
            zx.sub(p, tag, "None")
        zx.sub(cond, "TYPE", "Equals")
        zx.sub(cond, "STATE", "Empty")
    else:
        condition = _check(condition or "StringIsDefined", CONDITIONS, "condition")
        pset = pset or "Summary"
        if prop_type is None:
            prop_type = pset if pset in ("Summary", "Location") else "PropertySet"
        prop_type = _check(prop_type, PROPERTY_TYPES, "property type")
        value_type = _check(value_type or infer_value_type(condition), VALUE_TYPES, "value type")
        zx.sub(p, "NAME", prop)
        zx.sub(p, "PROPERTYSETNAME", pset)
        zx.sub(p, "TYPE", prop_type)
        zx.sub(p, "VALUETYPE", value_type)
        zx.sub(p, "UNIT", unit)
        zx.sub(cond, "TYPE", condition)
        if condition in STATE_CONDITIONS:
            zx.sub(cond, "STATE", "Empty")
        elif condition in VALUELESS_CONDITIONS:
            zx.sub(cond, "VALUE", "")
        else:
            if value is None or value == "":
                raise LibraryError(f"Condition '{condition}' requires --value")
            if value_type == "DoubleValue":
                try:
                    value = f"{float(value):.11f}"
                except ValueError:
                    raise LibraryError(f"Condition '{condition}' needs a numeric value, got '{value}'")
            zx.sub(cond, "VALUE", value)
    rule.append(cond)
    a = zx.sub(rule, "ACTION")
    zx.sub(a, "TYPE", action)
    if action in COLOR_ACTIONS:
        r, g, b, alpha = parse_color(color)
        for tag, v in zip(("R", "G", "B", "A"), (r, g, b, alpha)):
            zx.sub(a, tag, str(v))
    elif color:
        raise LibraryError(f"--color only applies to actions {sorted(COLOR_ACTIONS)}")
    return rule


def _smartview_core(title: str, description: str, author: str, explode_mode: str,
                    rules: list[ET.Element]) -> ET.Element:
    explode_mode = _check(explode_mode, EXPLODE_MODES, "explode mode")
    sv = ET.Element("SMARTVIEW")
    ts = now_iso()
    zx.sub(sv, "TITLE", title)
    zx.sub(sv, "DESCRIPTION", description)
    zx.sub(sv, "CREATOR", author)
    zx.sub(sv, "CREATIONDATE", ts)
    zx.sub(sv, "MODIFIER", author)
    zx.sub(sv, "MODIFICATIONDATE", ts)
    zx.sub(sv, "GUID", new_guid())
    rs = zx.sub(sv, "RULES")
    for r in rules:
        rs.append(r)
    ito = zx.sub(sv, "INFORMATIONTAKEOFF")
    zx.sub(ito, "PROPERTYSETNAME", "None")
    zx.sub(ito, "PROPERTYNAME", "None")
    zx.sub(ito, "OPERATION", "0")
    zx.sub(sv, "EXPLODEMODE", explode_mode)
    return sv


def build_smartview(title: str, description: str = "", author: str = "",
                    explode_mode: str = "KeepParentsAndChildren",
                    rules: list[ET.Element] | None = None) -> ET.Element:
    return _smartview_core(title, description, author, explode_mode, rules or [])


def build_property_query(name: str, sources: list[dict], description: str = "",
                         author: str = "", explode_mode: str = "KeepParentsAndChildren") -> ET.Element:
    """A Smart Property (``<PROPERTYQUERY>``) whose value is copied (MapValue) from the first
    source property defined on each element — e.g. one 'Volume' fed by several Qto_* psets.

    sources: [{pset, prop, ifc_type='Any', prop_type?, value_type='DoubleValue', unit='None'}]
    """
    if not sources:
        raise LibraryError("A smart property needs at least one source property")
    rules = []
    for s in sources:
        vt = s.get("value_type", "DoubleValue")
        cond = "NumericIsDefined" if vt == "DoubleValue" else "StringIsDefined"
        r = build_rule(ifc_type=s.get("ifc_type", "Any"), prop=s["prop"], pset=s["pset"],
                       prop_type=s.get("prop_type"), value_type=vt, unit=s.get("unit", "None"),
                       condition=cond, action="Add")
        a = r.find("ACTION")
        a.find("TYPE").text = "MapValue"
        zx.sub(a, "VALUE", "")
        a.append(ET.fromstring(ET.tostring(r.find("PROPERTY"))))  # copy the source property
        rules.append(r)
    pq = ET.Element("PROPERTYQUERY")
    zx.sub(pq, "NAME", name)
    zx.sub(pq, "GUID", new_guid())
    zx.sub(pq, "MODIFICATIONDATE", "")
    pq.append(_smartview_core(name, description, author, explode_mode, rules))
    return pq


PIVOT_AGGREGATES =["Sum", "Count", "Avg", "Min", "Max", "DistinctCount"]


def _pivot_field(name: str, agg: str = "Count") -> dict:
    return {"name": name, "caption": name, "showNoDataItems": False, "showSubTotals": True,
            "type": agg, "showFilterIcon": True, "showSortIcon": True, "showRemoveIcon": True,
            "showValueTypeIcon": True, "showEditIcon": True, "allowDragAndDrop": True}


def build_list(title: str, filter_rules: list[ET.Element], properties: list[tuple[str, str]],
               rows: list[str], values: list[tuple[str, str]], columns: list[str] | None = None,
               description: str = "", author: str = "",
               explode_mode: str = "KeepParentsAndChildren") -> ET.Element:
    """A Zoom List (``<TABLE>``, pivot view).

    filter_rules: smart view rules selecting the elements (stored hex-encoded in CONFIG.filter).
    properties:   (property set, property) pairs loaded into the list, e.g. ("Summary", "Type").
    rows/columns: property names used as pivot rows/columns; values: (property, aggregate).
    """
    import json
    names = {p for _, p in properties}
    for n in list(rows) + list(columns or []) + [v for v, _ in values]:
        if n not in names:
            raise LibraryError(f"Pivot field '{n}' is not among the list properties {sorted(names)}")
    for _, agg in values:
        _check(agg, PIVOT_AGGREGATES, "pivot aggregate")
    flt = _smartview_core("Filter components", "", "", explode_mode, filter_rules)
    lines: list[str] = []
    zx._serialize_element(flt, 0, lines)
    config = {
        "version": 5, "index": 0, "grid": {}, "pivot": {}, "gridLive": {},
        "pivotLive": {"values": [_pivot_field(v, a) for v, a in values],
                      "rows": [_pivot_field(r) for r in rows],
                      "columns": [_pivot_field(c) for c in (columns or [])],
                      "filterSettings": [], "filters": [], "sortSettings": []},
        "filter": ("\n".join(lines) + "\n").encode("utf-8").hex().upper(),
        "selected_properties": [{"label": p, "propertySet": s} for s, p in properties],
    }
    tb = ET.Element("TABLE")
    ts = now_iso()
    zx.sub(tb, "TITLE", title)
    zx.sub(tb, "DESCRIPTION", description)
    zx.sub(tb, "CREATOR", author)
    zx.sub(tb, "CREATIONDATE", ts)
    zx.sub(tb, "MODIFIER", author)
    zx.sub(tb, "MODIFICATIONDATE", ts)
    zx.sub(tb, "GUID", new_guid())
    zx.sub(tb, "TABLETYPE", "PivotView")
    zx.sub(tb, "CONFIG", json.dumps(config, ensure_ascii=False, separators=(",", ":")))
    return tb


def set_takeoff(sv: ET.Element, pset: str, prop: str, operation: int) -> None:
    ito = sv.find("INFORMATIONTAKEOFF")
    ito.find("PROPERTYSETNAME").text = pset
    ito.find("PROPERTYNAME").text = prop
    ito.find("OPERATION").text = str(operation)


def build_clashrule(title: str, description: str = "", author: str = "",
                    source_rules: list[ET.Element] | None = None,
                    target_rules: list[ET.Element] | None = None,
                    min_distance: float = 0.0, check_clashes: bool = True,
                    check_duplicates: bool = True, check_distance: bool = False,
                    same_model: bool = True, same_system: bool = False,
                    subparts: bool = False,
                    penetration_tolerance: float | None = None,
                    volume_tolerance: float | None = None) -> ET.Element:
    def b(v: bool) -> str:
        return "true" if v else "false"

    def num(v: float) -> str:
        return f"{v:g}"

    cr = ET.Element("CLASHRULE")
    ts = now_iso()
    zx.sub(cr, "TITLE", title)
    zx.sub(cr, "DESCRIPTION", description)
    zx.sub(cr, "CREATOR", author)
    zx.sub(cr, "CREATIONDATE", ts)
    zx.sub(cr, "MODIFIER", author)
    zx.sub(cr, "MODIFICATIONDATE", ts)
    zx.sub(cr, "GUID", new_guid())
    for tag, rules in (("SOURCESET", source_rules), ("TARGETSET", target_rules)):
        holder = zx.sub(cr, tag)
        holder.append(_smartview_core("", "", author, "KeepParentsAndChildren", rules or []))
    zx.sub(cr, "CLASHTYPE", b(check_clashes))
    zx.sub(cr, "DUPLICATETYPE", b(check_duplicates))
    zx.sub(cr, "DISTANCETYPE", b(check_distance))
    zx.sub(cr, "MINIMUMDISTANCE", num(min_distance))
    zx.sub(cr, "PENETRATIONDEPTHTOLERANCEENABLED", b(penetration_tolerance is not None))
    zx.sub(cr, "PENETRATIONDEPTHTOLERANCE", num(penetration_tolerance or 0))
    zx.sub(cr, "PENETRATIONDEPTHTOLERANCEUSESHORTEST", "false")
    zx.sub(cr, "VOLUMETOLERANCEENABLED", b(volume_tolerance is not None))
    zx.sub(cr, "VOLUMETOLERANCE", num(volume_tolerance or 0))
    zx.sub(cr, "CLASHSAMEMODEL", b(same_model))
    zx.sub(cr, "CLASHSAMESYSTEM", b(same_system))
    zx.sub(cr, "CLASHSUBPARTS", b(subparts))
    return cr


def rules_container(item: ET.Element, kind_name: str, side: str | None = None) -> ET.Element:
    """Return the <RULES> element that rule add/remove should target."""
    if kind_name == "smartviews":
        path = "RULES"
    elif kind_name == "clashrules":
        if side not in ("source", "target"):
            raise LibraryError("Clash rules need --side source|target")
        path = f"{side.upper()}SET/SMARTVIEW/RULES"
    elif kind_name == "smartproperties":
        path = "SMARTVIEW/RULES"
    else:
        raise LibraryError(f"Library kind '{kind_name}' has no rules")
    rs = item.find(path)
    if rs is None:
        raise LibraryError(f"Item has no {path}")
    return rs


def touch_item(item: ET.Element, author: str | None = None) -> None:
    md = item.find("MODIFICATIONDATE")
    if md is not None:
        md.text = now_iso()
    if author:
        m = item.find("MODIFIER")
        if m is not None:
            m.text = author
