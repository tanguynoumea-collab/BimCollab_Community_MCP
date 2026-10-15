"""IFC analysis engine (IfcOpenShell) with an in-memory model cache.

Field syntax (``fields`` / ``group_by``) is IfcOpenShell's selector value syntax:
  class | id | Name | Description | predefined_type | type.Name | storey.Name |
  building.Name | material.Name | classification.Identification |
  Pset_WallCommon.FireRating | /Pset_.*Common/.LoadBearing | Qto_WallBaseQuantities.NetVolume
Element filters (``selector``) use IfcOpenShell's filter syntax, e.g.
  "IfcWall, IfcSlab"  |  "IfcDoor, Pset_DoorCommon.FireRating=EI30"  |
  "IfcElement, /Pset_.*Common/.LoadBearing=TRUE"  |  "IfcWall, storey.Name=T_12_P02"
"""

from __future__ import annotations

import csv
import functools
import hashlib
import os
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.element as uel
import ifcopenshell.util.selector as usel
import ifcopenshell.util.unit as uunit

DEFAULT_FIELDS = ["GlobalId", "class", "Name", "type.Name", "storey.Name"]
DEFAULT_SELECTOR = "IfcElement"
MAX_ROWS = 500


class IfcEngineError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Model cache
# ---------------------------------------------------------------------------

def _fast_props(pdef) -> dict | None:
    """Single values / quantities read directly (~5x faster than get_property_definition);
    None when the definition holds other property kinds."""
    # Positional access (p[i]) avoids entity_instance.__getattr__, which dominated the build
    # (3 M calls, ~13 s on a 17k-element model).
    out = {}
    cls = pdef.is_a()
    if cls == "IfcPropertySet":
        for p in pdef[4] or []:                     # HasProperties
            if p.is_a() != "IfcPropertySingleValue":
                return None
            nv = p[2]                               # NominalValue
            out[p[0]] = nv[0] if nv is not None else None
        return out
    if cls == "IfcElementQuantity":
        for q in pdef[5] or []:                     # Quantities
            if not q.is_a().startswith("IfcQuantity"):
                return None
            out[q[0]] = q[3]                        # Length/Area/Volume/Count/WeightValue
        return out
    return None


class PropertyIndex:
    """All psets/qtos of a model, read once (walking IfcRelDefinesByProperties / types),
    so lookups are O(1) instead of a get_psets() per element per call (~12 s on 17k elements)."""

    def __init__(self, model: ifcopenshell.file):
        t = time.time()
        self.occ: dict[int, dict[str, dict]] = defaultdict(dict)
        self.qto_names: set[str] = set()
        cache: dict[int, dict] = {}

        def props(pdef):
            d = cache.get(pdef.id())
            if d is None:
                d = _fast_props(pdef)
                if d is None:  # unusual definitions: library fallback
                    try:
                        d = uel.get_property_definition(pdef) or {}
                    except Exception:  # noqa: BLE001
                        d = {}
                    d.pop("id", None)
                cache[pdef.id()] = d
            return d

        for rel in model.by_type("IfcRelDefinesByProperties"):
            pdef = rel.RelatingPropertyDefinition
            name = getattr(pdef, "Name", None)
            if not name:
                continue
            if pdef.is_a("IfcElementQuantity"):
                self.qto_names.add(name)
            d = props(pdef)
            for o in rel.RelatedObjects:
                self.occ[o.id()][name] = d
        self.type_of: dict[int, ifcopenshell.entity_instance] = {}
        for rel in model.by_type("IfcRelDefinesByType"):
            for o in rel.RelatedObjects:
                self.type_of[o.id()] = rel.RelatingType
        self.typ: dict[int, dict[str, dict]] = {}
        for t_ in model.by_type("IfcTypeObject"):
            d = {}
            for pdef in getattr(t_, "HasPropertySets", None) or []:
                if getattr(pdef, "Name", None):
                    d[pdef.Name] = props(pdef)
            self.typ[t_.id()] = d
        self.build_s = round(time.time() - t, 2)

    def psets(self, el, qtos: bool = True) -> dict[str, dict]:
        d = self.occ.get(el.id(), {})
        return d if qtos else {k: v for k, v in d.items() if k not in self.qto_names}

    def type_psets(self, el) -> dict[str, dict]:
        t = self.type_of.get(el.id())
        return self.typ.get(t.id(), {}) if t is not None else {}

    def get(self, el, pset: str, prop: str):
        for ps in (self.occ.get(el.id(), {}), self.type_psets(el)):
            props = ps.get(pset)
            if props and prop in props:
                return props[prop]
        return None


