"""Bridge from IFC analysis to BIMcollab Zoom, on top of the cli-anything-bczoom core."""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from cli_anything.bczoom.core import library as lib
from cli_anything.bczoom.core import nightshift as ns
from cli_anything.bczoom.core import project as prj
from cli_anything.bczoom.core import rules
from cli_anything.bczoom.core import zoomxml as zx
from cli_anything.bczoom.utils import zoom_backend as backend

# Display names used by Zoom for IFC classes (extracted from BIMcollab.Zoom.Engine.dll).
_SPECIAL = {"IfcBuildingStorey": "Building Story", "IfcOpeningElement": "Opening",
            "IfcOpeningStandardCase": "Opening", "IfcElement": "Any", "IfcProduct": "Any"}


class ZoomBridgeError(ValueError):
    pass


def zoom_type(ifc_class: str) -> str:
    """IfcDuctSegment -> 'Duct Segment', IfcWallStandardCase -> 'Wall', 'Any' stays."""
    if not ifc_class or ifc_class.lower() == "any":
        return "Any"
    if not ifc_class.startswith("Ifc"):
        return ifc_class                    # already a Zoom name
    if ifc_class in _SPECIAL:
        return _SPECIAL[ifc_class]
    name = re.sub(r"(StandardCase|ElementedCase)$", "", ifc_class[3:]) or ifc_class[3:]
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def _author() -> str:
    import os
    return os.environ.get("BCZOOM_AUTHOR", "")


def _load_or_new(path: str, kind_name: str):
    kind = lib.KINDS[kind_name]
    p = Path(path).expanduser()
    if p.suffix.lower() != kind.ext:
        raise ZoomBridgeError(f"A {kind_name} library must use {kind.ext}")
    if p.exists():
        doc, k = lib.load(str(p))
        if k.name != kind_name:
            raise ZoomBridgeError(f"{p.name} is a {k.name} library, not {kind_name}")
        return doc, kind, p
    return lib.new_library(kind), kind, p


def _set(doc, kind, set_name):
    try:
        return lib.find_set(doc, kind, set_name)
    except lib.LibraryError:
        return lib.add_set(doc, kind, set_name)


def _rule(spec: dict):
    """spec keys: ifc_class|ifc_type, prop, pset, condition, value, action, color, value_type, unit."""
    return rules.build_rule(
        ifc_type=zoom_type(spec.get("ifc_class") or spec.get("ifc_type") or "Any"),
        prop=spec.get("prop"), pset=spec.get("pset"), prop_type=spec.get("prop_type"),
        value_type=spec.get("value_type"), unit=spec.get("unit", "None"),
        condition=spec.get("condition"), value=None if spec.get("value") is None else str(spec["value"]),
        action=spec.get("action", "Add"), color=spec.get("color"))


def _save(doc, path: Path) -> dict:
    issues = [i for i in lib.validate(doc, lib.detect_kind(doc)) if i["level"] == "error"]
    if issues:
        raise ZoomBridgeError(f"Refusing to write invalid library: {issues[:3]}")
    n = zx.write_file(doc, path)
    return {"library": str(path.resolve()), "bytes": n}


# ---------------------------------------------------------------------------
# Smart views / clash rules
# ---------------------------------------------------------------------------

def smartview_create(library: str, set_name: str, title: str, rule_specs: list[dict],
                     description: str = "", replace: bool = False) -> dict:
    if not rule_specs:
        raise ZoomBridgeError("At least one rule is required")
    doc, kind, p = _load_or_new(library, "smartviews")
    s = _set(doc, kind, set_name)
    existing = [i for i in lib.items(s, kind) if lib.title(i, kind) == title]
    if existing and not replace:
        raise ZoomBridgeError(f"Smart view '{title}' already exists in '{set_name}' (use replace)")
    for e in existing:
        s.find(kind.container_tag).remove(e)
    built = [_rule(r) for r in rule_specs]
    sv = rules.build_smartview(title, description, _author(), rules=built)
    lib.append_item(s, kind, sv)
    return {**_save(doc, p), "set": set_name, "smartview": title,
            "rules": [lib.rule_to_dict(r) for r in built]}


PALETTE = ["#E6194B", "#3CB44B", "#4363D8", "#F58231", "#911EB4", "#42D4F4", "#F032E6",
           "#BFEF45", "#FABED4", "#469990", "#DCBEFF", "#9A6324", "#FFFAC8", "#800000",
           "#AAFFC3", "#808000", "#FFD8B1", "#000075", "#A9A9A9"]


