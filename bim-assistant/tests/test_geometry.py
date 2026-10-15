"""Geometry layer on a small synthetic model with real Body geometry (metres)."""

import numpy as np
import ifcopenshell
import ifcopenshell.api as api
import pytest

from bim_assistant import geometry as geo
from bim_assistant import ifc_engine as ie


def _box(f, body, storey, cls, name, x, y, z, lx, ly, h, qto_volume=None):
    """Axis-aligned box: x..x+lx, y..y+ly, z..z+h (wall-style extrusion)."""
    el = api.run("root.create_entity", f, ifc_class=cls, name=name)
    rep = api.run("geometry.add_wall_representation", f, context=body, length=lx, height=h, thickness=ly)
    api.run("geometry.assign_representation", f, product=el, representation=rep)
    m = np.eye(4)
    m[:3, 3] = [x, y, z]
    api.run("geometry.edit_object_placement", f, product=el, matrix=m)
    api.run("spatial.assign_container", f, products=[el], relating_structure=storey)
    if qto_volume is not None:
        q = api.run("pset.add_qto", f, product=el, name="Qto_WallBaseQuantities")
        api.run("pset.edit_qto", f, qto=q, properties={"NetVolume": qto_volume})
    return el


def build_geo_model(path):
    f = ifcopenshell.file(schema="IFC4")
    project = api.run("root.create_entity", f, ifc_class="IfcProject", name="Geo")
    api.run("unit.assign_unit", f)
    ctx = api.run("context.add_context", f, context_type="Model")
    body = api.run("context.add_context", f, context_type="Model", context_identifier="Body",
                   target_view="MODEL_VIEW", parent=ctx)
    site = api.run("root.create_entity", f, ifc_class="IfcSite", name="Site")
    bldg = api.run("root.create_entity", f, ifc_class="IfcBuilding", name="B")
    st = api.run("root.create_entity", f, ifc_class="IfcBuildingStorey", name="P00")
    api.run("aggregate.assign_object", f, products=[site], relating_object=project)
    api.run("aggregate.assign_object", f, products=[bldg], relating_object=site)
    api.run("aggregate.assign_object", f, products=[st], relating_object=bldg)
    els = {
        # wall A 5 x 0.2 x 3 m (Qto deliberately wrong: 1.5 m3 instead of 3)
        "A": _box(f, body, st, "IfcWall", "Mur A", 0, 0, 0, 5, 0.2, 3, qto_volume=1.5),
        # lining B against A's face y=0.2 over 2 m -> 6 m2 of contact on A
        "B": _box(f, body, st, "IfcWall", "Doublage B", 1, 0.2, 0, 2, 0.2, 3, qto_volume=1.2),
        # column crossing A: overlap 0.4 x 0.2 x 3 = 0.24 m3
        "C": _box(f, body, st, "IfcColumn", "Poteau C", 4, -0.1, 0, 0.4, 0.4, 3),
        "S": _box(f, body, st, "IfcSlab", "Dalle", -0.5, -3, -0.25, 6, 6, 0.25),
        "K": _box(f, body, st, "IfcCovering", "Plafond", 0, -2, 2.6, 4, 2, 0.05),
    }
    f.write(str(path))
    return {k: e.GlobalId for k, e in els.items()}


@pytest.fixture
def geo_model(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "CACHE_DIR", tmp_path / "geomcache")
    ie.STORE._models.clear()
    ids = build_geo_model(tmp_path / "geo.ifc")
    return ie.STORE.open(str(tmp_path / "geo.ifc")), ids


def test_geometry_volumes_and_qto_check(geo_model):
    lm, ids = geo_model
    r = geo.geometry(lm, "IfcWall", group_by="Name", compare_qto=True)
    assert r["measured"] == 2
    assert r["groups"]["Mur A"]["volume_m3"] == pytest.approx(3.0, abs=1e-6)
    assert r["groups"]["Mur A"]["lateral_area_m2"] == pytest.approx(2 * 15 + 2 * 0.6, abs=1e-6)
    worst = r["qto_check"]["worst"]
    assert [w["guid"] for w in worst] == [ids["A"]]          # B's Qto (1.2) is right
    assert worst[0]["ratio"] == pytest.approx(0.5, abs=1e-3)


def test_disk_cache_reused(geo_model, tmp_path):
    lm, ids = geo_model
    geo.geometry(lm, "IfcWall")
    files = list((tmp_path / "geomcache").glob("*.npz"))
    assert len(files) == 1
    lm.geom = None                                            # forget the in-memory meshes
    gs = geo.geomset(lm)
    gs._load_disk()
    assert {ids["A"], ids["B"]} <= set(gs.meshes)


def test_distance_overlap_and_gap(geo_model):
    lm, ids = geo_model
    ov = geo.distance(lm, ids["A"], ids["C"])
    assert ov["status"] == "overlap"
    assert ov["volume_m3"] == pytest.approx(0.24, rel=1e-3)
    assert ov["depth_m"] == pytest.approx(0.2, abs=1e-3)
    gap = geo.distance(lm, ids["B"], ids["C"])
    assert gap["status"] == "gap" and gap["distance_m"] == pytest.approx(1.0, abs=1e-3)
    touch = geo.distance(lm, ids["A"], ids["B"])
    assert touch["status"] == "contact"


def test_nearby(geo_model):
    lm, ids = geo_model
    r = geo.nearby(lm, ids["B"], within_m=1.5, selector="IfcWall, IfcColumn")
    assert [n["guid"] for n in r["nearby"]] == [ids["A"], ids["C"]]


def test_contact_area_and_bands(geo_model):
    lm, ids = geo_model
    r = geo.contact_area(lm, 'IfcWall, Name="Mur A"', "IfcWall", group_by=None)
    t = r["totals"]
    assert t["lateral_gross_m2"] == pytest.approx(31.2, abs=1e-3)
    assert t["contact_m2"] == pytest.approx(6.0, abs=1e-3)
    assert t["net_m2"] == pytest.approx(25.2, abs=1e-3)
    # ceiling underside at 2.6 m over part of A's footprint: the band above it is excluded
    r2 = geo.contact_area(lm, 'IfcWall, Name="Mur A"', "IfcWall", group_by=None, ceiling_selector="IfcCovering")
    assert r2["totals"]["hidden_by_floor_ceiling_m2"] == pytest.approx(31.2 * 0.4 / 3, abs=1e-3)


def test_clearance(geo_model):
    lm, ids = geo_model
    r = geo.clearance(lm, "IfcSlab", "IfcCovering", grid_m=0.5, group_by=None)
    assert r["overall"]["min_m"] == pytest.approx(2.6, abs=1e-3)
    assert r["lowest"][0]["obstacle"]["guid"] == ids["K"]
    assert r["rays_without_obstacle"] > 0                     # most of the slab has no ceiling


def test_element_cap(geo_model, monkeypatch):
    lm, _ = geo_model
    monkeypatch.setattr(geo, "MAX_ELEMENTS", 1)
    with pytest.raises(geo.GeometryError, match="limit 1"):
        geo.geometry(lm, "IfcWall")