@dataclass
class LoadedModel:
    alias: str
    path: str
    mtime: float
    size: int
    load_s: float
    model: ifcopenshell.file
    length_scale: float      # project length unit -> metres
    area_scale: float
    volume_scale: float
    _index: PropertyIndex | None = None
    _summary: dict | None = None
    last_used: float = 0.0

    def __post_init__(self):
        self._index_lock = threading.Lock()

    @property
    def index(self) -> PropertyIndex:
        with self._index_lock:  # built once; a background warm-up may already be running
            if self._index is None:
                self._index = PropertyIndex(self.model)
        return self._index

    def warm_up(self) -> None:
        """Build the property index in the background right after loading (~9 s on 17k
        elements), so it is usually ready before the first query needs it."""
        threading.Thread(target=lambda: self.index, daemon=True, name=f"index-{self.alias}").start()


MAX_MODELS = int(os.environ.get("BIM_ASSISTANT_MAX_MODELS", "2"))


class ModelStore:
    def __init__(self):
        self._models: dict[str, LoadedModel] = {}

    def _evict(self, keep: LoadedModel) -> None:
        """Keep at most MAX_MODELS in memory (~0.7 GB each for a 170 MB IFC); drop the least
        recently used, and any older copy of the same file."""
        for a, lm in list(self._models.items()):
            if lm is not keep and lm.path == keep.path:
                del self._models[a]
        while len(self._models) > MAX_MODELS:
            a = min((x for x in self._models if self._models[x] is not keep),
                    key=lambda x: self._models[x].last_used)
            del self._models[a]

    def _alias_for(self, path: Path) -> str:
        base = path.stem
        alias, n = base, 2
        while alias in self._models and self._models[alias].path != str(path):
            alias, n = f"{base}~{n}", n + 1
        return alias

    def open(self, path: str, alias: str | None = None, reload: bool = False) -> LoadedModel:
        p = Path(path).expanduser()
        if not p.exists():
            raise IfcEngineError(f"IFC file not found: {p}")
        p = p.resolve()
        st = p.stat()
        for lm in self._models.values():
            if lm.path == str(p) and not reload and lm.mtime == st.st_mtime:
                lm.last_used = time.time()
                return lm
        t = time.time()
        try:
            model = ifcopenshell.open(str(p))
        except Exception as exc:  # noqa: BLE001 - ifcopenshell raises various types
            raise IfcEngineError(f"Cannot read {p.name}: {exc}") from exc
        alias = alias or next((a for a, lm in self._models.items() if lm.path == str(p)), None) \
            or self._alias_for(p)

        def scale(kind):
            try:
                return uunit.calculate_unit_scale(model, kind)
            except Exception:  # noqa: BLE001
                return 1.0

        lm = LoadedModel(alias, str(p), st.st_mtime, st.st_size, round(time.time() - t, 2), model,
                         scale("LENGTHUNIT"), scale("AREAUNIT"), scale("VOLUMEUNIT"),
                         last_used=time.time())
        self._models[alias] = lm
        self._evict(lm)
        return lm

    def get(self, ref: str) -> LoadedModel:
        """Get by alias or path; opens the file if a path is given. Waits for a background
        index build so the ifcopenshell file is never read by two threads at once."""
        lm = self._models.get(ref) or next(
            (m for m in self._models.values()
             if Path(m.path).name.lower() == ref.lower() or m.path.lower() == ref.lower()), None)
        if lm is not None:
            lm.last_used = time.time()
            lm.index  # noqa: B018 - blocks until a running warm-up has finished
            return lm
        if Path(ref).suffix.lower() in (".ifc", ".ifczip", ".ifcxml") or Path(ref).exists():
            return self.open(ref)
        raise IfcEngineError(f"Model '{ref}' is not loaded. Loaded: {list(self._models)}. "
                             "Pass a file path or call ifc_open first.")

    def close(self, ref: str) -> str:
        lm = self.get(ref)
        del self._models[lm.alias]
        return lm.alias

    def list(self) -> list[dict]:
        return [{"alias": lm.alias, "path": lm.path, "schema": lm.model.schema,
                 "size_mb": round(lm.size / 1e6, 1), "load_s": lm.load_s,
                 "elements": len(lm.model.by_type("IfcElement"))} for lm in self._models.values()]