def smartview_color_by_values(library: str, set_name: str, title: str, ifc_class: str,
                              pset: str, prop: str, values: list[str], colors: list[str] | None = None,
                              highlight_missing: bool = True, replace: bool = True) -> dict:
    """One colour per value (+ grey for 'not defined'), Zoom-ready."""
    vals = [v for v in values if v not in ("(vide)", "", None)]
    if len(vals) > len(PALETTE) and not colors:
        raise ZoomBridgeError(f"{len(vals)} values: too many for one colour each (max {len(PALETTE)}); "
                              "filter or pass colors")
    colors = colors or PALETTE
    specs = [{"ifc_class": ifc_class, "action": "Add"}]
    specs += [{"ifc_class": ifc_class, "pset": pset, "prop": prop, "condition": "Is", "value": v,
               "action": "SetColored", "color": c} for v, c in zip(vals, colors)]
    if highlight_missing:
        specs.append({"ifc_class": ifc_class, "pset": pset, "prop": prop,
                      "condition": "StringIsNotDefined", "action": "SetColored", "color": "#808080"})
    res = smartview_create(library, set_name, title, specs,
                           f"{pset}.{prop} par valeur (généré par bim-assistant)", replace)
    res["legend"] = dict(zip(vals, colors[:len(vals)]))
    if highlight_missing:
        res["legend"]["(non défini)"] = "#808080"
    return res


def smartproperty_create(library: str, set_name: str, name: str, sources: list[dict],
                         description: str = "", replace: bool = False) -> dict:
    """Smart property `set_name`.`name` = value of the first defined source property."""
    doc, kind, p = _load_or_new(library, "smartproperties")
    s = _set(doc, kind, set_name)
    existing = [i for i in lib.items(s, kind) if lib.title(i, kind) == name]
    if existing and not replace:
        raise ZoomBridgeError(f"Smart property '{name}' already exists in '{set_name}' (use replace)")
    for e in existing:
        s.find(kind.container_tag).remove(e)
    pq = rules.build_property_query(name, sources, description, _author())
    lib.append_item(s, kind, pq)
    return {**_save(doc, p), "set": set_name, "smart_property": name,
            "sources": [f"{x['pset']}.{x['prop']}" for x in sources]}


def list_create(library: str, set_name: str, title: str, filter_rules: list[dict],
                properties: list[dict], rows: list[str], values: list[dict],
                columns: list[str] | None = None, description: str = "",
                replace: bool = False) -> dict:
    """Zoom List (pivot): filter_rules like smartview rules; properties [{pset, prop}];
    rows/columns = property names; values [{prop, aggregate}]."""
    if not filter_rules:
        raise ZoomBridgeError("At least one filter rule is required")
    doc, kind, p = _load_or_new(library, "lists")
    s = _set(doc, kind, set_name)
    existing = [i for i in lib.items(s, kind) if lib.title(i, kind) == title]
    if existing and not replace:
        raise ZoomBridgeError(f"List '{title}' already exists in '{set_name}' (use replace)")
    for e in existing:
        s.find(kind.container_tag).remove(e)
    built = [_rule(r) for r in filter_rules]
    tb = rules.build_list(title, built, [(x["pset"], x["prop"]) for x in properties], rows,
                          [(v["prop"], v.get("aggregate", "Sum")) for v in values], columns,
                          description, _author())
    lib.append_item(s, kind, tb)
    return {**_save(doc, p), "set": set_name, "list": title,
            "filter": [lib.rule_to_dict(r) for r in built]}


def clash_rule_create(library: str, set_name: str, title: str, source: list[dict],
                      target: list[dict], min_distance: float = 0.0, same_model: bool = True,
                      check_distance: bool = False, replace: bool = False) -> dict:
    if not source or not target:
        raise ZoomBridgeError("Source and target need at least one rule each")
    doc, kind, p = _load_or_new(library, "clashrules")
    s = _set(doc, kind, set_name)
    existing = [i for i in lib.items(s, kind) if lib.title(i, kind) == title]
    if existing and not replace:
        raise ZoomBridgeError(f"Clash rule '{title}' already exists (use replace)")
    for e in existing:
        s.find(kind.container_tag).remove(e)
    cr = rules.build_clashrule(title, "", _author(), source_rules=[_rule(r) for r in source],
                               target_rules=[_rule(r) for r in target], min_distance=min_distance,
                               same_model=same_model, check_distance=check_distance)
    lib.append_item(s, kind, cr)
    return {**_save(doc, p), "set": set_name, "clash_rule": title,
            "detail": lib.item_detail(cr, kind)}


