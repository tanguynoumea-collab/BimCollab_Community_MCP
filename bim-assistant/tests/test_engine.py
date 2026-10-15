"""IFC engine + Zoom bridge tests on a synthetic model."""

import json
from pathlib import Path

import pytest

from bim_assistant import ifc_engine as ie
from bim_assistant import zoom_bridge as zb
from cli_anything.bczoom.core import library as lib
from cli_anything.bczoom.core import zoomxml as zx
from tests.conftest import build_model


class TestEngine:
    def test_cache_reuses_model(self, model_path):
        a = ie.STORE.open(str(model_path))
        assert ie.STORE.open(str(model_path)) is a
        assert ie.STORE.get("demo") is a and ie.STORE.get("demo.ifc") is a

    def test_get_unknown(self):
        with pytest.raises(ie.IfcEngineError):
            ie.STORE.get("nope")

    def test_summary(self, lm):
        s = ie.summary(lm)
        assert s["schema"] == "IFC4"
        assert [x["name"] for x in s["storeys"]] == ["P00", "P01"]
        assert s["storeys"][1]["elevation_m"] == 3.2
        assert s["elements_by_class"] == {"IfcDoor": 4, "IfcWall": 3, "IfcBuildingElementProxy": 1}

    def test_count_group_by_two_fields(self, lm):
        c = ie.count(lm, "IfcDoor", ["storey.Name", "Données.#EI_FEU_CIBLE"])
        assert c["counts"] == {"P00 | EI30": 2, "P01 | NC": 1, "P01 | (vide)": 1}

    def test_pset_with_spaces(self, lm):
        dv = ie.distinct_values(lm, "IfcDoor", "Protection contre les incendies.#CIBLE_RES_FEU")
        assert dv["values"] == {"EI30": 2, "NC": 1, "?": 1}

    def test_selector_quoted_pset(self, lm):
        assert len(ie.select(lm, 'IfcDoor, "Protection contre les incendies".#CIBLE_RES_FEU=EI30')) == 2

    def test_bad_selector(self, lm):
        with pytest.raises(ie.IfcEngineError):
            ie.select(lm, "IfcWall,,,===")

    def test_query_pagination(self, lm):
        q = ie.query(lm, "IfcWall", ["Name", "Pset_WallCommon.LoadBearing"], limit=2)
        assert q["total"] == 3 and q["returned"] == 2 and q["truncated"]
        assert q["rows"][0]["Pset_WallCommon.LoadBearing"] is True

    def test_element(self, lm):
        e = ie.element(lm, "DOOR" + "0" * 18)
        assert e["class"] == "IfcDoor" and e["container"]["name"] == "P00"
        assert e["psets"]["Données"]["#EI_FEU_CIBLE"] == "EI30"

    def test_catalog(self, lm):
        cat = {p["field"]: p for p in ie.properties_catalog(lm, "IfcDoor")["properties"]}
        assert cat["Données.#EI_FEU_CIBLE"]["fill_pct"] == 75.0
        assert cat["Données.#TYPOLOGIE"]["fill_pct"] == 100.0

    def test_quantities(self, lm):
        q = ie.quantities(lm, "IfcWall", "storey.Name")
        assert q["groups"]["P00"]["Length [m]"] == 10.0
        assert q["groups"]["P01"]["NetVolume [m3]"] == 1.5

    def test_audit(self, lm):
        a = ie.audit(lm, required=["Données.#EI_FEU_CIBLE"], selector="IfcDoor")
        checks = {i["check"]: i["count"] for i in a["issues"]}
        assert checks["missing:Données.#EI_FEU_CIBLE"] == 1
        full = {i["check"] for i in ie.audit(lm)["issues"]}
        assert {"building_element_proxy", "no_spatial_container", "empty_name"} <= full

    def test_compare(self, tmp_path, lm):
        b_path = build_model(tmp_path / "demo_v2.ifc", fire=("EI60", "EI30", "NC", None),
                             extra_wall=True, rename_door0="Porte entrée")
        lb = ie.STORE.open(str(b_path))
        d = ie.compare(lm, lb)
        assert d["added"] == 1 and d["added_by_class"] == {"IfcWall": 1}
        assert d["changed"] == 1
        ch = d["changed_sample"][0]["changes"]
        assert ch["Name"] == ["Porte 0", "Porte entrée"]
        assert ch["properties"]["Données.#EI_FEU_CIBLE"] == ["EI30", "EI60"]

    def test_spatial_tree(self, lm):
        t = ie.spatial_tree(lm)["tree"][0]
        storeys = t["children"][0]["children"][0]["children"]
        assert [s["name"] for s in storeys] == ["P00", "P01"] and storeys[0]["elements"] == 4

    @pytest.mark.parametrize("ext", [".xlsx", ".csv"])
    def test_export(self, lm, tmp_path, ext):
        r = ie.export_table(lm, "IfcDoor", ["GlobalId", "Name", "Données.#EI_FEU_CIBLE"],
                            str(tmp_path / f"out{ext}"))
        assert r["rows"] == 4 and Path(r["output"]).stat().st_size > 0


