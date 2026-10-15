"""Clash engine + BCF 2.1 output on the synthetic geometry model."""

import zipfile

import ifcopenshell
import pytest

from bim_assistant import clash as cl
from bim_assistant import geometry as geo
from bim_assistant import ifc_engine as ie
from bim_assistant import snapshot as snap
from cli_anything.bczoom.core import bcf
from tests.test_geometry import _box, build_geo_model


@pytest.fixture
def clash_model(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "CACHE_DIR", tmp_path / "geomcache")
    ie.STORE._models.clear()
    ids = build_geo_model(tmp_path / "geo.ifc")
    f = ifcopenshell.open(str(tmp_path / "geo.ifc"))
    body = next(c for c in f.by_type("IfcGeometricRepresentationSubContext") if c.ContextIdentifier == "Body")
    st = f.by_type("IfcBuildingStorey")[0]
    # sleeve fully inside the slab (no triangle intersects: only a containment test finds it)
    d = _box(f, body, st, "IfcBuildingElementProxy", "Fourreau", 3, -2, -0.2, 0.1, 0.1, 0.1)
    f.write(str(tmp_path / "geo.ifc"))
    ids["D"] = d.GlobalId
    return ie.STORE.open(str(tmp_path / "geo.ifc")), ids


def _pairs(r):
    return {frozenset((c["a"], c["b"])) for c in r["clashes"]}


def test_hard_finds_crossing_and_containment(clash_model):
    lm, ids = clash_model
    r = cl.detect(lm, "IfcElement")
    pairs = _pairs(r)
    assert frozenset((ids["A"], ids["C"])) in pairs              # column through wall
    assert frozenset((ids["S"], ids["D"])) in pairs              # sleeve inside slab
    assert frozenset((ids["A"], ids["B"])) not in pairs          # face contact is not a hard clash
    ac = next(c for c in r["clashes"] if {c["a"], c["b"]} == {ids["A"], ids["C"]})
    assert ac["volume_m3"] == pytest.approx(0.24, rel=1e-3)
    assert len(r["clashes"]) == len(pairs)                       # self-clash: each pair once


def test_tolerance_and_modes(clash_model):
    lm, ids = clash_model
    assert frozenset((ids["A"], ids["C"])) not in _pairs(cl.detect(lm, "IfcElement", tolerance_m=0.25))
    touch = _pairs(cl.detect(lm, "IfcWall", mode="touch"))
    assert frozenset((ids["A"], ids["B"])) in touch
    near = cl.detect(lm, "IfcWall, IfcColumn", mode="clearance", clearance_m=1.2)
    bc = next(c for c in near["clashes"] if {c["a"], c["b"]} == {ids["B"], ids["C"]})
    assert bc["status"] == "too_close" and bc["distance_m"] == pytest.approx(1.0, abs=1e-3)


def test_two_selections_and_grouping(clash_model):
    lm, ids = clash_model
    r = cl.clash(lm, "IfcWall", lm, "IfcColumn, IfcBuildingElementProxy", group_by="element_b")
    assert r["clashes"] == 1 and r["results"][0]["group"] == ids["C"]
    assert r["results"][0]["worst"]["a"]["guid"] == ids["A"]


def test_bcf_output(clash_model, tmp_path):
    lm, ids = clash_model
    out = tmp_path / "clash.bcf"
    r = cl.clash(lm, "IfcElement", bcf_output=str(out), snapshots=snap.available())
    s = bcf.summary(out)
    assert s["version"] == "2.1" and s["topics"] == r["clashes"] == 2
    assert all(t["viewpoint"] for t in s["items"])
    assert all(t["snapshot"] for t in s["items"]) == snap.available()
    with zipfile.ZipFile(out) as z:
        vp = next(z.read(n).decode() for n in z.namelist() if n.endswith("viewpoint.bcfv"))
    assert 'Color="FF0000"' in vp and "<PerspectiveCamera>" in vp
    # stable topic ids: re-running gives the same GUIDs (re-import without duplicates)
    r2 = cl.clash(lm, "IfcElement", bcf_output=str(tmp_path / "again.bcf"), snapshots=False)
    assert {t["guid"] for t in bcf.summary(tmp_path / "again.bcf")["items"]} == {t["guid"] for t in s["items"]}
    assert r2["clashes"] == 2
