"""Dependency-free BCF 2.1 writer (stdlib only: zipfile + xml.etree).

Design choices for BIMcollab Zoom 9.5 (BCF 1.0/2.0/2.1 reader, no 3.0):
  * first viewpoint is named ``viewpoint.bcfv`` + ``snapshot.png`` (2.0-style names, read by every 2.x reader),
    further viewpoints ``viewpoint-<guid>.bcfv`` / ``snapshot-<guid>.png``;
  * ``extensions.xsd`` (2.1 redefine) declares TopicType/TopicStatus/Priority/Labels/Stage/Users enumerations
    so offline readers can fill their drop-downs; referenced from project.bcfp/ExtensionSchema;
  * colours: 6 hex digits RGB (or 8 = ARGB), upper case (2.1 XSD pattern is [0-9,A-F]).
"""
from __future__ import annotations

import math
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET


# --------------------------------------------------------------------------- data model
@dataclass
class Camera:
    eye: tuple[float, float, float]
    direction: tuple[float, float, float]
    up: tuple[float, float, float]
    fov: float = 60.0                 # perspective; BCF 2.1 XSD: 45..60
    ortho_scale: float | None = None  # if set -> OrthogonalCamera (ViewToWorldScale, metres)

    @classmethod
    def look_at_bbox(cls, bmin, bmax, view_dir=(-1.0, -1.0, -0.8), fov=60.0, margin=1.5, min_radius=0.5):
        c = [(a + b) / 2 for a, b in zip(bmin, bmax)]
        r = max(math.dist(bmin, bmax) / 2, min_radius)
        dist = r / math.tan(math.radians(fov) / 2) * margin
        d = _unit(view_dir)
        eye = tuple(ci - di * dist for ci, di in zip(c, d))
        right = _cross(d, (0.0, 0.0, 1.0))
        if math.hypot(*right) < 1e-6:
            right = _cross(d, (0.0, 1.0, 0.0))
        up = _cross(_unit(right), d)
        return cls(eye, d, _unit(up), fov)


@dataclass
class Viewpoint:
    camera: Camera
    selected: list[str] = field(default_factory=list)              # IfcGuids
    colors: dict[str, list[str]] = field(default_factory=dict)      # "FF0000" -> [IfcGuid, ...]
    default_visibility: bool = True
    exceptions: list[str] = field(default_factory=list)             # visibility exceptions
    clipping_planes: list[tuple[tuple, tuple]] = field(default_factory=list)  # (location, direction)
    lines: list[tuple[tuple, tuple]] = field(default_factory=list)
    snapshot_png: bytes | None = None
    spaces_visible: bool = False
    openings_visible: bool = False
    guid: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class Comment:
    text: str
    author: str
    date: datetime | None = None
    viewpoint_guid: str | None = None
    guid: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class Topic:
    title: str
    author: str
    description: str = ""
    topic_type: str = "Clash"
    status: str = "Active"
    priority: str | None = None
    labels: list[str] = field(default_factory=list)
    assigned_to: str | None = None
    stage: str | None = None          # BIMcollab "Milestone"
    due_date: datetime | None = None
    index: int | None = None
    reference_links: list[str] = field(default_factory=list)
    viewpoints: list[Viewpoint] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    creation_date: datetime | None = None
    guid: str = field(default_factory=lambda: str(uuid.uuid4()))


# --------------------------------------------------------------------------- helpers
def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return tuple(x / n for x in v)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _iso(d: datetime | None) -> str:
    d = d or datetime.now(timezone.utc)
    if d.tzinfo is None:
        d = d.astimezone()
    return d.replace(microsecond=0).isoformat()


def _sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: v for k, v in attrs.items() if v is not None})
    if text is not None:
        el.text = str(text)
    return el


def _xyz(parent, tag, v):
    el = _sub(parent, tag)
    for k, x in zip("XYZ", v):
        _sub(el, k, repr(float(x)))
    return el


def _bytes(root) -> bytes:
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def _check_guid(g: str) -> str:
    if len(g) != 22:
        raise ValueError(f"IfcGuid must be 22 chars (IFC base64), got {g!r}")
    return g


