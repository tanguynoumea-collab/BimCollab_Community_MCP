"""Geometry layer: tessellation cache (memory + disk), resource guards, metrics and spatial queries.

All coordinates are metres in a per-model LOCAL frame: world = local + GeomSet.origin. Swiss
LV95 coordinates (~2.5e6 m) lose ~0.25 m in float32 and break the manifold kernel, so the
iterator runs with a model offset rounded to the kilometre (same offset for every model of a
project, so meshes of different models share the frame without conversion).

Resource rules (a parallel, unbounded run once froze the user's PC):
  * one geometry job at a time (the MCP server already serialises tools),
  * BIM_ASSISTANT_GEOM_THREADS threads (default min(4, cpu/2)),
  * BIM_ASSISTANT_RAM_MB resident-memory budget (default 4000), checked while tessellating,
  * BIM_ASSISTANT_MAX_ELEMENTS elements per call (default 20000; 5000 above 150 MB of IFC),
  * disk cache in %LOCALAPPDATA%\\bim-assistant\\geomcache (never next to the IFC), 2 GB max.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from bim_assistant import ifc_engine as ie

KERNEL = os.environ.get("BIM_ASSISTANT_GEOM_KERNEL", "hybrid-manifold-opencascade")
FALLBACK_KERNEL = "opencascade"
GEOM_THREADS = int(os.environ.get("BIM_ASSISTANT_GEOM_THREADS", 0)) or max(1, min(4, (os.cpu_count() or 2) // 2))
RAM_BUDGET_MB = int(os.environ.get("BIM_ASSISTANT_RAM_MB", "4000"))
MAX_ELEMENTS = int(os.environ.get("BIM_ASSISTANT_MAX_ELEMENTS", "20000"))
BIG_MODEL_BYTES = 150e6
BIG_MODEL_MAX_ELEMENTS = 5000
CACHE_MAX_MB = int(os.environ.get("BIM_ASSISTANT_GEOMCACHE_MB", "2048"))
CACHE_DIR = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "bim-assistant" / "geomcache"
SKIP_CLASSES = ("IfcOpeningElement", "IfcSpace", "IfcAnnotation", "IfcGrid", "IfcVirtualElement")
VERTICAL_NZ = 0.1     # |nz| below this: vertical face
HORIZONTAL_NZ = 0.9   # |nz| above this: horizontal face

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, str(GEOM_THREADS))


class GeometryError(ie.IfcEngineError):
    pass


# ---------------------------------------------------------------------------
# Resource guards
# ---------------------------------------------------------------------------

def _rss_mb() -> float:
    import psutil
    return psutil.Process().memory_info().rss / 2**20


def check_memory(context: str) -> None:
    import psutil
    avail = psutil.virtual_memory().available / 2**20
    if avail < 1024:
        raise GeometryError(f"{context}: only {avail:.0f} MB of RAM available on this PC; close "
                            "applications or narrow the selection before a geometry job.")
    if _rss_mb() > RAM_BUDGET_MB:
        raise GeometryError(f"{context}: bim-assistant already uses {_rss_mb():.0f} MB "
                            f"(budget {RAM_BUDGET_MB} MB). Close models (ifc_close) or narrow the selection.")


def limit_for(lm) -> int:
    return BIG_MODEL_MAX_ELEMENTS if lm.size > BIG_MODEL_BYTES else MAX_ELEMENTS


def guard_count(lm, n: int, what: str) -> None:
    cap = limit_for(lm)
    if n > cap:
        raise GeometryError(f"{what}: {n} elements selected, limit {cap} for this model "
                            f"({lm.size / 1e6:.0f} MB). Narrow the selector (classes, storey, type) "
                            "or raise BIM_ASSISTANT_MAX_ELEMENTS knowingly.")


# ---------------------------------------------------------------------------
# Meshes
# ---------------------------------------------------------------------------

class Mesh:
    """One element's triangulated Body, local frame (metres, float64)."""
    __slots__ = ("guid", "cls", "v", "f", "lo", "hi", "_tri", "_fcl", "_man")

    def __init__(self, guid: str, cls: str, v: np.ndarray, f: np.ndarray):
        self.guid, self.cls = guid, cls
        self.v = np.ascontiguousarray(v, dtype=np.float64)
        self.f = np.ascontiguousarray(f, dtype=np.int32)
        self.lo, self.hi = self.v.min(0), self.v.max(0)
        self._tri = self._fcl = self._man = None

    def tri(self):
        """(triangles (n,3,3), unit normals (n,3), areas (n,), plane offsets (n,))."""
        if self._tri is None:
            T = self.v[self.f]
            c = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
            l2 = np.linalg.norm(c, axis=1)
            k = l2 > 1e-12
            T, c, l2 = T[k], c[k], l2[k]
            n = c / l2[:, None]
            self._tri = (T, n, 0.5 * l2, np.einsum("ij,ij->i", n, T[:, 0]))
        return self._tri

    def volume(self) -> float:
        T = self.v[self.f]
        return abs(float(np.einsum("ij,ij->i", T[:, 0], np.cross(T[:, 1], T[:, 2])).sum()) / 6.0)

    def fcl(self):
        if self._fcl is None:
            import fcl
            m = fcl.BVHModel()
            m.beginModel(len(self.v), len(self.f))
            m.addSubModel(self.v, self.f)
            m.endModel()
            self._fcl = fcl.CollisionObject(m, fcl.Transform())
        return self._fcl

    def manifold(self):
        """manifold3d solid, or None when the mesh is not a closed 2-manifold."""
        if self._man is None:
            import manifold3d as m3
            try:
                man = m3.Manifold(m3.Mesh(vert_properties=self.v.astype(np.float32),
                                          tri_verts=self.f.astype(np.uint32)))
                self._man = man if man.status() == m3.Error.NoError and not man.is_empty() else False
            except Exception:  # noqa: BLE001
                self._man = False
        return self._man or None


