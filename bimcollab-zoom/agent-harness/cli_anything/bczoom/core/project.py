"""BIMcollab Zoom project files (.bcp) and IFC header probing.

A .bcp lists the IFC models of a Zoom project (paths, sizes, IFC header data),
the coordinated origin and, for cloud projects, the BIMcollab Nexus connection.
Opening a .bcp in Zoom reloads all its models.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from cli_anything.bczoom.core import zoomxml as zx
from cli_anything.bczoom.core.library import APP_VERSION_STRING, LibraryError

MODEL_FIELDS = ["filepath", "filesize", "timestamp", "prefix", "modelloaded", "online",
                "originalfilename", "description", "implementationlevel", "author",
                "organization", "originatingsystem", "authorization", "fileschema", "discipline"]


# ---------------------------------------------------------------------------
# IFC header (ISO-10303-21) probing
# ---------------------------------------------------------------------------

def _step_strings(args: str) -> list[str]:
    return [s.replace("''", "'") for s in re.findall(r"'((?:[^']|'')*)'", args)]


def probe_ifc(path: str | Path) -> dict:
    """Read the HEADER section of an IFC-SPF file (first 64 KB only)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"IFC file not found: {p}")
    with p.open("rb") as f:
        head = f.read(65536).decode("latin-1", errors="replace")
    if not head.lstrip().startswith("ISO-10303-21"):
        raise LibraryError(f"{p.name} is not an IFC STEP file (missing ISO-10303-21 header)")
    end = head.find("ENDSEC;")
    header = head[:end if end > 0 else len(head)]
    header = re.sub(r"/\*.*?\*/", "", header, flags=re.S)
    info = {"path": str(p.resolve()), "size": p.stat().st_size,
            "modified": datetime.fromtimestamp(p.stat().st_mtime).replace(microsecond=0).isoformat(),
            "schema": "", "description": "", "implementation_level": "", "name": "",
            "timestamp": "", "author": "", "organization": "", "originating_system": "",
            "authorization": ""}
    m = re.search(r"FILE_DESCRIPTION\s*\((.*?)\)\s*;", header, re.S)
    if m:
        strs = _step_strings(m.group(1))
        if strs:
            info["implementation_level"] = strs[-1]
            info["description"] = ", ".join(strs[:-1])
    m = re.search(r"FILE_NAME\s*\((.*?)\)\s*;", header, re.S)
    if m:
        args = m.group(1)
        strs = _step_strings(args)
        groups = re.findall(r"\(([^()]*)\)", args)
        info["name"] = strs[0] if strs else ""
        info["timestamp"] = strs[1] if len(strs) > 1 else ""
        if len(groups) >= 2:
            info["author"] = ", ".join(s for s in _step_strings(groups[0]) if s)
            info["organization"] = ", ".join(s for s in _step_strings(groups[1]) if s)
        tail = re.split(r"\)\s*,", args)[-1]
        tail_strs = _step_strings(tail)
        if len(tail_strs) >= 3:
            info["originating_system"] = tail_strs[1]
            info["authorization"] = tail_strs[2]
        elif len(tail_strs) == 2:
            info["originating_system"] = tail_strs[0]
            info["authorization"] = tail_strs[1]
    m = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'", header)
    if m:
        info["schema"] = m.group(1)
    return info


# ---------------------------------------------------------------------------
# .bcp read/write
# ---------------------------------------------------------------------------

def load(path: str) -> zx.ZoomDocument:
    doc = zx.parse_file(path)
    if doc.roots[0].tag != "bimcollabprojectfile":
        raise LibraryError(f"{path} is not a Zoom project file (.bcp)")
    if doc.root("project") is None:
        raise LibraryError(f"{path} has no <project> element")
    return doc