# --------------------------------------------------------------------------- XML builders
def visinfo_xml(vp: Viewpoint) -> bytes:
    root = ET.Element("VisualizationInfo", Guid=vp.guid)
    comps = _sub(root, "Components")
    _sub(comps, "ViewSetupHints", SpacesVisible=str(vp.spaces_visible).lower(),
         SpaceBoundariesVisible="false", OpeningsVisible=str(vp.openings_visible).lower())
    if vp.selected:
        sel = _sub(comps, "Selection")
        for g in vp.selected:
            _sub(sel, "Component", IfcGuid=_check_guid(g))
    vis = _sub(comps, "Visibility", DefaultVisibility=str(vp.default_visibility).lower())  # mandatory in 2.1
    if vp.exceptions:
        ex = _sub(vis, "Exceptions")
        for g in vp.exceptions:
            _sub(ex, "Component", IfcGuid=_check_guid(g))
    if vp.colors:
        col = _sub(comps, "Coloring")
        for color, guids in vp.colors.items():
            c = _sub(col, "Color", Color=color.upper().lstrip("#"))
            for g in guids:
                _sub(c, "Component", IfcGuid=_check_guid(g))
    cam = vp.camera
    tag = "OrthogonalCamera" if cam.ortho_scale else "PerspectiveCamera"
    ce = _sub(root, tag)
    _xyz(ce, "CameraViewPoint", cam.eye)
    _xyz(ce, "CameraDirection", cam.direction)
    _xyz(ce, "CameraUpVector", cam.up)
    if cam.ortho_scale:
        _sub(ce, "ViewToWorldScale", repr(float(cam.ortho_scale)))
    else:
        _sub(ce, "FieldOfView", repr(float(min(max(cam.fov, 45.0), 60.0))))
    if vp.lines:
        ls = _sub(root, "Lines")
        for a, b in vp.lines:
            ln = _sub(ls, "Line")
            _xyz(ln, "StartPoint", a)
            _xyz(ln, "EndPoint", b)
    if vp.clipping_planes:
        cps = _sub(root, "ClippingPlanes")
        for loc, d in vp.clipping_planes:
            cp = _sub(cps, "ClippingPlane")
            _xyz(cp, "Location", loc)
            _xyz(cp, "Direction", d)
    return _bytes(root)


def _vp_names(i: int, vp: Viewpoint) -> tuple[str, str]:
    return ("viewpoint.bcfv", "snapshot.png") if i == 0 else (f"viewpoint-{vp.guid}.bcfv", f"snapshot-{vp.guid}.png")


def markup_xml(t: Topic, ifc_files: list[dict]) -> bytes:
    root = ET.Element("Markup")
    if ifc_files:
        h = _sub(root, "Header")
        for f in ifc_files:
            fe = _sub(h, "File", IfcProject=f.get("ifc_project"), isExternal="true")
            _sub(fe, "Filename", f["filename"])
            if f.get("date"):
                _sub(fe, "Date", _iso(f["date"]))
            if f.get("reference"):
                _sub(fe, "Reference", f["reference"])
    te = _sub(root, "Topic", Guid=t.guid, TopicType=t.topic_type, TopicStatus=t.status)
    for link in t.reference_links:
        _sub(te, "ReferenceLink", link)
    _sub(te, "Title", t.title)
    if t.priority:
        _sub(te, "Priority", t.priority)
    if t.index is not None:
        _sub(te, "Index", t.index)
    for lab in t.labels:
        _sub(te, "Labels", lab)
    _sub(te, "CreationDate", _iso(t.creation_date))
    _sub(te, "CreationAuthor", t.author)
    if t.due_date:
        _sub(te, "DueDate", _iso(t.due_date))
    if t.assigned_to:
        _sub(te, "AssignedTo", t.assigned_to)
    if t.stage:
        _sub(te, "Stage", t.stage)
    if t.description:
        _sub(te, "Description", t.description)
    for c in t.comments:
        ce = _sub(root, "Comment", Guid=c.guid)
        _sub(ce, "Date", _iso(c.date))
        _sub(ce, "Author", c.author)
        _sub(ce, "Comment", c.text)
        if c.viewpoint_guid:
            _sub(ce, "Viewpoint", Guid=c.viewpoint_guid)
    for i, vp in enumerate(t.viewpoints):
        bcfv, png = _vp_names(i, vp)
        ve = _sub(root, "Viewpoints", Guid=vp.guid)
        _sub(ve, "Viewpoint", bcfv)
        if vp.snapshot_png:
            _sub(ve, "Snapshot", png)
        _sub(ve, "Index", i)
    return _bytes(root)


