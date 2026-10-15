"""Clash detection on the cached Body meshes (geometry.py) and BCF 2.1 report.

Engine (research axis 1): rtree bounding-box broad phase -> python-fcl triangle collision ->
manifold3d exact intersection volume/depth. Unlike IfcOpenShell's tree collision it also finds
full containments (a sleeve inside a slab, a duct inside a shaft wall). Distances in metres.

Modes:
  hard       interpenetration deeper than tolerance_m (and bigger than min_volume_m3)
  clearance  elements closer than clearance_m (interpenetrations and contacts included)
  touch      any contact or interpenetration
  duplicate  near-identical elements (intersection >= 90 % of the larger volume)
"""

from __future__ import annotations

import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from bim_assistant import geometry as geo
from bim_assistant import ifc_engine as ie

MODES = ("hard", "clearance", "touch", "duplicate")
GROUPINGS = ("none", "element_a", "element_b", "storey", "types")
MAX_PAIRS = 200_000
_NS = uuid.UUID("6f1c3a52-2b0e-4b55-9a57-6a0b2c1f5e10")


def _contains(a_lo, a_hi, b_lo, b_hi) -> bool:
    return bool((np.all(a_lo <= b_lo) and np.all(b_hi <= a_hi)) or (np.all(b_lo <= a_lo) and np.all(a_hi <= b_hi)))


def detect(lm_a, selector_a: str, lm_b=None, selector_b: str | None = None, mode: str = "hard",
           tolerance_m: float = 0.002, clearance_m: float = 0.05, min_volume_m3: float = 0.0,
           ignore_same_type: bool = False) -> dict:
    """Raw clash list between the A and B selections (B defaults to A: self-clash)."""
    import fcl
    if mode not in MODES:
        raise geo.GeometryError(f"mode must be one of {', '.join(MODES)}")
    t0 = time.perf_counter()
    lm_b = lm_b or lm_a
    selector_b = selector_b or selector_a
    _, A = geo.meshes_for(lm_a, selector_a, "ifc_clash (A)")
    _, B = geo.meshes_for(lm_b, selector_b, "ifc_clash (B)")
    t_mesh = time.perf_counter() - t0
    same = lm_b is lm_a and selector_b == selector_a
    sh = geo.shift(lm_b, lm_a)
    shifted = bool(np.any(sh))
    margin = clearance_m if mode == "clearance" else 0.0
    pairs = geo.candidate_pairs(A, B, margin, sh, same)
    truncated = len(pairs) > MAX_PAIRS
    if truncated:
        pairs = pairs[:MAX_PAIRS]
    idx_a, idx_b = lm_a.index, lm_b.index
    tname = {}

    def type_name(lm, guid):
        k = (lm.alias, guid)
        if k not in tname:
            tname[k] = ie.value(lm.model.by_guid(guid), "type.Name", lm.index)
        return tname[k]

    seen = set()
    ob_cache = {}
    req = fcl.CollisionRequest()
    out = []
    for i, j in pairs:
        ma, mb = A[i], B[j]
        if same:
            key = frozenset((ma.guid, mb.guid))
            if key in seen:
                continue
            seen.add(key)
        if ignore_same_type and type_name(lm_a, ma.guid) == type_name(lm_b, mb.guid):
            continue
        ob = ob_cache.get(j)
        if ob is None:
            ob = mb.fcl()
            if shifted:
                ob = fcl.CollisionObject(ob.collision_geometry, fcl.Transform(sh))
            ob_cache[j] = ob
        hit = fcl.collide(ma.fcl(), ob, req, fcl.CollisionResult()) > 0
        inside = not hit and _contains(ma.lo, ma.hi, mb.lo + sh, mb.hi + sh)
        rec = None
        if hit or inside:
            ov = geo.overlap(ma, mb, sh if shifted else None)
            st = ov["status"]
            vol, depth = ov.get("volume_m3"), ov.get("depth_m")
            if st == "contact_or_overlap" and not hit:
                continue                       # non-manifold + boxes only nested: undecidable, skip
            rec = {"status": st, "volume_m3": vol, "depth_m": depth, "distance_m": 0.0,
                   "center_local": ov.get("center_local")}
            if mode == "hard":
                if st == "contact" or (st == "overlap" and (depth < tolerance_m or vol < min_volume_m3)):
                    rec = None
            elif mode == "duplicate":
                big = max(ma.volume(), mb.volume())
                if st != "overlap" or big <= 0 or vol < 0.9 * big:
                    rec = None
            elif mode == "touch" and st == "contact_or_overlap":
                rec["status"] = "contact"
        elif mode == "clearance":
            d = geo.pair_distance(ma, mb, sh if shifted else None)
            if d["distance_m"] < clearance_m:
                p = np.asarray(d.get("points", [[0, 0, 0], [0, 0, 0]]), float)
                rec = {"status": "too_close", "volume_m3": 0.0, "depth_m": 0.0,
                       "distance_m": d["distance_m"], "center_local": p.mean(0).tolist()}
        if rec is None:
            continue
        if rec.get("center_local") is None:
            lo = np.maximum(ma.lo, mb.lo + sh)
            hi = np.minimum(ma.hi, mb.hi + sh)
            rec["center_local"] = ((lo + hi) / 2).tolist()
        rec.update(a=ma.guid, b=mb.guid, ia=i, ib=j)
        out.append(rec)
    return {"A": A, "B": B, "shift": sh, "clashes": out, "pairs_tested": len(pairs), "truncated": truncated,
            "seconds_mesh": round(t_mesh, 2), "seconds_total": round(time.perf_counter() - t0, 2),
            "same": same}


