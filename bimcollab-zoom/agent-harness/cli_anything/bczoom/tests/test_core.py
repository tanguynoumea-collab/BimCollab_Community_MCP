"""Unit tests — synthetic data, no BIMcollab Zoom required."""

import json
import os
from pathlib import Path

import pytest

from cli_anything.bczoom.core import library as lib
from cli_anything.bczoom.core import nightshift as ns
from cli_anything.bczoom.core import project as prj
from cli_anything.bczoom.core import rules
from cli_anything.bczoom.core import zoomxml as zx
from cli_anything.bczoom.core.session import Session

SAMPLE = """<?xml version="1.0"?>
<bimcollabsmartviewfile>
    <version>8</version>
    <applicationversion>Win - Version : 9.5 (build 9.5.3.0)</applicationversion>
</bimcollabsmartviewfile>

<SMARTVIEWSETS>
    <SMARTVIEWSET>
        <TITLE>Set A</TITLE>
        <DESCRIPTION>a &amp; b</DESCRIPTION>
        <GUID>g-set-a</GUID>
        <MODIFICATIONDATE>2024-01-01T00:00:00</MODIFICATIONDATE>
        <SMARTVIEWS>
            <SMARTVIEW>
                <TITLE>View\x1f1</TITLE>
                <DESCRIPTION></DESCRIPTION>
                <GUID>g-view-1</GUID>
                <RULES>
                </RULES>
            </SMARTVIEW>
        </SMARTVIEWS>
    </SMARTVIEWSET>
</SMARTVIEWSETS>
"""


@pytest.fixture
def tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BCZOOM_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def sv_doc():
    return lib.new_library(lib.KINDS["smartviews"])


def _ifc(path: Path, schema="IFC4"):
    path.write_text(
        "ISO-10303-21;\nHEADER;\n"
        "FILE_DESCRIPTION(('ViewDefinition [ReferenceView_V1.2]'),'2;1');\n"
        "FILE_NAME('model.ifc','2024-05-01T10:00:00',('Jane'),('ACME'),'pre','Revit 2025','auth');\n"
        f"FILE_SCHEMA(('{schema}'));\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n",
        encoding="latin-1")
    return path


# ---------------------------------------------------------------- zoomxml (6)

class TestZoomXml:
    def test_parse_multi_root(self):
        doc = zx.parse_string(SAMPLE)
        assert [r.tag for r in doc.roots] == ["bimcollabsmartviewfile", "SMARTVIEWSETS"]

    def test_roundtrip_identical_lf(self):
        doc = zx.parse_string(SAMPLE)
        out = zx.serialize(doc)
        # empty-with-whitespace containers are normalised; everything else identical
        assert out.replace("<RULES></RULES>", "<RULES>\n                </RULES>") == SAMPLE

    def test_bytes_are_crlf_utf8(self):
        doc = zx.parse_string(SAMPLE.replace("Set A", "Contrôle"))
        b = zx.to_bytes(doc)
        assert b"\r\n" in b and b"\n" not in b.replace(b"\r\n", b"")
        assert "Contrôle".encode("utf-8") in b

    def test_no_self_closing_and_escaping(self):
        doc = zx.parse_string(SAMPLE)
        out = zx.serialize(doc)
        assert "/>" not in out
        assert "a &amp; b" in out

    def test_control_char_roundtrip(self):
        doc = zx.parse_string(SAMPLE)
        item = doc.body.find("SMARTVIEWSET/SMARTVIEWS/SMARTVIEW")
        assert zx.text_of(item, "TITLE") == "View\x1f1"
        assert "View\x1f1" in zx.serialize(doc)

    def test_invalid(self):
        with pytest.raises(zx.ZoomFormatError):
            zx.parse_string("<?xml version=\"1.0\"?>\n<a><b></a>")


# ---------------------------------------------------------------- library (12)