def library_summary(path: str) -> dict:
    doc, kind = lib.load(str(Path(path).expanduser()))
    return lib.summarize(doc, kind)


# ---------------------------------------------------------------------------
# Live Zoom libraries ("send to Zoom")
# ---------------------------------------------------------------------------

def install_library(path: str, sets: list[str] | None = None) -> dict:
    """Merge sets from a library file into Zoom's live library (Zoom must be closed).

    A timestamped backup of the live file is made first. Sets with the same title are replaced.
    """
    if backend.running_instances():
        raise ZoomBridgeError("BIMcollab Zoom is open: it rewrites its libraries on exit and would "
                              "overwrite the change. Close Zoom, or import the file from Zoom's panel.")
    src_doc, kind = lib.load(str(Path(path).expanduser()))
    if sets:
        src_doc = lib.extract_sets(src_doc, kind, sets)
    live = lib.zoom_data_dir() / kind.user_file
    report = []
    if live.exists():
        backup_dir = lib.zoom_data_dir() / "bim-assistant-backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"{live.stem}.{datetime.now():%Y%m%d-%H%M%S}{live.suffix}"
        shutil.copy2(live, backup)
        live_doc, _ = lib.load(str(live))
        merged, report = lib.merge([live_doc, src_doc], kind, "replace")
    else:
        backup = None
        merged, report = lib.merge([src_doc], kind, "replace")
    zx.write_file(merged, live)
    return {"live_library": str(live), "backup": str(backup) if backup else None,
            "sets": [r for r in report if r["set"] in {lib.title(s, kind) for s in lib.sets(src_doc, kind)}]}


# ---------------------------------------------------------------------------
# Projects / GUI / Nightshift
# ---------------------------------------------------------------------------

def project_create(output: str, models: list[str], open_in_zoom: bool = False) -> dict:
    out = Path(output).expanduser()
    if out.suffix.lower() != ".bcp":
        raise ZoomBridgeError("Output must be a .bcp file")
    doc = prj.new_project()
    for m in models:
        prj.add_model(doc, m)
    zx.write_file(doc, out)
    res = {"project": str(out.resolve()), "models": [Path(m).name for m in models]}
    if open_in_zoom:
        res["launched"] = backend.launch([str(out)])
    return res


def open_in_zoom(files: list[str], new_instance: bool = False) -> dict:
    running = backend.running_instances()
    if running and not new_instance:
        raise ZoomBridgeError(
            f"BIMcollab Zoom is already open (PID {running}); launching it again opens a second, "
            "empty instance. Use the zoom_live_* tools on the open session instead, or pass "
            "new_instance=true if the user really wants another window.")
    return backend.launch(files)


def status() -> dict:
    info = backend.info()
    info["libraries"] = lib.user_library_paths()
    return info


def nightshift_run(models: list[str], space: str | None = None, project_name: str | None = None,
                   update_issues: bool = False, run_lists: bool = False, sync: bool = False,
                   comment: str = "", confirm_cloud_write: bool = False, timeout: int = 1800) -> dict:
    """Run Zoom unattended: open models, optionally connect / update issues / lists / sync."""
    script = ns.new_script("bim-assistant")
    if models:
        ns.append(script, ns.action_open_models(models))
    if space or project_name:
        ns.append(script, ns.action_connect(space, project_name))
    if update_issues:
        ns.append(script, ns.action_update_issues(comment=comment))
    if run_lists:
        ns.append(script, ns.action_run_lists())
    if sync:
        ns.append(script, ns.action_sync())
    if not script["actions"]:
        raise ZoomBridgeError("Nothing to do")
    if ns.describe(script)["writes_to_cloud"] and not confirm_cloud_write:
        raise ZoomBridgeError("This run writes to the BIMcollab cloud project; ask the user, then "
                              "call again with confirm_cloud_write=true.")
    errors = [i for i in ns.validate(script) if i["level"] == "error"]
    if errors:
        raise ZoomBridgeError(f"Invalid script: {errors}")
    import os
    stage = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "bczoom" / "scripts"
    stage.mkdir(parents=True, exist_ok=True)
    path = stage / f"bim-assistant-{datetime.now():%Y%m%d-%H%M%S}.json"
    ns.save(script, path)
    res = backend.run_nightshift(str(path), timeout=timeout)
    res["script"] = str(path)
    res["actions"] = [a["type"] for a in script["actions"]]
    return res