def _group_key(c, how, da, db):
    if how == "element_a":
        return c["a"]
    if how == "element_b":
        return c["b"]
    if how == "storey":
        return da.get("storey") or "(sans étage)"
    if how == "types":
        return f"{da.get('type') or da['class']} / {db.get('type') or db['class']}"
    return f"{c['a']}|{c['b']}"


def _label(d) -> str:
    return f"{d['class']} {d.get('type') or d.get('name') or ''}".strip()


def clash(lm_a, selector_a: str, lm_b=None, selector_b: str | None = None, mode: str = "hard",
          tolerance_m: float = 0.002, clearance_m: float = 0.05, min_volume_m3: float = 0.0,
          ignore_same_type: bool = False, group_by: str = "none", limit: int = 100,
          bcf_output: str | None = None, snapshots: bool = True, rule_name: str | None = None,
          max_topics: int = 300, author: str | None = None) -> dict:
    """Detect, group, summarise; optionally write a BCF 2.1 (one topic per group, viewpoint +
    rendered snapshot)."""
    if group_by not in GROUPINGS:
        raise geo.GeometryError(f"group_by must be one of {', '.join(GROUPINGS)}")
    lm_b = lm_b or lm_a
    r = detect(lm_a, selector_a, lm_b, selector_b, mode, tolerance_m, clearance_m, min_volume_m3,
               ignore_same_type)
    origin = geo.geomset(lm_a).origin
    descs = {}

    def desc(lm, guid):
        k = (lm.alias, guid)
        if k not in descs:
            descs[k] = geo._desc(lm, guid)
        return descs[k]

    groups = defaultdict(list)
    for c in r["clashes"]:
        da, db = desc(lm_a, c["a"]), desc(lm_b, c["b"])
        c["center"] = (np.asarray(c["center_local"]) + origin).round(3).tolist()
        groups[_group_key(c, group_by, da, db)].append(c)

    def severity(c):
        return (c["depth_m"] or 0.0, c["volume_m3"] or 0.0, -c["distance_m"])

    ordered = sorted(groups.items(), key=lambda kv: max(severity(c) for c in kv[1]), reverse=True)
    by_status = Counter(c["status"] for c in r["clashes"])
    by_types = Counter(f"{_label(desc(lm_a, c['a']))} × {_label(desc(lm_b, c['b']))}" for c in r["clashes"])
    rule = rule_name or f"{mode}: {selector_a} × {selector_b or selector_a}"

    def row(c):
        return {"a": desc(lm_a, c["a"]), "b": desc(lm_b, c["b"]), "status": c["status"],
                "depth_m": c["depth_m"], "volume_m3": c["volume_m3"], "distance_m": c["distance_m"],
                "center": c["center"]}

    out = {"rule": rule, "mode": mode, "model_a": lm_a.alias, "model_b": lm_b.alias,
           "elements_a": len(r["A"]), "elements_b": len(r["B"]), "pairs_tested": r["pairs_tested"],
           "truncated": r["truncated"], "clashes": len(r["clashes"]), "groups": len(groups),
           "by_status": dict(by_status), "by_types": dict(by_types.most_common(20)),
           "seconds": {"meshes": r["seconds_mesh"], "total": r["seconds_total"]},
           "results": [{"group": k, "count": len(v), "worst": row(max(v, key=severity)),
                        "clashes": [row(c) for c in sorted(v, key=severity, reverse=True)[:5]]}
                       for k, v in ordered[:limit]],
           "units": "metres (coordinates: world / project CRS)",
           "tolerances": {"tolerance_m": tolerance_m, "clearance_m": clearance_m, "min_volume_m3": min_volume_m3}}
    if bcf_output:
        out["bcf"] = write_bcf(bcf_output, ordered[:max_topics], r, lm_a, lm_b, desc, rule, mode,
                               snapshots, author)
        if len(ordered) > max_topics:
            out["bcf"]["note"] = f"{len(ordered)} groups, only the {max_topics} worst were written"
    return out


