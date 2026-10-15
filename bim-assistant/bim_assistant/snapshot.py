"""Off-screen clash snapshots (PyVista/VTK) and the matching BCF camera (research axis 3).

The scene is moved to a frame centred on the clash before going to the GPU (float32), and the
camera is computed there, then converted back to world coordinates for the BCF viewpoint, so the
image and the view Zoom restores from the viewpoint match.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass, field

import numpy as np

from bim_assistant import geometry as geo

RED = (0.85, 0.10, 0.10)
BLUE = (0.10, 0.30, 0.90)
GREY = (0.62, 0.62, 0.62)
CTX_ALPHA = 0.18
SIZE = (1280, 960)
CONTEXT_MARGIN = 2.5
CONTEXT_MAX = 400
SKIP_CONTEXT = ("IfcMechanicalFastener", "IfcFastener", "IfcDiscreteAccessory")


def available() -> bool:
    try:
        import pyvista  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Scene + camera
# ---------------------------------------------------------------------------

@dataclass
class Camera:
    position: np.ndarray
    direction: np.ndarray
    up: np.ndarray
    fov_deg: float
    target: np.ndarray
    distance: float


@dataclass
class Scene:
    origin: np.ndarray                     # scene origin, in the model-A local frame
    A: tuple
    B: tuple
    context: tuple
    camera: Camera
    box: tuple = field(default=None)
    container: str | None = None           # "A" / "B" when one element encloses the other


def _clip_plane(v, f, ax, val, keep_ge):
    s = (v[:, ax] - val) if keep_ge else (val - v[:, ax])
    n_in = (s[f] >= 0).sum(1)
    keep = f[n_in == 3]
    part = f[(n_in > 0) & (n_in < 3)]
    if len(part) == 0:
        return v, keep
    newv, newf, base = [], [], len(v)
    for tri in part:
        poly = []
        for k in range(3):
            i, j = tri[k], tri[(k + 1) % 3]
            si, sj = s[i], s[j]
            if si >= 0:
                poly.append(v[i])
            if (si >= 0) != (sj >= 0):
                t = si / (si - sj)
                poly.append(v[i] + t * (v[j] - v[i]))
        for k in range(1, len(poly) - 1):
            newf.append((base, base + k, base + k + 1))
        newv.extend(poly)
        base += len(poly)
    v2 = np.vstack([v, np.asarray(newv, v.dtype)]) if newv else v
    f2 = np.vstack([keep, np.asarray(newf, keep.dtype)]) if newf else keep
    return v2, f2


def clip_to_box(v, f, lo, hi):
    for ax in range(3):
        v, f = _clip_plane(v, f, ax, lo[ax], True)
        v, f = _clip_plane(v, f, ax, hi[ax], False)
    return v, f


def _principal_axis(v):
    c = v - v.mean(0)
    _, vec = np.linalg.eigh(c.T @ c)
    return vec[:, -1]


def auto_camera(a_lo, a_hi, b_lo, b_hi, target, *, fov_deg=60.0, elevation_deg=35.0, axis=None,
                aspect=SIZE[0] / SIZE[1], margin=1.3, min_radius=1.0, frame_half=1.5) -> Camera:
    """3/4 plunging perspective camera looking at the clash: frames the intersection plus the
    smaller element, clipped to +-frame_half (a long wall does not zoom the view out)."""
    target = np.asarray(target, float)
    ilo, ihi = np.maximum(a_lo, b_lo), np.minimum(a_hi, b_hi)
    s_lo, s_hi = (a_lo, a_hi) if np.linalg.norm(a_hi - a_lo) < np.linalg.norm(b_hi - b_lo) else (b_lo, b_hi)
    ulo = np.maximum(np.minimum(np.minimum(ilo, ihi), s_lo), target - frame_half)
    uhi = np.minimum(np.maximum(np.maximum(ilo, ihi), s_hi), target + frame_half)
    radius = max(min_radius, 0.5 * float(np.linalg.norm(uhi - ulo)))
    if axis is not None and math.hypot(axis[0], axis[1]) > 0.5:
        az = math.radians(math.degrees(math.atan2(axis[1], axis[0])) - 125.0)
    else:
        az = math.radians(225.0)
    el = math.radians(elevation_deg)
    d = -np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])
    d /= np.linalg.norm(d)
    vfov = math.radians(fov_deg)
    hfov = 2 * math.atan(math.tan(vfov / 2) * aspect)
    dist = margin * radius / math.sin(min(vfov, hfov) / 2)
    right = np.cross(d, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, d)
    up /= np.linalg.norm(up)
    return Camera(target - d * dist, d, up, fov_deg, target, dist)


def context_meshes(models, center_local, exclude=(), margin=CONTEXT_MARGIN):
    """Already-tessellated meshes of the given models around the clash: [(mesh, shift)]."""
    c = np.asarray(center_local, float)
    out = []
    for lm, sh in models:
        sh = np.zeros(3) if sh is None else np.asarray(sh, float)
        gs = geo.geomset(lm)
        for m in gs.meshes.values():
            if m.guid in exclude or m.cls in SKIP_CONTEXT:
                continue
            if np.all(m.lo + sh <= c + margin) and np.all(m.hi + sh >= c - margin):
                out.append((m, sh))
    if len(out) > CONTEXT_MAX:
        out.sort(key=lambda t: float(np.linalg.norm((t[0].lo + t[0].hi) / 2 + t[1] - c)))
        out = out[:CONTEXT_MAX]
    return out


def build_scene(ma, mb, shift_b, center_local, context=(), margin=CONTEXT_MARGIN) -> Scene:
    sh = np.asarray(shift_b, float)
    origin = np.round(np.asarray(center_local, float), 3)
    lo, hi = origin - margin, origin + margin
    vs, fs, n = [], [], 0
    for m, s in context:
        vs.append(m.v + s)
        fs.append(m.f + n)
        n += len(m.v)
    if vs:
        cv, cf = clip_to_box(np.concatenate(vs), np.concatenate(fs), lo, hi)
    else:
        cv, cf = np.zeros((0, 3)), np.zeros((0, 3), np.int32)
    va, vb = ma.v, mb.v + sh
    axes = [_principal_axis(va), _principal_axis(vb)]
    axis = max(axes, key=lambda a: math.hypot(a[0], a[1]))
    cam = auto_camera(ma.lo - origin, ma.hi - origin, mb.lo + sh - origin, mb.hi + sh - origin,
                      np.zeros(3), axis=axis)
    container = None
    if np.all(ma.lo <= mb.lo + sh) and np.all(mb.hi + sh <= ma.hi):
        container = "A"
    elif np.all(mb.lo + sh <= ma.lo) and np.all(ma.hi <= mb.hi + sh):
        container = "B"
    # A and B clipped to the same box as the context: a 30 m slab does not fill the picture
    va, fa = clip_to_box(va, ma.f, lo, hi)
    vb, fb = clip_to_box(vb, mb.f, lo, hi)
    loc = lambda v: (v - origin).astype(np.float32)  # noqa: E731
    return Scene(origin, (loc(va), fa), (loc(vb), fb), (loc(cv), np.asarray(cf, np.int32)), cam,
                 (lo - origin, hi - origin), container)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class Renderer:
    """One off-screen PyVista plotter reused for a batch of snapshots."""

    def __init__(self, size=SIZE):
        import pyvista as pv
        pv.global_theme.allow_empty_mesh = True
        self.pv = pv
        self.pl = pv.Plotter(off_screen=True, window_size=size)
        self.pl.set_background("white")
        self.pl.enable_depth_peeling(number_of_peels=8)
        self.pl.enable_anti_aliasing("ssaa")

    def _poly(self, v, f):
        faces = np.hstack([np.full((len(f), 1), 3, np.int64), np.asarray(f, np.int64)]).ravel()
        return self.pv.PolyData(np.asarray(v, np.float64), faces).clean(tolerance=1e-6)

    @staticmethod
    def _edges(m):
        return m.extract_feature_edges(25, boundary_edges=False, non_manifold_edges=False, manifold_edges=False)

    def render(self, sc: Scene, label: str = "") -> bytes:
        pl = self.pl
        pl.clear_actors()
        vc, fc = sc.context
        if len(fc):
            m = self._poly(vc, fc)
            pl.add_mesh(m, color=GREY, opacity=CTX_ALPHA)
            pl.add_mesh(self._edges(m), color=(0.4, 0.4, 0.4), opacity=0.5, line_width=1)
        for name, (v, f), col in (("A", sc.A, RED), ("B", sc.B, BLUE)):
            if len(f):
                m = self._poly(v, f)
                # the enclosing element is see-through so the enclosed one stays visible
                pl.add_mesh(m, color=col, specular=0.3, opacity=0.35 if sc.container == name else 1.0)
                pl.add_mesh(self._edges(m), color="black", line_width=2)
        pl.add_mesh(self.pv.Sphere(radius=0.04, center=sc.camera.target), color="yellow")
        c = sc.camera
        pl.camera.position = tuple(c.position)
        pl.camera.focal_point = tuple(c.target)
        pl.camera.up = tuple(c.up)
        pl.camera.view_angle = c.fov_deg
        pl.camera.clipping_range = (0.05, c.distance * 4)
        if label:
            pl.add_text(label, font_size=9, position="upper_left", color="black")
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            pl.screenshot(tmp)
            with open(tmp, "rb") as fh:
                return fh.read()
        finally:
            os.unlink(tmp)

    def close(self):
        try:
            self.pl.close()
        except Exception:  # noqa: BLE001
            pass