def _origin_for(lm) -> np.ndarray:
    """World position (m) of the local frame: median placement of the elements, rounded to the km."""
    import ifcopenshell.util.placement as upl
    pts = []
    for el in lm.model.by_type("IfcElement")[:400]:
        if getattr(el, "ObjectPlacement", None) is None:
            continue
        try:
            pts.append(upl.get_local_placement(el.ObjectPlacement)[:3, 3])
        except Exception:  # noqa: BLE001
            continue
    if not pts:
        return np.zeros(3)
    med = np.median(np.array(pts), axis=0) * lm.length_scale
    o = np.round(med / 1000.0) * 1000.0
    o[2] = 0.0
    return o


class GeomSet:
    """Meshes of one loaded model, tessellated lazily and cached in memory + on disk."""

    def __init__(self, lm):
        self.lm = lm
        self.origin = _origin_for(lm)
        self.meshes: dict[str, Mesh] = {}
        self.failed: set[str] = set()
        self.kernel = KERNEL
        self._disk_loaded = False
        self.stats: dict = {}

    # -- disk cache --------------------------------------------------------------------------
    def _cache_path(self) -> Path:
        key = f"{self.lm.path}|{self.lm.mtime}|{self.lm.size}|{self.kernel}|body|v1"
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return CACHE_DIR / f"{Path(self.lm.path).stem[:40]}_{h}.npz"

    def _load_disk(self) -> None:
        self._disk_loaded = True
        p = self._cache_path()
        if not p.exists():
            return
        try:
            z = dict(np.load(p, allow_pickle=False))   # dict(): one read; z["V"] on a NpzFile re-reads
        except Exception:  # noqa: BLE001 - corrupt cache: ignore and rebuild
            return
        if not np.allclose(z.get("origin", self.origin), self.origin):
            return
        V, F, vo, fo = z["V"], z["F"], z["vo"], z["fo"]
        for i, (g, c) in enumerate(zip(z["gids"], z["classes"])):
            self.meshes[str(g)] = Mesh(str(g), str(c), V[vo[i]:vo[i + 1]], F[fo[i]:fo[i + 1]])
        self.failed.update(str(g) for g in z.get("failed", []))
        os.utime(p)

    def _save_disk(self) -> None:
        if not self.meshes:
            return
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        ms = list(self.meshes.values())
        vo = np.cumsum([0] + [len(m.v) for m in ms])
        fo = np.cumsum([0] + [len(m.f) for m in ms])
        p = self._cache_path()
        tmp = p.with_suffix(".tmp.npz")
        np.savez(tmp, gids=np.array([m.guid for m in ms]), classes=np.array([m.cls for m in ms]),
                 V=np.concatenate([m.v for m in ms]), F=np.concatenate([m.f for m in ms]),
                 vo=vo, fo=fo, origin=self.origin, failed=np.array(sorted(self.failed), dtype=str))
        os.replace(tmp, p)
        _purge_cache(keep=p)

    # -- tessellation ------------------------------------------------------------------------
    def _run(self, els, kernel: str) -> int:
        import ifcopenshell.geom as geom
        s = geom.settings()
        s.set("use-world-coords", True)
        s.set("context-identifiers", ["Body"])   # the Axis representation is picked otherwise
        s.set("model-offset", tuple(float(x) for x in -self.origin))
        it = geom.iterator(s, self.lm.model, GEOM_THREADS, include=els, geometry_library=kernel)
        n = 0
        if not it.initialize():
            return 0
        while True:
            sh = it.get()
            g = sh.geometry
            f = np.asarray(g.faces, dtype=np.int32).reshape(-1, 3)
            if len(f):
                v = np.asarray(g.verts, dtype=np.float64).reshape(-1, 3)
                self.meshes[sh.guid] = Mesh(sh.guid, self.lm.model.by_guid(sh.guid).is_a(), v, f)
                n += 1
            if n % 200 == 0 and _rss_mb() > RAM_BUDGET_MB:
                raise GeometryError(f"Memory budget reached ({RAM_BUDGET_MB} MB) after {n} elements: "
                                    "narrow the selection (classes / storey) and retry.")
            if not it.next():
                break
        return n

    def ensure(self, els) -> list[Mesh]:
        """Meshes for the given elements (tessellating what is missing). Elements without a
        Body (or that no kernel can mesh) are skipped and listed in self.failed."""
        if not self._disk_loaded:
            self._load_disk()
        els = [e for e in els if getattr(e, "GlobalId", None) and getattr(e, "Representation", None)
               and not e.is_a() in SKIP_CLASSES]
        todo = [e for e in els if e.GlobalId not in self.meshes and e.GlobalId not in self.failed]
        if todo:
            check_memory("Tessellation")
            t = time.perf_counter()
            self._run(todo, self.kernel)
            miss = [e for e in todo if e.GlobalId not in self.meshes]
            if miss and self.kernel != FALLBACK_KERNEL:   # empty/missing shapes: exact kernel per element
                self._run(miss, FALLBACK_KERNEL)
            self.failed.update(e.GlobalId for e in todo if e.GlobalId not in self.meshes)
            self.stats = {"tessellated": len(todo), "seconds": round(time.perf_counter() - t, 2),
                          "kernel": self.kernel, "threads": GEOM_THREADS, "rss_mb": round(_rss_mb())}
            self._save_disk()
        return [self.meshes[e.GlobalId] for e in els if e.GlobalId in self.meshes]