def write_bcf(path, ordered_groups, r, lm_a, lm_b, desc, rule, mode, snapshots, author):
    from cli_anything.bczoom.core import bcf
    from bim_assistant import snapshot as snap
    from bim_assistant import zoom_bridge as zb
    author = author or zb._author()
    origin = geo.geomset(lm_a).origin
    renderer = None
    snap_note = None
    if snapshots:
        if snap.available():
            renderer = snap.Renderer()
        else:
            snap_note = "pyvista not installed: topics without snapshot (pip install bim-assistant[render])"
    topics = []
    t0 = time.perf_counter()
    try:
        for n, (key, cl) in enumerate(ordered_groups, 1):
            c = max(cl, key=lambda c: (c["depth_m"] or 0.0, c["volume_m3"] or 0.0, -c["distance_m"]))
            da, db = desc(lm_a, c["a"]), desc(lm_b, c["b"])
            ma, mb = r["A"][c["ia"]], r["B"][c["ib"]]
            scene = snap.build_scene(ma, mb, r["shift"], c["center_local"],
                                     context=snap.context_meshes([(lm_a, None), (lm_b, r["shift"])],
                                                                 c["center_local"], exclude={ma.guid, mb.guid}))
            cam = scene.camera
            png = renderer.render(scene, f"{_label(da)}\n{_label(db)}") if renderer else None
            guids_a = sorted({x["a"] for x in cl})
            guids_b = sorted({x["b"] for x in cl})
            vp = bcf.Viewpoint(
                camera=bcf.Camera(tuple(cam.position + scene.origin + origin), tuple(cam.direction),
                                  tuple(cam.up), cam.fov_deg),
                selected=guids_a + guids_b,
                colors={"FF0000": guids_a, "0000FF": guids_b},
                snapshot_png=png)
            dist = (f"profondeur {c['depth_m'] * 100:.1f} cm, volume {c['volume_m3'] * 1e6:.0f} cm³"
                    if c["status"] == "overlap" else
                    f"distance {c['distance_m'] * 100:.1f} cm" if c["status"] == "too_close" else c["status"])
            title = f"{_label(da)} / {_label(db)}"[:200]
            descr = (f"Règle : {rule}\nA : {da['class']} « {da.get('name')} » ({da['model']}) {da['guid']}\n"
                     f"B : {db['class']} « {db.get('name')} » ({db['model']}) {db['guid']}\n"
                     f"Étage : {da.get('storey') or '-'}\nPire cas : {dist}\n"
                     f"Point : {c['center']}\nConflits dans ce groupe : {len(cl)}")
            labels = ["Clash", mode] + [x for x in {da.get("storey")} if x]
            topics.append(bcf.Topic(
                title=title, author=author, description=descr, topic_type="Clash", status="Active",
                priority="Critical" if (c["depth_m"] or 0) > 0.05 else "Major" if c["status"] == "overlap" else "Normal",
                labels=labels, index=n, viewpoints=[vp],
                guid=str(uuid.uuid5(_NS, f"{rule}|{key}"))))
    finally:
        if renderer:
            renderer.close()
    files = [{"filename": Path(lm.path).name} for lm in ({lm_a.alias: lm_a, lm_b.alias: lm_b}).values()]
    p = bcf.write_bcf21(path, topics, project_name=rule[:80], ifc_files=files)
    res = {"file": str(p), "version": "2.1", "topics": len(topics),
           "snapshots": sum(1 for t in topics if t.viewpoints[0].snapshot_png),
           "seconds": round(time.perf_counter() - t0, 2)}
    if snap_note:
        res["note"] = snap_note
    return res
