"""Synthetic IFC fixtures built with ifcopenshell.api (no external files needed)."""

import os

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.pset
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.unit
import ifcopenshell.guid
import pytest

from bim_assistant import ifc_engine as ie


def build_model(path, *, fire=("EI30", "EI30", "NC", None), extra_wall=False, rename_door0=None):
    f = ifcopenshell.file(schema="IFC4")
    project = ifcopenshell.api.root.create_entity(f, ifc_class="IfcProject", name="Demo")
    ifcopenshell.api.unit.assign_unit(f)          # default: millimetres, m2, m3
    site = ifcopenshell.api.root.create_entity(f, ifc_class="IfcSite", name="Site")
    bldg = ifcopenshell.api.root.create_entity(f, ifc_class="IfcBuilding", name="B")
    s0 = ifcopenshell.api.root.create_entity(f, ifc_class="IfcBuildingStorey", name="P00")
    s1 = ifcopenshell.api.root.create_entity(f, ifc_class="IfcBuildingStorey", name="P01")
    s0.Elevation, s1.Elevation = 0.0, 3200.0       # mm
    ifcopenshell.api.aggregate.assign_object(f, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(f, products=[bldg], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(f, products=[s0, s1], relating_object=bldg)

    walls = []
    for i in range(3 + (1 if extra_wall else 0)):
        w = ifcopenshell.api.root.create_entity(f, ifc_class="IfcWall", name=f"Mur {i}")
        w.GlobalId = f"WALL{i:018d}"[:22]
        ifcopenshell.api.spatial.assign_container(f, products=[w], relating_structure=s0 if i < 2 else s1)
        ps = ifcopenshell.api.pset.add_pset(f, product=w, name="Pset_WallCommon")
        ifcopenshell.api.pset.edit_pset(f, pset=ps, properties={"LoadBearing": i % 2 == 0,
                                                                 "FireRating": "REI60"})
        q = ifcopenshell.api.pset.add_qto(f, product=w, name="Qto_WallBaseQuantities")
        ifcopenshell.api.pset.edit_qto(f, qto=q, properties={"Length": 5000.0, "NetVolume": 1.5})
        walls.append(w)

    for i, fr in enumerate(fire):
        d = ifcopenshell.api.root.create_entity(f, ifc_class="IfcDoor",
                                                name=(rename_door0 if i == 0 and rename_door0 else f"Porte {i}"))
        d.GlobalId = f"DOOR{i:018d}"[:22]
        ifcopenshell.api.spatial.assign_container(f, products=[d], relating_structure=s0 if i < 2 else s1)
        props = {"#TYPOLOGIE": "Battante"}
        if fr:
            props["#EI_FEU_CIBLE"] = fr
        ps = ifcopenshell.api.pset.add_pset(f, product=d, name="Données")
        ifcopenshell.api.pset.edit_pset(f, pset=ps, properties=props)
        ps2 = ifcopenshell.api.pset.add_pset(f, product=d, name="Protection contre les incendies")
        ifcopenshell.api.pset.edit_pset(f, pset=ps2, properties={"#CIBLE_RES_FEU": fr or "?"})

    # Quality defects: a proxy outside any storey
    proxy = ifcopenshell.api.root.create_entity(f, ifc_class="IfcBuildingElementProxy", name="")
    proxy.GlobalId = "PROXY" + "0" * 17
    f.write(str(path))
    return path


@pytest.fixture
def model_path(tmp_path):
    ie.STORE._models.clear()
    return build_model(tmp_path / "demo.ifc")


@pytest.fixture
def lm(model_path):
    return ie.STORE.open(str(model_path))


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BCZOOM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BCZOOM_DATA_DIR", str(tmp_path / "zoomdata"))
    os.makedirs(tmp_path / "zoomdata", exist_ok=True)