class TestLibrary:
    @pytest.mark.parametrize("kind", list(lib.KINDS))
    def test_new_library_each_kind(self, kind):
        doc = lib.new_library(lib.KINDS[kind])
        assert lib.detect_kind(doc).name == kind
        assert lib.summarize(doc, lib.KINDS[kind])["set_count"] == 0

    def test_add_and_find_set(self, sv_doc):
        k = lib.KINDS["smartviews"]
        s = lib.add_set(sv_doc, k, "QA")
        assert lib.find_set(sv_doc, k, "QA") is s
        assert lib.find_set(sv_doc, k, "0") is s
        assert lib.find_set(sv_doc, k, zx.text_of(s, "GUID")) is s

    def test_duplicate_set_rejected(self, sv_doc):
        k = lib.KINDS["smartviews"]
        lib.add_set(sv_doc, k, "QA")
        with pytest.raises(lib.LibraryError):
            lib.add_set(sv_doc, k, "QA")

    def test_unknown_set_lists_available(self, sv_doc):
        k = lib.KINDS["smartviews"]
        lib.add_set(sv_doc, k, "QA")
        with pytest.raises(lib.LibraryError, match="QA"):
            lib.find_set(sv_doc, k, "nope")

    def test_smartproperties_set_uses_name(self):
        k = lib.KINDS["smartproperties"]
        doc = lib.new_library(k)
        s = lib.add_set(doc, k, "Props")
        assert [c.tag for c in s][:3] == ["NAME", "GUID", "DESCRIPTION"]

    def test_remove_item(self, sv_doc):
        k = lib.KINDS["smartviews"]
        s = lib.add_set(sv_doc, k, "QA")
        lib.append_item(s, k, rules.build_smartview("V1"))
        assert lib.remove_item(sv_doc, k, "QA", "V1") == "V1"
        assert lib.items(s, k) == []

    def test_extract(self, sv_doc):
        k = lib.KINDS["smartviews"]
        lib.add_set(sv_doc, k, "A")
        lib.add_set(sv_doc, k, "B")
        out = lib.extract_sets(sv_doc, k, ["B"])
        assert [lib.title(s, k) for s in lib.sets(out, k)] == ["B"]

    def test_merge_rename_skip_replace(self, sv_doc):
        k = lib.KINDS["smartviews"]
        lib.add_set(sv_doc, k, "A")
        other = sv_doc.copy()
        merged, rep = lib.merge([sv_doc, other], k, "rename")
        assert [lib.title(s, k) for s in lib.sets(merged, k)] == ["A", "A (2)"]
        merged, rep = lib.merge([sv_doc, other], k, "skip")
        assert len(lib.sets(merged, k)) == 1 and rep[1]["action"] == "skipped"
        merged, rep = lib.merge([sv_doc, other], k, "replace")
        assert len(lib.sets(merged, k)) == 1 and rep[1]["action"] == "replaced"

    def test_merge_mixed_kinds_rejected(self, sv_doc):
        with pytest.raises(lib.LibraryError):
            lib.merge([sv_doc, lib.new_library(lib.KINDS["lists"])], lib.KINDS["smartviews"])

    def test_validate_flags_bad_color_and_dup_guid(self, sv_doc):
        k = lib.KINDS["smartviews"]
        s = lib.add_set(sv_doc, k, "QA")
        v1 = rules.build_smartview("V1", rules=[rules.build_rule(action="SetColored", color="red")])
        v1.find("RULES/RULE/ACTION/R").text = "300"
        v2 = rules.build_smartview("V2", rules=[rules.build_rule()])
        v2.find("GUID").text = v1.find("GUID").text
        lib.append_item(s, k, v1)
        lib.append_item(s, k, v2)
        msgs = [i["message"] for i in lib.validate(sv_doc, k)]
        assert any("not in 0-255" in m for m in msgs)
        assert any("Duplicate GUID" in m for m in msgs)


# ---------------------------------------------------------------- rules (12)