STORE = ModelStore()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jsonable(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, ifcopenshell.entity_instance):
        name = getattr(v, "Name", None) if hasattr(v, "Name") else None
        return f"{v.is_a()}#{v.id()}" + (f" '{name}'" if name else "")
    return str(v)


def select(lm: LoadedModel, selector: str | None) -> list:
    q = (selector or DEFAULT_SELECTOR).strip()
    try:
        els = usel.filter_elements(lm.model, q)
    except Exception as exc:  # noqa: BLE001 - lark parse errors etc.
        raise IfcEngineError(f"Invalid selector '{q}': {exc}") from exc
    return sorted(els, key=lambda e: e.id())


_SELECTOR_KEYWORDS = {"type", "types", "occurrences", "container", "space", "storey", "building",
                      "site", "parent", "classification", "group", "system", "zone", "material",
                      "materials", "mat", "item", "profiles", "query"}


@functools.lru_cache(maxsize=512)
def _split_pset_field(field: str) -> tuple[str, str] | None:
    """'Pset name.Prop' -> (pset, prop) for a plain pset lookup, else None."""
    if "." not in field or field.startswith("/"):
        return None
    pset, prop = field.split(".", 1)
    pset = pset.strip().strip('"')
    if pset.lower() in _SELECTOR_KEYWORDS:
        return None
    return pset, prop.strip().strip('"')


_NO_KEYS = object()


@functools.lru_cache(maxsize=512)
def _parsed_keys(field: str):
    """Selector value grammar parsed once per field (a lark Earley parse costs ~0.15 ms; it was
    re-run for every element: 2.8 s per 17k-element pass)."""
    try:
        return usel.GetElementTransformer().transform(usel.get_element_grammar.parse(field))
    except Exception:  # noqa: BLE001 - names with spaces/accents are not parseable
        return _NO_KEYS


def _pset_value(el, field: str, idx: PropertyIndex | None = None):
    """Direct 'Pset name.Prop' lookup; handles names with spaces/accents ('Données',
    'Protection contre les incendies') that the selector value grammar does not parse."""
    sp = _split_pset_field(field)
    if sp is None:
        return None
    if idx is not None:
        return idx.get(el, *sp)
    pset, prop = sp
    for ps in (uel.get_psets(el), uel.get_psets(uel.get_type(el)) if uel.get_type(el) else {}):
        props = ps.get(pset)
        if props and prop in props:
            return props[prop]
    return None


def value(el, field: str, idx: PropertyIndex | None = None):
    if field == "GlobalId":
        return getattr(el, "GlobalId", None)
    if idx is not None and _split_pset_field(field) is not None:
        # O(1) from the model index, which holds every occurrence + type pset: a miss is final
        # (falling back to the selector grammar re-read all psets: 5 s per pass on misses).
        return _pset_value(el, field, idx)
    keys = _parsed_keys(field)
    v = None
    if keys is not _NO_KEYS:
        try:
            v = usel._get_element_value(el, keys)  # noqa: SLF001 - pre-parsed keys (0.9.x API)
        except Exception:  # noqa: BLE001
            v = None
    if v is None and idx is None:
        v = _pset_value(el, field)
    return v


def _key(v) -> str:
    if v is None or v == "":
        return "(vide)"
    if isinstance(v, (list, tuple)):
        return ", ".join(sorted(str(_jsonable(x)) for x in v)) or "(vide)"
    return str(_jsonable(v))