def _purge_cache(keep: Path | None = None) -> None:
    files = sorted(CACHE_DIR.glob("*.npz"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for p in files:
        if total <= CACHE_MAX_MB * 2**20:
            break
        if p == keep:
            continue
        total -= p.stat().st_size
        p.unlink(missing_ok=True)


def geomset(lm) -> GeomSet:
    gs = getattr(lm, "geom", None)
    if gs is None:
        gs = lm.geom = GeomSet(lm)
    return gs


def meshes_for(lm, selector: str | None, what: str) -> tuple[list, list[Mesh]]:
    els = ie.select(lm, selector)
    guard_count(lm, len(els), what)
    return els, geomset(lm).ensure(els)


def shift(lm_from, lm_to) -> np.ndarray:
    """Vector to add to lm_from's local coordinates to express them in lm_to's frame."""
    return geomset(lm_from).origin - geomset(lm_to).origin


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _qto_volume(el, idx, lm) -> float | None:
    best = None
    for name, props in idx.psets(el).items():
        if name not in idx.qto_names:
            continue
        for q in ("NetVolume", "GrossVolume"):
            v = props.get(q)
            if isinstance(v, (int, float)) and v > 0:
                if q == "NetVolume":
                    return v * lm.volume_scale
                best = best or v * lm.volume_scale
    return best


def _footprint(m: Mesh) -> float:
    import shapely
    from shapely.geometry import Polygon
    T, n, a, _ = m.tri()
    up = T[np.abs(n[:, 2]) > 1e-6]
    if not len(up):
        return 0.0
    return float(shapely.union_all([Polygon(t[:, :2]) for t in up]).area)


METRICS = ("volume", "area", "lateral_area", "top_area", "bottom_area", "footprint", "height", "bbox")


def element_metrics(m: Mesh, metrics, origin) -> dict:
    T, n, a, _ = m.tri()
    out = {}
    if "volume" in metrics:
        out["volume_m3"] = m.volume()
    if "area" in metrics:
        out["area_m2"] = float(a.sum())
    if "lateral_area" in metrics:
        out["lateral_area_m2"] = float(a[np.abs(n[:, 2]) < VERTICAL_NZ].sum())
    if "top_area" in metrics:
        out["top_area_m2"] = float(a[n[:, 2] > HORIZONTAL_NZ].sum())
    if "bottom_area" in metrics:
        out["bottom_area_m2"] = float(a[n[:, 2] < -HORIZONTAL_NZ].sum())
    if "footprint" in metrics:
        out["footprint_m2"] = _footprint(m)
    if "height" in metrics:
        out["z_min"] = float(m.lo[2] + origin[2])
        out["z_max"] = float(m.hi[2] + origin[2])
        out["height_m"] = float(m.hi[2] - m.lo[2])
    if "bbox" in metrics:
        out["bbox_min"] = [round(float(x), 3) for x in m.lo + origin]
        out["bbox_max"] = [round(float(x), 3) for x in m.hi + origin]
    return out


def geometry(lm, selector=None, metrics=None, group_by="class", compare_qto=False,
             qto_tolerance=0.05, limit=50) -> dict:
    """Measured geometry (Body meshes) per group, with optional check against the IFC Qto."""
    metrics = [m for m in (metrics or ["volume", "area", "lateral_area", "height"]) if m in METRICS]
    els, ms = meshes_for(lm, selector, "ifc_geometry")
    gs = geomset(lm)
    idx = lm.index
    by_guid = {m.guid: m for m in ms}
    gfields = group_by if isinstance(group_by, list) else ([group_by] if group_by else [])
    groups: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    counts = defaultdict(int)
    rows, mism = [], []
    checked = 0
    for el in els:
        m = by_guid.get(el.GlobalId)
        if m is None:
            continue
        em = element_metrics(m, metrics, gs.origin)
        g = " | ".join(ie._key(ie.value(el, f, idx)) for f in gfields) if gfields else "total"
        counts[g] += 1
        for k, v in em.items():
            if isinstance(v, float) and k.endswith(("_m2", "_m3")):
                groups[g][k] += v
        if compare_qto and "volume_m3" in em:
            q = _qto_volume(el, idx, lm)
            if q is not None and em["volume_m3"] > 1e-4:
                checked += 1
                r = q / em["volume_m3"]
                if abs(r - 1) > qto_tolerance:
                    mism.append({"guid": el.GlobalId, "class": el.is_a(), "name": el.Name,
                                 "type": ie._key(ie.value(el, "type.Name", idx)),
                                 "geometry_m3": round(em["volume_m3"], 4), "qto_m3": round(q, 4),
                                 "ratio": round(r, 3)})
        if len(rows) < limit:
            rows.append({"guid": el.GlobalId, "class": el.is_a(), "name": el.Name, "group": g,
                         **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in em.items()}})
    totals = defaultdict(float)
    for q in groups.values():
        for k, v in q.items():
            totals[k] += v
    out = {"selector": selector or ie.DEFAULT_SELECTOR, "elements": len(els), "measured": len(ms),
           "without_geometry": len(els) - len(ms), "group_by": gfields,
           "totals": {k: round(v, 3) for k, v in sorted(totals.items())},
           "groups": {g: {"elements": counts[g], **{k: round(v, 3) for k, v in sorted(q.items())}}
                      for g, q in sorted(groups.items())},
           "elements_sample": rows, "tessellation": gs.stats or "cache",
           "note": "Measured on the Body meshes (m, m², m³). lateral_area = vertical faces, both "
                   "sides; top/bottom = horizontal faces."}
    if compare_qto:
        mism.sort(key=lambda d: -abs(math.log(max(d["ratio"], 1e-9))))
        out["qto_check"] = {"compared": checked, "mismatches": len(mism), "tolerance": qto_tolerance,
                            "worst": mism[:limit]}
    return out


# ---------------------------------------------------------------------------
# Distances
# ---------------------------------------------------------------------------

def pair_distance(ma: Mesh, mb: Mesh, shift_b=None) -> dict:
    """Minimum distance between two meshes; overlap volume/depth when they interpenetrate."""
    import fcl
    ob = mb.fcl()
    if shift_b is not None and np.any(shift_b):
        ob = fcl.CollisionObject(ob.collision_geometry, fcl.Transform(np.asarray(shift_b, float)))
    req = fcl.DistanceRequest(enable_nearest_points=True)
    res = fcl.DistanceResult()
    d = fcl.distance(ma.fcl(), ob, req, res)
    out = {"distance_m": round(max(float(d), 0.0), 4)}
    if d > 1e-6:
        out["status"] = "gap"
        out["points"] = [np.round(np.asarray(p, float), 4).tolist() for p in res.nearest_points]
        return out
    ov = overlap(ma, mb, shift_b)
    out.update(ov)
    return out


def overlap(ma: Mesh, mb: Mesh, shift_b=None) -> dict:
    """Exact intersection volume (manifold3d) of two touching/colliding meshes."""
    a, b = ma.manifold(), mb.manifold()
    if a is None or b is None:
        return {"status": "contact_or_overlap", "volume_m3": None, "depth_m": None,
                "note": "non-manifold mesh: volume not computable"}
    if shift_b is not None and np.any(shift_b):
        b = b.translate(tuple(float(x) for x in shift_b))
    x = a ^ b
    vol = float(x.volume())
    if vol <= 1e-9:
        return {"status": "contact", "volume_m3": 0.0, "depth_m": 0.0}
    bb = np.asarray(x.bounding_box(), float)
    lo, hi = bb[:3], bb[3:]
    return {"status": "overlap", "volume_m3": round(vol, 6), "depth_m": round(float((hi - lo).min()), 4),
            "center_local": ((lo + hi) / 2).tolist()}


def _boxes(ms: list[Mesh], shift_v=None):
    lo = np.array([m.lo for m in ms]) if ms else np.zeros((0, 3))
    hi = np.array([m.hi for m in ms]) if ms else np.zeros((0, 3))
    if shift_v is not None:
        lo, hi = lo + shift_v, hi + shift_v
    return lo, hi


def candidate_pairs(A: list[Mesh], B: list[Mesh], margin: float, shift_b=None, same=False):
    """Bounding-box broad phase (rtree). Yields (i, j) with boxes closer than margin."""
    import rtree
    loA, hiA = _boxes(A)
    loB, hiB = _boxes(B, shift_b)
    if not len(A) or not len(B):
        return []
    p = rtree.index.Property(dimension=3)
    idx = rtree.index.Index(((i, (*(loA[i] - margin), *(hiA[i] + margin)), None) for i in range(len(A))),
                            properties=p)
    out = []
    for j in range(len(B)):
        for i in idx.intersection((*loB[j], *hiB[j])):
            if same and (i >= j or A[i].guid == B[j].guid):
                continue
            if not same and A[i].guid == B[j].guid:
                continue
            out.append((i, j))
    return out


def distance(lm_a, guid_a: str, guid_b: str, lm_b=None) -> dict:
    lm_b = lm_b or lm_a
    ma = _one(lm_a, guid_a)
    mb = _one(lm_b, guid_b)
    sh = shift(lm_b, lm_a)
    r = pair_distance(ma, mb, sh)
    o = geomset(lm_a).origin
    if "points" in r:
        r["points"] = [(np.asarray(p) + o).round(3).tolist() for p in r["points"]]
    if "center_local" in r:
        r["center"] = (np.asarray(r.pop("center_local")) + o).round(3).tolist()
    r.update({"a": _desc(lm_a, guid_a), "b": _desc(lm_b, guid_b)})
    return r


def nearby(lm, guid: str, within_m=0.5, selector=None, other=None, limit=50) -> dict:
    other = other or lm
    m = _one(lm, guid)
    _, cands = meshes_for(other, selector, "ifc_nearby")
    sh = shift(other, lm)
    pairs = candidate_pairs([m], cands, within_m, sh)
    res = []
    for _, j in pairs:
        mb = cands[j]
        if mb.guid == guid:
            continue
        d = pair_distance(m, mb, sh)
        if d["distance_m"] <= within_m:
            res.append({**_desc(other, mb.guid), "distance_m": d["distance_m"], "status": d["status"],
                        **({"overlap_m3": d["volume_m3"]} if d.get("volume_m3") else {})})
    res.sort(key=lambda r: r["distance_m"])
    return {"element": _desc(lm, guid), "within_m": within_m, "found": len(res), "nearby": res[:limit]}


def _one(lm, guid: str) -> Mesh:
    try:
        el = lm.model.by_guid(guid)
    except Exception as exc:  # noqa: BLE001
        raise GeometryError(f"GlobalId {guid} not found in {lm.alias}") from exc
    ms = geomset(lm).ensure([el])
    if not ms:
        raise GeometryError(f"{el.is_a()} {guid} has no Body geometry")
    return ms[0]


def _desc(lm, guid: str) -> dict:
    el = lm.model.by_guid(guid)
    t = ie.value(el, "type.Name", lm.index)
    return {"model": lm.alias, "guid": guid, "class": el.is_a(), "name": el.Name,
            "type": t, "storey": ie.value(el, "storey.Name", lm.index)}


# ---------------------------------------------------------------------------
# Contact areas (paintable / net lateral surfaces)
# ---------------------------------------------------------------------------

def _plane_basis(n):
    u = np.cross(n, [0.0, 0.0, 1.0])
    if np.linalg.norm(u) < 1e-6:
        u = np.cross(n, [1.0, 0.0, 0.0])
    u /= np.linalg.norm(u)
    return u, np.cross(n, u)


def _union2d(tris, u, w):
    import shapely
    from shapely.geometry import Polygon
    polys = [Polygon(np.c_[t @ u, t @ w]) for t in tris]
    polys = [p if p.is_valid else p.buffer(0) for p in polys if p.area > 1e-10]
    return shapely.union_all(polys) if polys else None


def contact_area(lm, selector_a, selector_b=None, other=None, group_by="type.Name", tolerance=0.01,
                 floor_selector=None, ceiling_selector=None, limit=50) -> dict:
    """Net lateral (vertical-face) area of the A elements, minus faces in contact with B elements
    (coplanar within `tolerance`), minus the band hidden below floors / above ceilings."""
    import shapely
    from shapely.geometry import box as sbox
    other = other or lm
    els_a, A = meshes_for(lm, selector_a, "ifc_contact_area (A)")
    _, B = meshes_for(other, selector_b or "IfcElement", "ifc_contact_area (B)")
    sh = shift(other, lm)
    floors = meshes_for(lm, floor_selector, "floors")[1] if floor_selector else []
    ceilings = meshes_for(lm, ceiling_selector, "ceilings")[1] if ceiling_selector else []
    pairs = defaultdict(list)
    for i, j in candidate_pairs(A, B, tolerance, sh):
        if A[i].guid != B[j].guid:
            pairs[i].append(j)
    fl_lo, fl_hi = _boxes(floors)
    ce_lo, ce_hi = _boxes(ceilings)
    idx = lm.index
    by_guid = {e.GlobalId: e for e in els_a}
    gfields = group_by if isinstance(group_by, list) else ([group_by] if group_by else [])
    groups = defaultdict(lambda: defaultdict(float))
    counts = defaultdict(int)
    rows = []
    for i, m in enumerate(A):
        T, n, a, d = m.tri()
        vert = np.abs(n[:, 2]) < VERTICAL_NZ
        zlo, zhi = m.lo[2], m.hi[2]
        xy_lo, xy_hi = m.lo[:2] - 0.05, m.hi[:2] + 0.05
        if len(floors):   # top of floors standing against / inside the element's footprint
            k = np.all(fl_lo[:, :2] <= xy_hi, 1) & np.all(fl_hi[:, :2] >= xy_lo, 1) & \
                (fl_hi[:, 2] > zlo + 1e-3) & (fl_hi[:, 2] < zhi)
            if k.any():
                zlo = max(zlo, float(fl_hi[k, 2].max()))
        if len(ceilings):
            k = np.all(ce_lo[:, :2] <= xy_hi, 1) & np.all(ce_hi[:, :2] >= xy_lo, 1) & \
                (ce_lo[:, 2] < zhi - 1e-3) & (ce_lo[:, 2] > zlo)
            if k.any():
                zhi = min(zhi, float(ce_lo[k, 2].min()))
        gross = float(a[vert].sum())
        band_area = contact = 0.0
        planes = defaultdict(list)
        for t in np.nonzero(vert)[0]:
            planes[(tuple(np.round(n[t], 3)), round(float(d[t]), 2))].append(t)
        for (nk, _), ts in planes.items():
            nv = np.array(nk) / np.linalg.norm(nk)
            u, w = _plane_basis(nv)
            pa = _union2d(T[ts], u, w)
            if pa is None:
                continue
            wz = w[2] if abs(w[2]) > 1e-9 else 1.0
            b0, b1 = sorted((wz * zlo, wz * zhi))
            ub = pa.bounds
            pa = pa.intersection(sbox(ub[0] - 1, b0, ub[2] + 1, b1))
            band_area += pa.area
            hits = []
            dplane = float(np.mean(d[ts]))
            for j in pairs.get(i, []):
                TB, nb, _, db = B[j].tri()
                TBs = TB + sh
                dbs = db + nb @ sh
                k = (nb @ nv < -0.999) & (np.abs(dbs + dplane) < tolerance)
                if k.any():
                    pb = _union2d(TBs[k], u, w)
                    if pb is not None:
                        hits.append(pb)
            if hits:
                contact += pa.intersection(shapely.union_all(hits)).area
        net = max(0.0, band_area - contact)
        el = by_guid[m.guid]
        g = " | ".join(ie._key(ie.value(el, f, idx)) for f in gfields) if gfields else "total"
        counts[g] += 1
        q = groups[g]
        q["lateral_gross_m2"] += gross
        q["hidden_by_floor_ceiling_m2"] += gross - band_area
        q["contact_m2"] += contact
        q["net_m2"] += net
        if len(rows) < limit:
            rows.append({"guid": m.guid, "name": el.Name, "group": g, "lateral_gross_m2": round(gross, 3),
                         "net_m2": round(net, 3), "contact_m2": round(contact, 3),
                         "band_z": [round(zlo + geomset(lm).origin[2], 3), round(zhi + geomset(lm).origin[2], 3)]})
    tot = defaultdict(float)
    for q in groups.values():
        for k, v in q.items():
            tot[k] += v
    return {"selector_a": selector_a, "selector_b": selector_b or "IfcElement", "elements": len(A),
            "tolerance_m": tolerance, "totals": {k: round(v, 3) for k, v in tot.items()},
            "groups": {g: {"elements": counts[g], **{k: round(v, 3) for k, v in q.items()}}
                       for g, q in sorted(groups.items())},
            "elements_sample": rows,
            "note": "Vertical faces of A, both sides. contact = faces of A covered by a coplanar, "
                    "opposite face of B (junctions with walls, linings, columns...). The band below "
                    "the top of floor_selector elements and above the underside of ceiling_selector "
                    "elements is excluded."}


# ---------------------------------------------------------------------------
# Vertical rays (clear heights)
# ---------------------------------------------------------------------------

def _ray_z(points: np.ndarray, m: Mesh, up: bool, shift_v=None) -> np.ndarray:
    """For vertical rays from points (k,3), z of the first hit on mesh m (nan if none)."""
    T, n, _, _ = m.tri()
    if shift_v is not None:
        T = T + shift_v
    keep = np.abs(n[:, 2]) > 1e-6
    T = T[keep]
    out = np.full(len(points), np.nan)
    if not len(T):
        return out
    x0, y0 = T[:, 0, 0], T[:, 0, 1]
    e1, e2 = T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]
    det = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    ok = np.abs(det) > 1e-12
    T, x0, y0, e1, e2, det = T[ok], x0[ok], y0[ok], e1[ok], e2[ok], det[ok]
    for s in range(0, len(points), 256):
        P = points[s:s + 256]
        px = P[:, 0:1] - x0
        py = P[:, 1:2] - y0
        a = (px * e2[:, 1] - py * e2[:, 0]) / det
        b = (e1[:, 0] * py - e1[:, 1] * px) / det
        inside = (a >= -1e-9) & (b >= -1e-9) & (a + b <= 1 + 1e-9)
        z = T[:, 0, 2] + a * e1[:, 2] + b * e2[:, 2]
        z0 = P[:, 2:3]
        valid = inside & ((z > z0 + 1e-3) if up else (z < z0 - 1e-3))
        zz = np.where(valid, z, np.inf if up else -np.inf)
        best = zz.min(1) if up else zz.max(1)
        out[s:s + 256] = np.where(np.isfinite(best), best, np.nan)
    return out


