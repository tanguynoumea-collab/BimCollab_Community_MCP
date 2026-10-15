"""Generic handling of Zoom rule libraries.

Zoom stores four kinds of "set-based" libraries, all sharing the same shape
(header + BODY > SET* > CONTAINER > ITEM*):

=================  ======  ===================  ======================
kind               ext     set element          item element
=================  ======  ===================  ======================
smartviews         .bcsv   SMARTVIEWSET         SMARTVIEW
clashrules         .bcr    CLASHRULESET         CLASHRULE
lists              .bcl    TABLESET             TABLE
smartproperties    .bcsp   PROPERTYQUERYSET     PROPERTYQUERY
=================  ======  ===================  ======================

The user's live libraries are in ``%APPDATA%\\KUBUS\\BIMcollab Zoom``; files
exported from Zoom (Smart Views > Export) use exactly the same format, and so do
files written by this module, so they can be imported back via the Zoom GUI.
"""

from __future__ import annotations

import os
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cli_anything.bczoom.core import zoomxml as zx

APP_VERSION_STRING = "Win - Version : 9.5 (build 9.5.3.0)"


@dataclass(frozen=True)
class LibraryKind:
    name: str
    ext: str
    header_tag: str
    header_version: str
    body_tag: str
    set_tag: str
    container_tag: str
    item_tag: str
    title_tag: str          # TITLE, or NAME for smart properties
    user_file: str          # file name in the user's Zoom data folder
    app_version_string: str = APP_VERSION_STRING


KINDS: dict[str, LibraryKind] = {
    "smartviews": LibraryKind("smartviews", ".bcsv", "bimcollabsmartviewfile", "8",
                              "SMARTVIEWSETS", "SMARTVIEWSET", "SMARTVIEWS", "SMARTVIEW",
                              "TITLE", "smartviews.bcsv"),
    "clashrules": LibraryKind("clashrules", ".bcr", "bimcollabclashrulefile", "7",
                              "CLASHRULESETS", "CLASHRULESET", "CLASHRULES", "CLASHRULE",
                              "TITLE", "clashrules.bcr"),
    "lists": LibraryKind("lists", ".bcl", "bimcollabtablesfile", "5",
                         "TABLESETS", "TABLESET", "TABLES", "TABLE",
                         "TITLE", "lists.bcl"),
    "smartproperties": LibraryKind("smartproperties", ".bcsp", "bimcollabpropertyqueryfile", "8",
                                   "PROPERTYQUERYSETS", "PROPERTYQUERYSET", "PROPERTYQUERIES",
                                   "PROPERTYQUERY", "NAME", "smartproperties.bcsp",
                                   app_version_string="9.5.3.0"),
}
EXT_TO_KIND = {k.ext: k for k in KINDS.values()}