def header_info(model) -> dict:
    h = model.header
    out = {}
    try:
        out["description"] = list(h.file_description.description)
        fn = h.file_name
        out.update({"name": fn.name, "timestamp": fn.time_stamp,
                    "author": list(fn.author), "organization": list(fn.organization),
                    "originating_system": fn.originating_system,
                    "preprocessor": fn.preprocessor_version})
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------

def summary(lm: LoadedModel) -> dict:
    if lm._summary is None:
        lm._summary = _summary(lm)
    return lm._summary


def _summary(lm: LoadedModel) -> dict:
    m = lm.model
    els = m.by_type("IfcElement")
    by_class = Counter(e.is_a() for e in els)
    storeys = []
    contained = Counter()
    for e in els:
        c = uel.get_container(e)
        if c is not None:
            contained[c.id()] += 1
    for s in sorted(m.by_type("IfcBuildingStorey"), key=lambda s: s.Elevation or 0):
        storeys.append({"name": s.Name, "elevation_m": round((s.Elevation or 0) * lm.length_scale, 3),
                        "elements": contained.get(s.id(), 0)})
    apps = [{"name": a.ApplicationFullName, "version": a.Version}
            for a in m.by_type("IfcApplication")]
    georef = []
    for mc in (m.by_type("IfcMapConversion") if m.schema != "IFC2X3" else []):
        crs = mc.TargetCRS
        georef.append({"crs": getattr(crs, "Name", None), "eastings": mc.Eastings,
                       "northings": mc.Northings, "height": mc.OrthogonalHeight})
    project = (m.by_type("IfcProject") or [None])[0]
    psets = Counter()
    for r in m.by_type("IfcRelDefinesByProperties"):
        pd = r.RelatingPropertyDefinition
        if hasattr(pd, "Name"):
            psets[pd.Name] += len(r.RelatedObjects)
    return {
        "alias": lm.alias, "file": Path(lm.path).name, "path": lm.path,
        "size_mb": round(lm.size / 1e6, 1), "schema": m.schema, "load_s": lm.load_s,
        "header": header_info(m), "authoring_apps": apps,
        "length_unit_to_m": lm.length_scale,
        "project": getattr(project, "Name", None),
        "sites": [s.Name for s in m.by_type("IfcSite")],
        "buildings": [b.Name for b in m.by_type("IfcBuilding")],
        "storeys": storeys, "spaces": len(m.by_type("IfcSpace")),
        "element_count": len(els),
        "elements_by_class": dict(by_class.most_common()),
        "types": len(m.by_type("IfcTypeObject")),
        "materials": len(m.by_type("IfcMaterial")),
        "classification_systems": sorted({c.Name for c in m.by_type("IfcClassification") if c.Name}),
        "property_sets_top": dict(psets.most_common(25)),
        "georeferencing": georef,
    }


def query(lm, selector=None, fields=None, limit=100, offset=0) -> dict:
    fields = fields or DEFAULT_FIELDS
    els = select(lm, selector)
    limit = max(1, min(int(limit), MAX_ROWS))
    idx = lm.index
    rows = [{f: _jsonable(value(e, f, idx)) for f in fields} for e in els[offset:offset + limit]]
    return {"selector": selector or DEFAULT_SELECTOR, "total": len(els), "offset": offset,
            "returned": len(rows), "truncated": offset + len(rows) < len(els),
            "fields": fields, "rows": rows}


def count(lm, selector=None, group_by="class", top=100) -> dict:
    els = select(lm, selector)
    groups = group_by if isinstance(group_by, list) else [group_by]
    idx = lm.index
    c = Counter(" | ".join(_key(value(e, g, idx)) for g in groups) for e in els)
    return {"selector": selector or DEFAULT_SELECTOR, "group_by": groups, "total": len(els),
            "distinct": len(c), "counts": dict(c.most_common(top))}