def clearance(lm, floor_selector="IfcSlab", obstacle_selector="IfcElement", grid_m=1.0,
              max_rays=20000, group_by="storey.Name", limit=20, other=None) -> dict:
    """Clear height above the top faces of the floor elements up to the first obstacle above
    (vertical rays on a grid)."""
    import shapely
    from shapely.geometry import Polygon
    other = other or lm
    fl_els, floors = meshes_for(lm, floor_selector, "ifc_clearance (floors)")
    _, obs = meshes_for(other, obstacle_selector, "ifc_clearance (obstacles)")
    sh = shift(other, lm)
    olo, ohi = _boxes(obs, sh)
    o = geomset(lm).origin
    idx = lm.index
    by_guid = {e.GlobalId: e for e in fl_els}
    gfields = group_by if isinstance(group_by, list) else ([group_by] if group_by else [])
    samples = []   # (group, floor guid, x, y, z0, clear, obstacle guid)
    budget = max_rays
    for m in floors:
        if budget <= 0:
            break
        T, n, _, _ = m.tri()
        top = T[n[:, 2] > HORIZONTAL_NZ]
        if not len(top):
            continue
        poly = shapely.union_all([Polygon(t[:, :2]) for t in top])
        x0, y0, x1, y1 = poly.bounds
        gx = np.arange(x0 + grid_m / 2, x1, grid_m)
        gy = np.arange(y0 + grid_m / 2, y1, grid_m)
        if not len(gx) or not len(gy):
            continue
        X, Y = np.meshgrid(gx, gy)
        X, Y = X.ravel(), Y.ravel()
        k = shapely.contains_xy(poly, X, Y)
        X, Y = X[k][:budget], Y[k][:budget]
        if not len(X):
            continue
        budget -= len(X)
        P = np.c_[X, Y, np.full(len(X), m.hi[2] + 1.0)]
        z0 = _ray_z(P, m, up=False)
        P[:, 2] = z0
        okp = ~np.isnan(z0)
        P = P[okp]
        if not len(P):
            continue
        best = np.full(len(P), np.inf)
        who = np.full(len(P), -1)
        cand = np.nonzero((olo[:, 0] <= P[:, 0].max()) & (ohi[:, 0] >= P[:, 0].min()) &
                          (olo[:, 1] <= P[:, 1].max()) & (ohi[:, 1] >= P[:, 1].min()) &
                          (ohi[:, 2] > P[:, 2].min() + 1e-3))[0]
        for j in cand:
            if obs[j].guid == m.guid:
                continue
            inb = (P[:, 0] >= olo[j, 0]) & (P[:, 0] <= ohi[j, 0]) & (P[:, 1] >= olo[j, 1]) & (P[:, 1] <= ohi[j, 1])
            if not inb.any():
                continue
            z = _ray_z(P[inb], obs[j], up=True, shift_v=sh)
            sel = np.nonzero(inb)[0]
            better = ~np.isnan(z) & (z < best[sel])
            best[sel[better]] = z[better]
            who[sel[better]] = j
        el = by_guid[m.guid]
        g = " | ".join(ie._key(ie.value(el, f, idx)) for f in gfields) if gfields else "total"
        for p, b, w in zip(P, best, who):
            samples.append((g, m.guid, p[0], p[1], p[2], (b - p[2]) if np.isfinite(b) else None,
                            obs[w].guid if w >= 0 else None))
    hits = [s for s in samples if s[5] is not None]
    groups = defaultdict(list)
    for s in hits:
        groups[s[0]].append(s[5])

    def stats(v):
        v = np.asarray(v)
        return {"rays": len(v), "min_m": round(float(v.min()), 3), "p05_m": round(float(np.percentile(v, 5)), 3),
                "median_m": round(float(np.median(v)), 3), "max_m": round(float(v.max()), 3)}

    worst = sorted(hits, key=lambda s: s[5])[:limit]
    return {"floor_selector": floor_selector, "obstacle_selector": obstacle_selector, "grid_m": grid_m,
            "rays": len(samples), "rays_without_obstacle": len(samples) - len(hits),
            "truncated": budget <= 0,
            "overall": stats([s[5] for s in hits]) if hits else None,
            "groups": {g: stats(v) for g, v in sorted(groups.items())},
            "lowest": [{"group": s[0], "floor": s[1], "xyz": [round(s[2] + o[0], 3), round(s[3] + o[1], 3),
                                                             round(s[4] + o[2], 3)],
                        "clear_m": round(s[5], 3), "obstacle": _desc(other, s[6])} for s in worst]}