class TestRules:
    def test_type_only_rule(self):
        r = rules.build_rule(ifc_type="Wall")
        d = lib.rule_to_dict(r)
        assert d["property"] == "None" and d["condition"] == "Equals" and d["state"] == "Empty"

    def test_property_rule_infers_types(self):
        r = rules.build_rule(ifc_type="Any", prop="Name", condition="Contains", value="X")
        d = lib.rule_to_dict(r)
        assert (d["property_set"], d["property_type"], d["value_type"], d["value"]) == \
               ("Summary", "Summary", "StringValue", "X")

    def test_pset_property_type(self):
        d = lib.rule_to_dict(rules.build_rule(prop="FireRating", pset="Pset_WallCommon",
                                              condition="Is", value="EI60"))
        assert d["property_type"] == "PropertySet"

    def test_valueless_condition_writes_empty_value(self):
        r = rules.build_rule(prop="Load Bearing", condition="BooleanTrue")
        assert r.find("CONDITION/VALUE") is not None and r.find("CONDITION/VALUE").text == ""
        assert lib.rule_to_dict(r)["value_type"] == "BoolValue"

    def test_state_condition(self):
        r = rules.build_rule(prop="Height", condition="NumericIsDefined")
        assert r.find("CONDITION/STATE").text == "Empty"

    def test_numeric_value_format(self):
        r = rules.build_rule(prop="Story Number", condition="Equals", value="0")
        assert r.find("CONDITION/VALUE").text == "0.00000000000"

    def test_numeric_value_rejects_text(self):
        with pytest.raises(lib.LibraryError):
            rules.build_rule(prop="Story Number", condition="Less", value="abc")

    def test_value_required(self):
        with pytest.raises(lib.LibraryError, match="requires --value"):
            rules.build_rule(prop="Name", condition="Contains")

    @pytest.mark.parametrize("spec,expected", [
        ("red", (255, 0, 0, 255)), ("#00FF0080", (0, 255, 0, 128)),
        ("1,2,3", (1, 2, 3, 255)), ("1,2,3,4", (1, 2, 3, 4))])
    def test_colors(self, spec, expected):
        assert rules.parse_color(spec) == expected

    def test_bad_inputs(self):
        with pytest.raises(lib.LibraryError):
            rules.build_rule(action="Explode")
        with pytest.raises(lib.LibraryError):
            rules.parse_color("#12")
        with pytest.raises(lib.LibraryError):
            rules.build_rule(action="Add", color="red")

    def test_smartview_structure(self):
        sv = rules.build_smartview("T", author="me", rules=[rules.build_rule()])
        assert [c.tag for c in sv] == ["TITLE", "DESCRIPTION", "CREATOR", "CREATIONDATE",
                                       "MODIFIER", "MODIFICATIONDATE", "GUID", "RULES",
                                       "INFORMATIONTAKEOFF", "EXPLODEMODE"]

    def test_clashrule_structure(self):
        cr = rules.build_clashrule("C", min_distance=0.05, same_model=False,
                                   source_rules=[rules.build_rule(ifc_type="Beam")],
                                   penetration_tolerance=0.01)
        assert cr.find("MINIMUMDISTANCE").text == "0.05"
        assert cr.find("CLASHSAMEMODEL").text == "false"
        assert cr.find("PENETRATIONDEPTHTOLERANCEENABLED").text == "true"
        assert len(cr.findall("SOURCESET/SMARTVIEW/RULES/RULE")) == 1
        assert len(cr.findall("TARGETSET/SMARTVIEW/RULES/RULE")) == 0


# ---------------------------------------------------------------- nightshift (9)

class TestNightshift:
    def test_open_models_forward_slashes(self, tmp_dir):
        f = _ifc(tmp_dir / "m.ifc")
        a = ns.action_open_models([str(f)])
        assert a["type"] == "OPEN_MODELS" and "\\" not in a["models"][0]

    def test_add_to_project(self, tmp_dir):
        f = _ifc(tmp_dir / "m.ifc")
        assert ns.action_open_models([str(f)], add=True)["type"] == "ADD_TO_PROJECT"

    def test_missing_model(self):
        with pytest.raises(ns.NightshiftError):
            ns.action_open_models(["C:/nope/x.ifc"])

    def test_sync_kept_last(self):
        s = ns.new_script("t")
        ns.append(s, ns.action_sync())
        ns.append(s, ns.action_run_lists())
        assert [a["type"] for a in s["actions"]] == ["RUN_AND_SHARE_LISTS", "SYNCHRONIZE_PROJECT"]

    def test_filter_defaults_omitted_priority_kept(self):
        a = ns.action_filter_issues(filter_type="Issue", show_active=True, priority=None)
        assert a == {"type": "FILTER_ISSUES", "filter_type": "Issue", "priority": "All"}

    def test_filter_bad_deadline(self):
        with pytest.raises(ns.NightshiftError):
            ns.action_filter_issues(deadline="2024-01-01")

    def test_validate_errors(self):
        s = {"title": "", "actions": [{"type": "OPEN_MODELS", "models": ["C:\\x.ifc"]},
                                      {"type": "SYNCHRONIZE_PROJECT"}, {"type": "BOGUS"}]}
        msgs = " | ".join(i["message"] for i in ns.validate(s, check_files=False))
        assert "title" in msgs and "forward slashes" in msgs and "Unknown type" in msgs
        assert "must be the last" in msgs

    def test_describe_cloud_flags(self):
        s = ns.new_script("t")
        ns.append(s, ns.action_connect("x.bimcollab.com", "P"))
        d = ns.describe(s)
        assert d["touches_cloud"] and not d["writes_to_cloud"]
        ns.append(s, ns.action_sync())
        assert ns.describe(s)["writes_to_cloud"]

    def test_save_rejects_spaces(self, tmp_dir):
        with pytest.raises(ns.NightshiftError):
            ns.save(ns.new_script("t"), tmp_dir / "my script.json")