def distinct_values(lm, selector, field, top=50) -> dict:
    els = select(lm, selector)
    idx = lm.index
    c = Counter(_key(value(e, field, idx)) for e in els)
    return {"selector": selector, "field": field, "total": len(els), "distinct": len(c),
            "values": dict(c.most_common(top))}


def element(lm, ref: str) -> dict:
    m = lm.model
    try:
        el = m.by_guid(ref)
    except RuntimeError:
        el = None
    if el is None and ref.lstrip("#").isdigit():
        try:
            el = m.by_id(int(ref.lstrip("#")))
        except RuntimeError:
            el = None
    if el is None:
        raise IfcEngineError(f"No element with GlobalId or id '{ref}' in {lm.alias}")
    info = {k: _jsonable(v) for k, v in el.get_info(recursive=False).items()
            if not isinstance(v, (ifcopenshell.entity_instance, tuple)) or k == "type"}
    t = uel.get_type(el)
    cont = uel.get_container(el)
    mats = uel.get_materials(el) if hasattr(uel, "get_materials") else []
    parent = uel.get_aggregate(el) if hasattr(uel, "get_aggregate") else None
    out = {
        "alias": lm.alias, "id": el.id(), "class": el.is_a(), "attributes": info,
        "predefined_type": uel.get_predefined_type(el),
        "type": None if t is None else {"class": t.is_a(), "name": t.Name, "guid": t.GlobalId,
                                        "psets": _jsonable(uel.get_psets(t))},
        "container": None if cont is None else {"class": cont.is_a(), "name": cont.Name},
        "aggregate_parent": None if parent is None else {"class": parent.is_a(), "name": parent.Name},
        "materials": [getattr(x, "Name", None) for x in mats],
        "psets": _jsonable(uel.get_psets(el, psets_only=True)),
        "quantities": _jsonable(uel.get_psets(el, qtos_only=True)),
        "classifications": [_jsonable(r.RelatingClassification)
                            for r in getattr(el, "HasAssociations", [])
                            if r.is_a("IfcRelAssociatesClassification")],
    }
    if hasattr(el, "HasOpenings"):
        out["openings"] = len(el.HasOpenings)
    if hasattr(el, "FillsVoids"):
        out["fills_opening_in"] = [_jsonable(r.RelatingOpeningElement.VoidsElements[0].RelatingBuildingElement)
                                   for r in el.FillsVoids if r.RelatingOpeningElement.VoidsElements]
    return out


def properties_catalog(lm, selector=None, include_quantities=True, top_values=5) -> dict:
    """What information exists on the selected elements: every pset.prop with fill rate."""
    els = select(lm, selector)
    stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "values": Counter(), "types": Counter()})
    idx = lm.index
    for e in els:
        for source, ps in (("occurrence", idx.psets(e, qtos=include_quantities)),
                           ("type", idx.type_psets(e))):
            for pset, props in ps.items():
                for prop, v in props.items():
                    if prop == "id":
                        continue
                    k = f"{pset}.{prop}"
                    s = stats[k]
                    s["count"] += 1
                    s["types"][type(v).__name__] += 1
                    s["values"][_key(v)] += 1
                    s.setdefault("source", set()).add(source)
    n = len(els) or 1
    props = []
    for k, s in sorted(stats.items()):
        props.append({"field": k, "filled": s["count"], "fill_pct": round(100 * min(s["count"], n) / n, 1),
                      "value_type": s["types"].most_common(1)[0][0],
                      "distinct": len(s["values"]), "source": sorted(s["source"]),
                      "top_values": dict(s["values"].most_common(top_values))})
    return {"selector": selector or DEFAULT_SELECTOR, "elements": len(els),
            "property_count": len(props), "properties": props}


_QTY_ATTR = {"IfcQuantityLength": ("LengthValue", "length"),
             "IfcQuantityArea": ("AreaValue", "area"),
             "IfcQuantityVolume": ("VolumeValue", "volume"),
             "IfcQuantityCount": ("CountValue", "count"),
             "IfcQuantityWeight": ("WeightValue", "weight")}