class LibraryError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def zoom_data_dir() -> Path:
    override = os.environ.get("BCZOOM_DATA_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "KUBUS" / "BIMcollab Zoom"


def user_library_paths() -> dict[str, dict]:
    base = zoom_data_dir()
    out = {}
    for kind in KINDS.values():
        p = base / kind.user_file
        out[kind.name] = {"path": str(p), "exists": p.exists(),
                          "size": p.stat().st_size if p.exists() else 0}
    return out


# ---------------------------------------------------------------------------
# Kind detection / creation
# ---------------------------------------------------------------------------

def detect_kind(doc: zx.ZoomDocument, path: str | None = None) -> LibraryKind:
    if doc.roots:
        for kind in KINDS.values():
            if doc.roots[0].tag == kind.header_tag:
                return kind
    if path:
        kind = EXT_TO_KIND.get(Path(path).suffix.lower())
        if kind:
            return kind
    tag = doc.roots[0].tag if doc.roots else "?"
    raise LibraryError(f"Not a Zoom rule library (header <{tag}>)")


def kind_for_path(path: str) -> LibraryKind:
    kind = EXT_TO_KIND.get(Path(path).suffix.lower())
    if not kind:
        raise LibraryError(
            f"Unknown library extension '{Path(path).suffix}'. "
            f"Expected one of: {', '.join(sorted(EXT_TO_KIND))}")
    return kind


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def new_guid() -> str:
    return str(uuid.uuid4())


def new_library(kind: LibraryKind) -> zx.ZoomDocument:
    header = ET.Element(kind.header_tag)
    zx.sub(header, "version", kind.header_version)
    zx.sub(header, "applicationversion", kind.app_version_string)
    body = ET.Element(kind.body_tag)
    return zx.ZoomDocument(roots=[header, body])


def load(path: str) -> tuple[zx.ZoomDocument, LibraryKind]:
    doc = zx.parse_file(path)
    return doc, detect_kind(doc, path)


# ---------------------------------------------------------------------------
# Sets and items
# ---------------------------------------------------------------------------

def sets(doc: zx.ZoomDocument, kind: LibraryKind) -> list[ET.Element]:
    return doc.body.findall(kind.set_tag)


def items(set_el: ET.Element, kind: LibraryKind) -> list[ET.Element]:
    container = set_el.find(kind.container_tag)
    return [] if container is None else container.findall(kind.item_tag)


def title(el: ET.Element, kind: LibraryKind) -> str:
    return zx.text_of(el, kind.title_tag)


def _match(el: ET.Element, kind: LibraryKind, ref: str, index: int) -> bool:
    if ref.isdigit() and int(ref) == index:
        return True
    return title(el, kind) == ref or zx.text_of(el, "GUID") == ref


def find_set(doc, kind, ref: str) -> ET.Element:
    """Find a set by exact title, GUID or 0-based index."""
    for i, s in enumerate(sets(doc, kind)):
        if _match(s, kind, ref, i):
            return s
    names = [title(s, kind) for s in sets(doc, kind)]
    raise LibraryError(f"Set '{ref}' not found. Available: {names}")


def find_item(set_el, kind, ref: str) -> ET.Element:
    for i, it in enumerate(items(set_el, kind)):
        if _match(it, kind, ref, i):
            return it
    names = [title(it, kind) for it in items(set_el, kind)]
    raise LibraryError(f"Item '{ref}' not found in set '{title(set_el, kind)}'. Available: {names}")


def add_set(doc, kind, name: str, description: str = "") -> ET.Element:
    if any(title(s, kind) == name for s in sets(doc, kind)):
        raise LibraryError(f"A set named '{name}' already exists")
    s = ET.SubElement(doc.body, kind.set_tag)
    if kind.title_tag == "NAME":
        zx.sub(s, "NAME", name)
        zx.sub(s, "GUID", new_guid())
        zx.sub(s, "DESCRIPTION", description)
    else:
        zx.sub(s, "TITLE", name)
        zx.sub(s, "DESCRIPTION", description)
        zx.sub(s, "GUID", new_guid())
    zx.sub(s, "MODIFICATIONDATE", now_iso())
    ET.SubElement(s, kind.container_tag)
    return s


def remove_set(doc, kind, ref: str) -> str:
    s = find_set(doc, kind, ref)
    name = title(s, kind)
    doc.body.remove(s)
    return name


def remove_item(doc, kind, set_ref: str, item_ref: str) -> str:
    s = find_set(doc, kind, set_ref)
    it = find_item(s, kind, item_ref)
    s.find(kind.container_tag).remove(it)
    touch(s)
    return title(it, kind)


def touch(el: ET.Element) -> None:
    md = el.find("MODIFICATIONDATE")
    if md is not None:
        md.text = now_iso()


def append_item(set_el: ET.Element, kind: LibraryKind, item: ET.Element) -> None:
    container = set_el.find(kind.container_tag)
    if container is None:
        container = ET.SubElement(set_el, kind.container_tag)
    container.append(item)
    touch(set_el)


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def summarize(doc, kind) -> dict:
    out_sets = []
    total = 0
    for i, s in enumerate(sets(doc, kind)):
        its = items(s, kind)
        total += len(its)
        out_sets.append({"index": i, "title": title(s, kind), "guid": zx.text_of(s, "GUID"),
                         "items": len(its), "modified": zx.text_of(s, "MODIFICATIONDATE")})
    return {"kind": kind.name,
            "format_version": zx.text_of(doc.header, "version"),
            "application_version": zx.text_of(doc.header, "applicationversion"),
            "set_count": len(out_sets), "item_count": total, "sets": out_sets}


def item_summary(item: ET.Element, kind: LibraryKind, index: int) -> dict:
    d = {"index": index, "title": title(item, kind), "guid": zx.text_of(item, "GUID"),
         "creator": zx.text_of(item, "CREATOR"), "modified": zx.text_of(item, "MODIFICATIONDATE")}
    if kind.name == "smartviews":
        d["rules"] = len(item.findall("RULES/RULE"))
    elif kind.name == "clashrules":
        d["source_rules"] = len(item.findall("SOURCESET/SMARTVIEW/RULES/RULE"))
        d["target_rules"] = len(item.findall("TARGETSET/SMARTVIEW/RULES/RULE"))
        d["min_distance"] = zx.text_of(item, "MINIMUMDISTANCE")
    elif kind.name == "lists":
        d["table_type"] = zx.text_of(item, "TABLETYPE")
    elif kind.name == "smartproperties":
        d["rules"] = len(item.findall("SMARTVIEW/RULES/RULE"))
    return d


def rule_to_dict(rule: ET.Element) -> dict:
    prop = rule.find("PROPERTY")
    cond = rule.find("CONDITION")
    act = rule.find("ACTION")
    d = {"ifc_type": zx.text_of(rule, "IFCTYPE"),
         "property": zx.text_of(prop, "NAME"), "property_set": zx.text_of(prop, "PROPERTYSETNAME"),
         "property_type": zx.text_of(prop, "TYPE"), "value_type": zx.text_of(prop, "VALUETYPE"),
         "unit": zx.text_of(prop, "UNIT"),
         "condition": zx.text_of(cond, "TYPE"), "action": zx.text_of(act, "TYPE")}
    if cond is not None and cond.find("VALUE") is not None:
        d["value"] = zx.text_of(cond, "VALUE")
    if cond is not None and cond.find("STATE") is not None:
        d["state"] = zx.text_of(cond, "STATE")
    if act is not None and act.find("R") is not None:
        d["color"] = [int(zx.text_of(act, c, "0")) for c in ("R", "G", "B", "A")]
    if act is not None and act.find("VALUE") is not None:
        d["action_value"] = zx.text_of(act, "VALUE")
    return d


def item_detail(item: ET.Element, kind: LibraryKind) -> dict:
    d = item_summary(item, kind, 0)
    d.pop("index", None)
    d["description"] = zx.text_of(item, "DESCRIPTION")
    if kind.name == "smartviews":
        d["rules"] = [rule_to_dict(r) for r in item.findall("RULES/RULE")]
    elif kind.name == "clashrules":
        d["source"] = [rule_to_dict(r) for r in item.findall("SOURCESET/SMARTVIEW/RULES/RULE")]
        d["target"] = [rule_to_dict(r) for r in item.findall("TARGETSET/SMARTVIEW/RULES/RULE")]
        for tag in ("CLASHTYPE", "DUPLICATETYPE", "DISTANCETYPE", "MINIMUMDISTANCE",
                    "PENETRATIONDEPTHTOLERANCEENABLED", "PENETRATIONDEPTHTOLERANCE",
                    "VOLUMETOLERANCEENABLED", "VOLUMETOLERANCE", "CLASHSAMEMODEL",
                    "CLASHSAMESYSTEM", "CLASHSUBPARTS"):
            d[tag.lower()] = zx.text_of(item, tag)
    elif kind.name == "lists":
        d["table_type"] = zx.text_of(item, "TABLETYPE")
        d["config_chars"] = len(zx.text_of(item, "CONFIG"))
    elif kind.name == "smartproperties":
        d["rules"] = [rule_to_dict(r) for r in item.findall("SMARTVIEW/RULES/RULE")]
    return d


# ---------------------------------------------------------------------------
# Extract / merge / validate
# ---------------------------------------------------------------------------

def extract_sets(doc, kind, refs: list[str]) -> zx.ZoomDocument:
    out = zx.ZoomDocument(roots=[ET.fromstring(ET.tostring(doc.header)), ET.Element(kind.body_tag)])
    for ref in refs:
        out.body.append(ET.fromstring(ET.tostring(find_set(doc, kind, ref))))
    return out


def merge(docs: list[zx.ZoomDocument], kind: LibraryKind, on_conflict: str = "rename") -> tuple[zx.ZoomDocument, list[dict]]:
    """Merge libraries. on_conflict: rename | skip | replace (by set title)."""
    out = new_library(kind)
    report = []
    for d in docs:
        if detect_kind(d) != kind:
            raise LibraryError("Cannot merge libraries of different kinds")
        for s in sets(d, kind):
            s = ET.fromstring(ET.tostring(s))
            name = title(s, kind)
            existing = next((e for e in sets(out, kind) if title(e, kind) == name), None)
            if existing is None:
                out.body.append(s)
                report.append({"set": name, "action": "added"})
            elif on_conflict == "skip":
                report.append({"set": name, "action": "skipped"})
            elif on_conflict == "replace":
                idx = list(out.body).index(existing)
                out.body.remove(existing)
                out.body.insert(idx, s)
                report.append({"set": name, "action": "replaced"})
            else:
                n = 2
                while any(title(e, kind) == f"{name} ({n})" for e in sets(out, kind)):
                    n += 1
                s.find(kind.title_tag).text = f"{name} ({n})"
                s.find("GUID").text = new_guid()
                out.body.append(s)
                report.append({"set": name, "action": f"renamed to '{name} ({n})'"})
    return out, report


def validate(doc, kind) -> list[dict]:
    """Structural checks. Returns a list of {level, where, message}."""
    issues = []

    def add(level, where, msg):
        issues.append({"level": level, "where": where, "message": msg})

    if doc.body.tag != kind.body_tag:
        add("error", "body", f"Expected <{kind.body_tag}>, found <{doc.body.tag}>")
        return issues
    guids: dict[str, str] = {}
    for si, s in enumerate(sets(doc, kind)):
        sname = title(s, kind) or f"#{si}"
        if not title(s, kind):
            add("error", sname, "Set has no title")
        # Zoom itself omits the container for empty sets, so its absence is not an error.
        seen_titles = set()
        for ii, it in enumerate(items(s, kind)):
            where = f"{sname} / {title(it, kind) or '#' + str(ii)}"
            g = zx.text_of(it, "GUID")
            if not g:
                add("error", where, "Item has no GUID")
            elif g in guids:
                add("warning", where, f"Duplicate GUID (also in {guids[g]})")
            else:
                guids[g] = where
            t = title(it, kind)
            if t in seen_titles:
                add("warning", where, "Duplicate title in set")
            seen_titles.add(t)
            rules = it.findall(".//RULE")
            for ri, r in enumerate(rules):
                if not zx.text_of(r, "IFCTYPE"):
                    add("error", f"{where} / rule {ri}", "Rule without IFCTYPE")
                if r.find("CONDITION/TYPE") is None or r.find("ACTION/TYPE") is None:
                    add("error", f"{where} / rule {ri}", "Rule without CONDITION/ACTION type")
                act = r.find("ACTION")
                if act is not None and act.find("R") is not None:
                    for c in ("R", "G", "B", "A"):
                        v = zx.text_of(act, c)
                        if not v.isdigit() or not 0 <= int(v) <= 255:
                            add("error", f"{where} / rule {ri}", f"Color channel {c}='{v}' not in 0-255")
            if kind.name == "smartviews" and not rules:
                add("warning", where, "Smart view has no rules")
    return issues