# ---------------------------------------------------------------- project (4)

class TestProject:
    def test_probe_ifc(self, tmp_dir):
        info = prj.probe_ifc(_ifc(tmp_dir / "m.ifc", "IFC2X3"))
        assert info["schema"] == "IFC2X3"
        assert info["originating_system"] == "Revit 2025"
        assert info["author"] == "Jane" and info["organization"] == "ACME"
        assert "ReferenceView" in info["description"]

    def test_probe_rejects_non_ifc(self, tmp_dir):
        p = tmp_dir / "x.ifc"
        p.write_text("hello")
        with pytest.raises(lib.LibraryError):
            prj.probe_ifc(p)

    def test_new_add_remove(self, tmp_dir):
        doc = prj.new_project()
        prj.add_model(doc, str(_ifc(tmp_dir / "a.ifc")))
        with pytest.raises(lib.LibraryError):
            prj.add_model(doc, str(tmp_dir / "a.ifc"))
        assert prj.info(doc)["model_count"] == 1
        prj.remove_model(doc, "a.ifc")
        assert prj.info(doc)["model_count"] == 0

    def test_relink(self, tmp_dir):
        doc = prj.new_project()
        prj.add_model(doc, str(_ifc(tmp_dir / "a.ifc")))
        doc.root("project").find("model/filepath").text = "Z:/gone/a.ifc"
        rep = prj.relink(doc, [str(tmp_dir)])
        assert rep[0]["to"] and Path(rep[0]["to"]).exists()


# ---------------------------------------------------------------- session (4)

class TestSession:
    def _new(self, tmp_dir):
        path = tmp_dir / "s.bcsv"
        s = Session()
        s.attach_new(str(path), lib.new_library(lib.KINDS["smartviews"]))
        s.save_session()
        return s, path

    def test_undo_redo(self, tmp_dir):
        s, _ = self._new(tmp_dir)
        k = lib.KINDS["smartviews"]
        s.snapshot("add")
        lib.add_set(s.obj, k, "A")
        assert s.undo() and lib.sets(s.obj, k) == []
        assert s.redo() and len(lib.sets(s.obj, k)) == 1

    def test_undo_empty(self, tmp_dir):
        s, _ = self._new(tmp_dir)
        assert s.undo() is False

    def test_history_persists_across_sessions(self, tmp_dir):
        s, path = self._new(tmp_dir)
        s.snapshot("add")
        lib.add_set(s.obj, lib.KINDS["smartviews"], "A")
        s.save_session()
        s2 = Session()
        s2.open(str(path))
        assert s2.status()["undo_depth"] == 1
        assert s2.undo() and lib.sets(s2.obj, lib.KINDS["smartviews"]) == []

    def test_history_dropped_after_external_edit(self, tmp_dir):
        s, path = self._new(tmp_dir)
        s.snapshot("add")
        lib.add_set(s.obj, lib.KINDS["smartviews"], "A")
        s.save_session()
        doc = zx.parse_file(path)
        lib.add_set(doc, lib.KINDS["smartviews"], "external")
        zx.write_file(doc, path)
        s2 = Session()
        s2.open(str(path))
        assert s2.status()["undo_depth"] == 0


# ---------------------------------------------------------------- nightshift log (2)

class TestNightshiftLog:
    def _log(self, tmp_dir, exec_status="OK", code=0):
        from cli_anything.bczoom.utils import zoom_backend as backend
        p = tmp_dir / "2026-01-01-00-00-00_RobotScriptOutput.JSON"
        p.write_text(json.dumps({
            "original_title": "t", "status": "OK" if code == 0 else "FAILED", "error_code": code,
            "total_time-sec": 1.5,
            "actions": [{"original_type": "OPEN_MODELS", "output": {
                "parsing_status": "OK", "execution_status": exec_status,
                "error_code": code, "time-elapsed-ms": 1500}}]}), encoding="utf-8")
        return backend.parse_log(str(p))

    def test_ok_log(self, tmp_dir):
        r = self._log(tmp_dir)
        assert r["ok"] and r["status"] == "OK" and r["actions"][0]["type"] == "OPEN_MODELS"

    def test_failed_action(self, tmp_dir):
        r = self._log(tmp_dir, exec_status="FAILED", code=3)
        assert not r["ok"] and r["failures"][0]["error_code"] == 3