class TestZoomBridge:
    @pytest.mark.parametrize("cls,name", [("IfcDuctSegment", "Duct Segment"), ("IfcWallStandardCase", "Wall"),
                                          ("IfcBuildingStorey", "Building Story"),
                                          ("IfcOpeningElement", "Opening"), ("Any", "Any"), ("Door", "Door")])
    def test_zoom_type(self, cls, name):
        assert zb.zoom_type(cls) == name

    def test_color_by_values_from_ifc(self, lm, tmp_path):
        dv = ie.distinct_values(lm, "IfcDoor", "Données.#EI_FEU_CIBLE")
        out = tmp_path / "a.bcsv"
        r = zb.smartview_color_by_values(str(out), "Assistant", "Portes EI", "IfcDoor", "Données",
                                         "#EI_FEU_CIBLE", list(dv["values"]))
        assert set(r["legend"]) == {"EI30", "NC", "(non défini)"}
        doc, kind = lib.load(str(out))
        rules = lib.item_detail(lib.find_item(lib.find_set(doc, kind, "Assistant"), kind, "Portes EI"),
                                kind)["rules"]
        assert rules[0]["ifc_type"] == "Door" and rules[0]["action"] == "Add"
        assert rules[1]["property_set"] == "Données" and rules[1]["property_type"] == "PropertySet"
        assert rules[-1]["condition"] == "StringIsNotDefined"
        assert zx.to_bytes(zx.parse_file(out)) == out.read_bytes()

    def test_smartview_replace(self, tmp_path):
        out = str(tmp_path / "a.bcsv")
        zb.smartview_create(out, "S", "V", [{"ifc_class": "IfcWall"}])
        with pytest.raises(zb.ZoomBridgeError):
            zb.smartview_create(out, "S", "V", [{"ifc_class": "IfcWall"}])
        zb.smartview_create(out, "S", "V", [{"ifc_class": "IfcSlab"}], replace=True)
        assert zb.library_summary(out)["item_count"] == 1

    def test_clash_rule(self, tmp_path):
        r = zb.clash_rule_create(str(tmp_path / "c.bcr"), "Synth", "CVC/STR",
                                 [{"ifc_class": "IfcDuctSegment"}], [{"ifc_class": "IfcBeam"}], 0.05)
        assert r["detail"]["source"][0]["ifc_type"] == "Duct Segment"
        assert r["detail"]["minimumdistance"] == "0.05"

    def test_install_library_backup_and_merge(self, tmp_path, monkeypatch):
        monkeypatch.setattr(zb.backend, "running_instances", lambda: [])
        live_dir = lib.zoom_data_dir()
        live = live_dir / "smartviews.bcsv"
        zb.smartview_create(str(live), "Existing", "Keep", [{"ifc_class": "IfcWall"}])
        src = str(tmp_path / "new.bcsv")
        zb.smartview_create(src, "Assistant", "V", [{"ifc_class": "IfcDoor"}])
        r = zb.install_library(src)
        assert Path(r["backup"]).exists()
        names = [s["title"] for s in lib.summarize(*lib.load(str(live)))["sets"]]
        assert names == ["Existing", "Assistant"]

    def test_install_refused_when_zoom_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr(zb.backend, "running_instances", lambda: [1234])
        with pytest.raises(zb.ZoomBridgeError, match="open"):
            zb.install_library(str(tmp_path / "x.bcsv"))

    def test_nightshift_cloud_guard(self, model_path):
        with pytest.raises(zb.ZoomBridgeError, match="confirm_cloud_write"):
            zb.nightshift_run([str(model_path)], space="x.bimcollab.com", project_name="P", sync=True)

    def test_project_create(self, model_path, tmp_path):
        r = zb.project_create(str(tmp_path / "p.bcp"), [str(model_path)])
        assert Path(r["project"]).exists() and r["models"] == ["demo.ifc"]