def _element_quantities(el):
    for rel in getattr(el, "IsDefinedBy", []) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        q = rel.RelatingPropertyDefinition
        if not q.is_a("IfcElementQuantity"):
            continue
        for qty in q.Quantities:
            spec = _QTY_ATTR.get(qty.is_a())
            if spec:
                v = getattr(qty, spec[0], None)
                if v is not None:
                    yield q.Name, qty.Name, spec[1], float(v)


def _material_names(mat) -> list[str]:
    if mat is None:
        return []
    if mat.is_a("IfcMaterial"):
        return [mat.Name or ""]
    if mat.is_a("IfcMaterialLayerSet"):
        return [l.Material.Name or "" for l in mat.MaterialLayers if l.Material]
    if mat.is_a("IfcMaterialConstituentSet"):
        return [c.Material.Name or "" for c in (mat.MaterialConstituents or []) if c.Material]
    if mat.is_a("IfcMaterialProfileSet"):
        return [p.Material.Name or "" for p in mat.MaterialProfiles if p.Material]
    if mat.is_a("IfcMaterialList"):
        return [m.Name or "" for m in mat.Materials]
    return []


def effective_material(el) -> tuple[list[str], str]:
    """(material names, source) with source 'occurrence' | 'type' | 'none'. Constituent, layer
    and profile sets are flattened. Zoom's 'Material Name' only sees the occurrence material."""
    occ = uel.get_material(el, should_skip_usage=True, should_inherit=False)
    if occ is not None:
        return _material_names(occ), "occurrence"
    inh = uel.get_material(el, should_skip_usage=True)
    if inh is not None:
        return _material_names(inh), "type"
    return [], "none"