def extensions_xsd(topics: list[Topic], extra: dict[str, list[str]] | None = None) -> bytes:
    """BCF 2.1 extension schema (xs:redefine of markup.xsd simple types)."""
    vals = {"TopicType": {t.topic_type for t in topics}, "TopicStatus": {"Active", "Resolved", "Closed"},
            "Priority": {t.priority for t in topics if t.priority},
            "TopicLabel": {lab for t in topics for lab in t.labels},
            "Stage": {t.stage for t in topics if t.stage},
            "UserIdType": {u for t in topics for u in (t.author, t.assigned_to) if u}}
    for k, v in (extra or {}).items():
        vals.setdefault(k, set()).update(v)
    xs = "http://www.w3.org/2001/XMLSchema"
    ET.register_namespace("xs", xs)
    root = ET.Element(f"{{{xs}}}schema")
    red = ET.SubElement(root, f"{{{xs}}}redefine", schemaLocation="markup.xsd")
    for name, values in vals.items():
        if not values:
            continue
        st = ET.SubElement(red, f"{{{xs}}}simpleType", name=name)
        rs = ET.SubElement(st, f"{{{xs}}}restriction", base=name)
        for v in sorted(values):
            ET.SubElement(rs, f"{{{xs}}}enumeration", value=v)
    return _bytes(root)


def write_bcf21(path: str | Path, topics: list[Topic], project_name: str = "bim-assistant",
                project_id: str | None = None, ifc_files: list[dict] | None = None,
                with_extensions: bool = True) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("bcf.version", _bytes(_version()))
        pe = ET.Element("ProjectExtension")
        p = _sub(pe, "Project", ProjectId=project_id or str(uuid.uuid4()))
        _sub(p, "Name", project_name)
        _sub(pe, "ExtensionSchema", "extensions.xsd" if with_extensions else "")
        z.writestr("project.bcfp", _bytes(pe))
        if with_extensions:
            z.writestr("extensions.xsd", extensions_xsd(topics))
        for t in topics:
            z.writestr(f"{t.guid}/markup.bcf", markup_xml(t, ifc_files or []))
            for i, vp in enumerate(t.viewpoints):
                bcfv, png = _vp_names(i, vp)
                z.writestr(f"{t.guid}/{bcfv}", visinfo_xml(vp))
                if vp.snapshot_png:
                    z.writestr(f"{t.guid}/{png}", vp.snapshot_png)
    return path


def _version():
    v = ET.Element("Version", VersionId="2.1")
    _sub(v, "DetailedVersion", "2.1")
    return v


def summary(path: str | Path) -> dict:
    """Topics of a .bcf/.bcfzip (title, status, viewpoint/snapshot presence)."""
    out = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        version = ET.fromstring(z.read("bcf.version")).get("VersionId") if "bcf.version" in names else None
        for n in sorted(names):
            if not n.endswith("/markup.bcf"):
                continue
            t = ET.fromstring(z.read(n)).find("Topic")
            folder = n.rsplit("/", 1)[0]
            out.append({"guid": t.get("Guid"), "title": t.findtext("Title"), "status": t.get("TopicStatus"),
                        "viewpoint": f"{folder}/viewpoint.bcfv" in names,
                        "snapshot": f"{folder}/snapshot.png" in names})
    return {"file": str(path), "version": version, "topics": len(out), "items": out}