def info(doc: zx.ZoomDocument) -> dict:
    conn = doc.root("bimcollabconnectiondata")
    proj = doc.root("project")
    models = []
    for i, m in enumerate(proj.findall("model")):
        fp = zx.text_of(m, "filepath")
        models.append({"index": i, "file": Path(fp).name, "path": fp,
                       "exists": Path(fp).exists(),
                       "loaded": zx.text_of(m, "modelloaded") == "true",
                       "online": zx.text_of(m, "online") == "true",
                       "schema": zx.text_of(m, "fileschema"),
                       "originating_system": zx.text_of(m, "originatingsystem"),
                       "timestamp": zx.text_of(m, "timestamp")})
    pp = doc.root("projectpath")
    return {"application_version": zx.text_of(doc.header, "applicationversion"),
            "connected": conn is not None,
            "service": zx.text_of(conn, "service") if conn is not None else "",
            "project_name": zx.text_of(conn, "projectname") if conn is not None else "",
            "project_guid": zx.text_of(conn, "projectguid") if conn is not None else "",
            "project_path": pp.text if pp is not None else "",
            "coordinated_origin": zx.text_of(proj, "coordinatedorigin"),
            "model_count": len(models), "missing_models": sum(not m["exists"] for m in models),
            "models": models}


def new_project() -> zx.ZoomDocument:
    header = ET.Element("bimcollabprojectfile")
    zx.sub(header, "version", "1")
    zx.sub(header, "applicationversion", APP_VERSION_STRING)
    proj = ET.Element("project")
    zx.sub(proj, "coordinatedorigin", "0.0000000000000000;0.0000000000000000;0.0000000000000000")
    return zx.ZoomDocument(roots=[header, proj])


def _model_element(ifc: dict, prefix: str = "", discipline: str = "") -> ET.Element:
    m = ET.Element("model")
    values = {
        "filepath": ifc["path"], "filesize": f"{ifc['size']:g}", "timestamp": ifc["modified"],
        "prefix": prefix, "modelloaded": "true", "online": "false",
        "originalfilename": ifc["name"], "description": ifc["description"],
        "implementationlevel": ifc["implementation_level"], "author": ifc["author"],
        "organization": ifc["organization"], "originatingsystem": ifc["originating_system"],
        "authorization": ifc["authorization"], "fileschema": ifc["schema"],
        "discipline": discipline,
    }
    for f in MODEL_FIELDS:
        zx.sub(m, f, values[f])
    return m


def add_model(doc: zx.ZoomDocument, ifc_path: str, prefix: str = "", discipline: str = "") -> dict:
    proj = doc.root("project")
    ifc = probe_ifc(ifc_path)
    for m in proj.findall("model"):
        if Path(zx.text_of(m, "filepath")).resolve() == Path(ifc["path"]):
            raise LibraryError(f"Model already in project: {ifc['path']}")
    proj.append(_model_element(ifc, prefix, discipline))
    return ifc


def remove_model(doc: zx.ZoomDocument, ref: str) -> str:
    proj = doc.root("project")
    models = proj.findall("model")
    for i, m in enumerate(models):
        fp = zx.text_of(m, "filepath")
        if (ref.isdigit() and int(ref) == i) or Path(fp).name == ref or fp == ref:
            proj.remove(m)
            return fp
    raise LibraryError(f"Model '{ref}' not found. Models: {[Path(zx.text_of(m, 'filepath')).name for m in models]}")


def relink(doc: zx.ZoomDocument, search_dirs: list[str]) -> list[dict]:
    """Repoint missing model paths to files with the same name found in search_dirs."""
    report = []
    for m in doc.root("project").findall("model"):
        el = m.find("filepath")
        old = el.text or ""
        if Path(old).exists():
            continue
        name = Path(old).name
        found = None
        for d in search_dirs:
            hits = list(Path(d).rglob(name))
            if hits:
                found = hits[0]
                break
        if found:
            el.text = str(found.resolve())
            report.append({"model": name, "from": old, "to": el.text})
        else:
            report.append({"model": name, "from": old, "to": None})
    return report