def _norm(s: str) -> str:
    """Case- and accent-insensitive form ('Béton' == 'BETON')."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower()


def _matches_material(el, needle: str, idx: PropertyIndex, also_type_name: bool) -> bool:
    n = _norm(needle)
    names, _ = effective_material(el)
    if any(n in _norm(x) for x in names):
        return True
    if also_type_name:
        t = idx.type_of.get(el.id())
        return bool(t is not None and n in _norm(t.Name or ""))
    return False


def materials(lm, selector=None, group_by: str | list[str] = "class", top=200) -> dict:
    """Effective material per element (occurrence vs inherited from type vs none), counted per
    group — the reliable way to find e.g. all concrete, including elements whose material is
    only on their type or inside a constituent set."""
    els = select(lm, selector)
    groups = group_by if isinstance(group_by, list) else [group_by]
    idx = lm.index
    c = Counter()
    sources = Counter()
    for e in els:
        names, src = effective_material(e)
        sources[src] += 1
        g = " | ".join(_key(value(e, x, idx)) for x in groups)
        c[f"{g} | {' + '.join(sorted(set(names))) or '(aucun)'} | {src}"] += 1
    return {"selector": selector or DEFAULT_SELECTOR, "elements": len(els),
            "by_source": dict(sources),
            "key": groups + ["material(s)", "source"],
            "counts": dict(c.most_common(top))}


def quantities(lm, selector=None, group_by: str | list[str] = "class", names: list[str] | None = None,
               material_contains: str | None = None, also_type_name: bool = False,
               qto_pattern: str | None = None) -> dict:
    """Sum IFC base quantities (Qto_*) per group, converted to m / m² / m³.

    group_by: one field or a list (e.g. ["class", "type.Name"]).
    material_contains: keep elements whose effective material (occurrence, type, constituents)
      contains this text; also_type_name=True also keeps elements whose type name contains it.
    qto_pattern: regex on the quantity set name (e.g. 'Qto_.*BaseQuantities').
    Each quantity name is counted once per element (no double count if a tool exports both
    'BaseQuantities' and 'Qto_*')."""
    import re
    els = select(lm, selector)
    idx = lm.index
    if material_contains:
        els = [e for e in els if _matches_material(e, material_contains, idx, also_type_name)]
    rx = re.compile(qto_pattern) if qto_pattern else None
    gfields = group_by if isinstance(group_by, list) else ([group_by] if group_by else [])
    scale = {"length": lm.length_scale, "area": lm.area_scale, "volume": lm.volume_scale,
             "count": 1.0, "weight": 1.0}
    unit = {"length": "m", "area": "m2", "volume": "m3", "count": "u", "weight": "kg"}
    groups: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    totals: dict[str, float] = defaultdict(float)
    elcount = Counter()
    without = 0
    for e in els:
        g = " | ".join(_key(value(e, f, idx)) for f in gfields) if gfields else "total"
        elcount[g] += 1
        seen: set[str] = set()
        for qset, qname, kind, v in _element_quantities(e):
            if names and qname not in names:
                continue
            if rx and not rx.fullmatch(qset or ""):
                continue
            key = f"{qname} [{unit[kind]}]"
            if key in seen:
                continue
            seen.add(key)
            groups[g][key] += v * scale[kind]
            totals[key] += v * scale[kind]
        without += not seen
    return {"selector": selector or DEFAULT_SELECTOR, "group_by": gfields,
            "material_contains": material_contains, "elements": len(els),
            "elements_without_quantities": without,
            "totals": {k: round(v, 3) for k, v in sorted(totals.items())},
            "groups": {g: {"elements": elcount[g], **{k: round(v, 3) for k, v in sorted(q.items())}}
                       for g, q in sorted(groups.items())},
            "note": "Values come from the IFC Qto sets written by the authoring tool "
                    "(no geometric recomputation)."}


def audit(lm, selector=None, required: list[str] | None = None, sample=10) -> dict:
    """Model-quality checks + optional required-information checks."""
    m = lm.model
    els = select(lm, selector)
    issues = []

    def add(check, items, severity="warning", detail=""):
        items = list(items)
        if items:
            issues.append({"check": check, "severity": severity, "count": len(items),
                           "detail": detail,
                           "sample": [{"GlobalId": getattr(e, "GlobalId", None), "class": e.is_a(),
                                       "Name": getattr(e, "Name", None)} for e in items[:sample]]})

    guids = Counter(e.GlobalId for e in m.by_type("IfcRoot"))
    dup = {g for g, n in guids.items() if n > 1}
    add("duplicate_globalid", [e for e in m.by_type("IfcRoot") if e.GlobalId in dup], "error",
        "GlobalId must be unique (breaks BCF, clash tracking and comparisons)")
    physical = [e for e in els if not e.is_a("IfcFeatureElement")]
    add("no_spatial_container", [e for e in physical if uel.get_container(e) is None
                                 and uel.get_aggregate(e) is None], "error",
        "Element not contained in a storey/space and not part of an assembly")
    add("building_element_proxy", [e for e in physical if e.is_a("IfcBuildingElementProxy")],
        "warning", "Generic proxies: export mapping missing in the authoring tool")
    add("no_type", [e for e in physical if uel.get_type(e) is None], "info",
        "Element has no IfcTypeObject")
    add("empty_name", [e for e in physical if not getattr(e, "Name", None)], "warning")
    add("no_material", [e for e in physical if not uel.get_material(e)], "info")
    add("opening_without_host", [o for o in m.by_type("IfcOpeningElement") if not o.VoidsElements],
        "warning")
    for field in required or []:
        add(f"missing:{field}", [e for e in physical if value(e, field, lm.index) in (None, "", [])], "error",
            f"Required information '{field}' is empty or absent")
    errors = sum(i["count"] for i in issues if i["severity"] == "error")
    return {"selector": selector or DEFAULT_SELECTOR, "elements_checked": len(physical),
            "errors": errors, "issues": issues}


def _fingerprint(el, fields, idx: PropertyIndex | None = None) -> dict:
    d = {f: _key(value(el, f, idx)) for f in fields}
    ps = idx.psets(el) if idx is not None else uel.get_psets(el)
    flat = {f"{p}.{k}": _key(v) for p, props in ps.items() for k, v in props.items() if k != "id"}
    d["_psets_hash"] = hashlib.md5(repr(sorted(flat.items())).encode()).hexdigest()
    d["_psets"] = flat
    return d


def compare(lm_a, lm_b, selector=None, fields=None, sample=25) -> dict:
    """Diff two versions of a model by GlobalId."""
    fields = fields or ["class", "Name", "type.Name", "storey.Name"]
    a = {e.GlobalId: e for e in select(lm_a, selector) if hasattr(e, "GlobalId")}
    b = {e.GlobalId: e for e in select(lm_b, selector) if hasattr(e, "GlobalId")}
    added = [g for g in b if g not in a]
    removed = [g for g in a if g not in b]
    changed = []
    prop_changes = Counter()
    for g in a.keys() & b.keys():
        fa, fb = _fingerprint(a[g], fields, lm_a.index), _fingerprint(b[g], fields, lm_b.index)
        diffs = {f: [fa[f], fb[f]] for f in fields if fa[f] != fb[f]}
        if fa["_psets_hash"] != fb["_psets_hash"]:
            pa, pb = fa["_psets"], fb["_psets"]
            pdiff = {k: [pa.get(k), pb.get(k)] for k in pa.keys() | pb.keys() if pa.get(k) != pb.get(k)}
            prop_changes.update(pdiff.keys())
            diffs["properties"] = dict(list(pdiff.items())[:15])
        if diffs:
            changed.append({"GlobalId": g, "class": b[g].is_a(), "Name": getattr(b[g], "Name", None),
                            "changes": diffs})

    def brief(m, gs):
        return [{"GlobalId": g, "class": m[g].is_a(), "Name": getattr(m[g], "Name", None)} for g in gs[:sample]]

    return {"a": lm_a.alias, "b": lm_b.alias, "selector": selector or DEFAULT_SELECTOR,
            "count_a": len(a), "count_b": len(b),
            "added": len(added), "removed": len(removed), "changed": len(changed),
            "unchanged": len(a.keys() & b.keys()) - len(changed),
            "added_by_class": dict(Counter(b[g].is_a() for g in added).most_common()),
            "removed_by_class": dict(Counter(a[g].is_a() for g in removed).most_common()),
            "most_changed_properties": dict(prop_changes.most_common(20)),
            "added_sample": brief(b, added), "removed_sample": brief(a, removed),
            "changed_sample": changed[:sample]}


def spatial_tree(lm) -> dict:
    m = lm.model

    def node(obj, depth):
        n = {"class": obj.is_a(), "name": getattr(obj, "Name", None)}
        if obj.is_a("IfcBuildingStorey"):
            n["elevation_m"] = round((obj.Elevation or 0) * lm.length_scale, 3)
        contained = [e for r in getattr(obj, "ContainsElements", []) for e in r.RelatedElements]
        if contained:
            n["elements"] = len(contained)
            n["by_class"] = dict(Counter(e.is_a() for e in contained).most_common(8))
        children = [c for r in getattr(obj, "IsDecomposedBy", []) for c in r.RelatedObjects
                    if c.is_a("IfcSpatialElement") or c.is_a("IfcSpatialStructureElement")]
        if children and depth < 6:
            spaces = [c for c in children if c.is_a("IfcSpace")]
            others = [c for c in children if not c.is_a("IfcSpace")]
            n["children"] = [node(c, depth + 1) for c in others]
            if spaces:
                n["spaces"] = len(spaces)
        return n

    return {"alias": lm.alias, "tree": [node(p, 0) for p in m.by_type("IfcProject")]}


def export_table(lm, selector, fields, output: str) -> dict:
    fields = fields or DEFAULT_FIELDS
    els = select(lm, selector)
    out = Path(output).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    idx = lm.index

    def cell(e, f):
        v = value(e, f, idx)  # computed once per cell (was up to 3 times)
        return _key(v) if isinstance(v, (list, tuple)) else _jsonable(v)
    rows = [[cell(e, f) for f in fields] for e in els]
    if out.suffix.lower() == ".xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = lm.alias[:31]
        ws.append(fields)
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        wb.save(out)
    elif out.suffix.lower() == ".csv":
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(fields)
            w.writerows(rows)
    else:
        raise IfcEngineError("Output must be .xlsx or .csv")
    return {"output": str(out), "rows": len(rows), "fields": fields}
