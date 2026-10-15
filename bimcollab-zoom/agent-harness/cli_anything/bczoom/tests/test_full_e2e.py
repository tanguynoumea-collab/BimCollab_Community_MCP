"""E2E tests — real Zoom data files, real IFC, installed CLI, real BIMcollab Zoom.

* Requires BIMcollab Zoom installed (tests fail, not skip, when it is missing).
* The true-backend Nightshift test is marked ``gui``: it starts Zoom and needs
  Zoom to be closed. Run it explicitly with ``pytest -m gui``.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cli_anything.bczoom.core import library as lib
from cli_anything.bczoom.core import project as prj
from cli_anything.bczoom.core import zoomxml as zx
from cli_anything.bczoom.utils import zoom_backend as backend

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_IFC = FIXTURES / "sample.ifc"


def _resolve_cli(name):
    """Resolve installed CLI command; falls back to python -m for dev.

    Set env CLI_ANYTHING_FORCE_INSTALLED=1 to require the installed command.
    """
    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if not path:
        cand = Path(sys.executable).parent / (name + (".exe" if os.name == "nt" else ""))
        path = str(cand) if cand.exists() else None
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    module = name.replace("cli-anything-", "cli_anything.") + "." + name.split("-")[-1] + "_cli"
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m {module}")
    return [sys.executable, "-m", module]


@pytest.fixture
def tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BCZOOM_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


# ---------------------------------------------------------------------------
# Native format fidelity against the user's real Zoom files
# ---------------------------------------------------------------------------

def _real_zoom_files():
    d = lib.zoom_data_dir()
    files = sorted(p for p in d.glob("*") if p.suffix.lower() in (".bcsv", ".bcr", ".bcl", ".bcsp", ".bcp"))
    return files


class TestNativeFidelity:
    def test_zoom_data_dir_has_libraries(self):
        files = _real_zoom_files()
        assert files, f"No Zoom libraries found in {lib.zoom_data_dir()} — is BIMcollab Zoom installed?"

    @pytest.mark.parametrize("path", _real_zoom_files(), ids=lambda p: p.name)
    def test_byte_identical_roundtrip(self, path, tmp_dir):
        copy = tmp_dir / path.name
        shutil.copyfile(path, copy)
        original = copy.read_bytes()
        zx.write_file(zx.parse_file(copy), copy)
        assert copy.read_bytes() == original
        print(f"\n  round-trip OK: {path.name} ({len(original):,} bytes)")

    def test_live_libraries_validate(self, tmp_dir):
        for kind in lib.KINDS.values():
            p = lib.zoom_data_dir() / kind.user_file
            if not p.exists():
                continue
            doc, k = lib.load(str(p))
            errors = [i for i in lib.validate(doc, k) if i["level"] == "error"]
            assert not errors, f"{p.name}: {errors[:3]}"


# ---------------------------------------------------------------------------
# Real IFC
# ---------------------------------------------------------------------------

class TestRealIfc:
    def test_probe_sample(self):
        info = prj.probe_ifc(SAMPLE_IFC)
        assert info["schema"] == "IFC4"
        assert "Exporter" in info["originating_system"]

    def test_project_from_ifc_parses_back(self, tmp_dir):
        doc = prj.new_project()
        prj.add_model(doc, str(SAMPLE_IFC))
        out = tmp_dir / "p.bcp"
        zx.write_file(doc, out)
        data = out.read_bytes()
        assert data.startswith(b'<?xml version="1.0"?>\r\n<bimcollabprojectfile>')
        info = prj.info(prj.load(str(out)))
        assert info["model_count"] == 1 and info["missing_models"] == 0
        print(f"\n  BCP: {out} ({len(data):,} bytes)")


# ---------------------------------------------------------------------------
# Installed CLI (subprocess)
# ---------------------------------------------------------------------------

class TestCLISubprocess:
    CLI_BASE = _resolve_cli("cli-anything-bczoom")

    def _run(self, args, check=True, env_extra=None):
        env = dict(os.environ)
        env.update(env_extra or {})
        res = subprocess.run(self.CLI_BASE + args, capture_output=True, text=True,
                             encoding="utf-8", env=env)
        if check and res.returncode != 0:
            raise AssertionError(f"{args} failed ({res.returncode}): {res.stdout}\n{res.stderr}")
        return res

    def _json(self, args, **kw):
        return json.loads(self._run(["--json"] + args, **kw).stdout)

    def test_help(self):
        out = self._run(["--help"]).stdout
        for group in ("smartview", "clash", "nightshift", "project", "library"):
            assert group in out

    def test_app_info_real_exe(self):
        data = self._json(["app", "info"])
        assert Path(data["exe"]).exists()
        assert data["version"] and data["version"] != "unknown"
        print(f"\n  Zoom {data['version']} at {data['exe']}")

    def test_smartview_workflow(self, tmp_dir):
        f = str(tmp_dir / "qa.bcsv")
        env = {"BCZOOM_STATE_DIR": str(tmp_dir / "state"), "BCZOOM_AUTHOR": "qa@test"}
        self._run(["library", "new", "smartviews", "-o", f], env_extra=env)
        self._run(["--project", f, "smartview", "add", "--set", "Contrôles", "--title",
                   "Murs porteurs", "--create-set"], env_extra=env)
        self._run(["--project", f, "smartview", "add-rule", "--set", "Contrôles", "--view",
                   "Murs porteurs", "--ifc-type", "Wall"], env_extra=env)
        self._run(["--project", f, "smartview", "add-rule", "--set", "0", "--view", "0",
                   "--ifc-type", "Wall", "--prop", "Load Bearing", "--condition", "BooleanTrue",
                   "--action", "SetColored", "--color", "#FF8800"], env_extra=env)
        self._run(["--project", f, "smartview", "color-by-values", "--set", "0", "--title",
                   "Portes", "--ifc-type", "Door", "--prop", "Type", "--values", "P1,P2"],
                  env_extra=env)
        assert self._json(["--project", f, "library", "validate"], env_extra=env)["ok"]
        detail = self._json(["--project", f, "library", "show", "--set", "0", "--item",
                             "Murs porteurs"], env_extra=env)
        assert detail["rules"][1]["color"] == [255, 136, 0, 255]
        assert detail["creator"] == "qa@test"
        # Undo across processes
        self._run(["--project", f, "session", "undo"], env_extra=env)
        info = self._json(["--project", f, "library", "info"], env_extra=env)
        assert info["item_count"] == 1
        # The written file is in Zoom's native format
        raw = Path(f).read_bytes()
        assert raw.startswith(b'<?xml version="1.0"?>\r\n<bimcollabsmartviewfile>')
        assert zx.to_bytes(zx.parse_file(f)) == raw
        print(f"\n  BCSV: {f} ({len(raw):,} bytes)")

    def test_dry_run_does_not_write(self, tmp_dir):
        f = tmp_dir / "d.bcsv"
        env = {"BCZOOM_STATE_DIR": str(tmp_dir / "state")}
        self._run(["library", "new", "smartviews", "-o", str(f)], env_extra=env)
        before = f.read_bytes()
        self._run(["--dry-run", "--project", str(f), "library", "add-set", "X"], env_extra=env)
        assert f.read_bytes() == before

    def test_clash_workflow(self, tmp_dir):
        f = str(tmp_dir / "c.bcr")
        env = {"BCZOOM_STATE_DIR": str(tmp_dir / "state")}
        self._run(["library", "new", "clashrules", "-o", f], env_extra=env)
        self._run(["--project", f, "clash", "add", "--set", "Synthèse", "--title", "CVC vs STR",
                   "--min-distance", "0.05", "--no-same-model", "--create-set"], env_extra=env)
        self._run(["--project", f, "clash", "add-rule", "--set", "0", "--rule", "0", "--side",
                   "source", "--ifc-type", "Duct Segment"], env_extra=env)
        self._run(["--project", f, "clash", "add-rule", "--set", "0", "--rule", "0", "--side",
                   "target", "--ifc-type", "Beam"], env_extra=env)
        self._run(["--project", f, "clash", "set", "--set", "0", "--rule", "0",
                   "--subparts", "true"], env_extra=env)
        d = self._json(["--project", f, "library", "show", "--set", "0", "--item", "0"], env_extra=env)
        assert d["minimumdistance"] == "0.05" and d["clashsamemodel"] == "false"
        assert d["clashsubparts"] == "true"
        assert d["source"][0]["ifc_type"] == "Duct Segment" and d["target"][0]["ifc_type"] == "Beam"

    def test_extract_and_merge_live_copy(self, tmp_dir):
        live = lib.zoom_data_dir() / "smartviews.bcsv"
        assert live.exists(), "Live smartviews library missing"
        copy = tmp_dir / "live.bcsv"
        shutil.copyfile(live, copy)
        env = {"BCZOOM_STATE_DIR": str(tmp_dir / "state")}
        out = tmp_dir / "one.bcsv"
        self._run(["--project", str(copy), "library", "extract", "--set", "0", "-o", str(out)],
                  env_extra=env)
        merged = tmp_dir / "merged.bcsv"
        rep = self._json(["library", "merge", str(out), str(out), "-o", str(merged)], env_extra=env)
        assert rep["report"][1]["action"].startswith("renamed")
        assert lib.summarize(*lib.load(str(merged)))["set_count"] == 2
        # the live library was not touched
        assert copy.read_bytes() == live.read_bytes()

    def test_project_new_and_info(self, tmp_dir):
        f = str(tmp_dir / "p.bcp")
        env = {"BCZOOM_STATE_DIR": str(tmp_dir / "state")}
        self._run(["project", "new", "-o", f, "--model", str(SAMPLE_IFC)], env_extra=env)
        info = self._json(["--project", f, "project", "info"], env_extra=env)
        assert info["models"][0]["schema"] == "IFC4" and info["models"][0]["exists"]

    def test_nightshift_build_validate_guard(self, tmp_dir):
        f = str(tmp_dir / "night.json")
        env = {"BCZOOM_STATE_DIR": str(tmp_dir / "state")}
        self._run(["nightshift", "new", "-o", f, "--title", "Nightly"], env_extra=env)
        self._run(["--project", f, "nightshift", "open-models", str(SAMPLE_IFC)], env_extra=env)
        self._run(["--project", f, "nightshift", "connect", "--space", "x.bimcollab.com",
                   "--project-name", "P"], env_extra=env)
        self._run(["--project", f, "nightshift", "filter-issues", "--filter-type", "Clash"],
                  env_extra=env)
        self._run(["--project", f, "nightshift", "sync"], env_extra=env)
        self._run(["--project", f, "nightshift", "update-issues", "--comment", "auto"], env_extra=env)
        script = json.loads(Path(f).read_text(encoding="utf-8"))
        assert [a["type"] for a in script["actions"]] == [
            "OPEN_MODELS", "CONNECT_TO_PROJECT", "FILTER_ISSUES", "UPDATE_ISSUES",
            "SYNCHRONIZE_PROJECT"]
        assert self._json(["--project", f, "nightshift", "validate"], env_extra=env)["ok"]
        # Refuses to write to the cloud without --yes (and never launches Zoom here)
        res = self._run(["--json", "--project", f, "nightshift", "run"], check=False, env_extra=env)
        assert res.returncode != 0 and "--yes" in res.stdout

    def test_schedule_dry_run(self, tmp_dir):
        f = str(tmp_dir / "night.json")
        env = {"BCZOOM_STATE_DIR": str(tmp_dir / "state")}
        self._run(["nightshift", "new", "-o", f, "--title", "N"], env_extra=env)
        self._run(["--project", f, "nightshift", "open-models", str(SAMPLE_IFC)], env_extra=env)
        d = self._json(["--dry-run", "--project", f, "nightshift", "schedule", "t1", "--at",
                        "02:30"], env_extra=env)
        assert d["dry_run"] and "-robotscript=" in " ".join(d["command"])
        assert "BIMcollab Zoom.exe" in " ".join(d["command"])


# ---------------------------------------------------------------------------
# True backend: run a Nightshift script with the real Zoom
# ---------------------------------------------------------------------------

@pytest.mark.gui
class TestNightshiftRealZoom:
    CLI_BASE = _resolve_cli("cli-anything-bczoom")

    def test_open_models_run(self, tmp_dir):
        assert not backend.running_instances(), \
            "Close BIMcollab Zoom first: OPEN_MODELS would replace the loaded models."
        f = str(tmp_dir / "probe.json")
        env = dict(os.environ, BCZOOM_STATE_DIR=str(tmp_dir / "state"))
        run = lambda a: subprocess.run(self.CLI_BASE + a, capture_output=True, text=True,
                                       encoding="utf-8", env=env)
        assert run(["nightshift", "new", "-o", f, "--title", "bczoom-e2e"]).returncode == 0
        assert run(["--project", f, "nightshift", "open-models", str(SAMPLE_IFC)]).returncode == 0
        started = time.time()
        res = run(["--json", "--project", f, "nightshift", "run", "--timeout", "600"])
        print(f"\n  stdout: {res.stdout[-2000:]}\n  stderr: {res.stderr[-1000:]}")
        data = json.loads(res.stdout.strip().splitlines()[0] if res.stdout.strip().startswith('{"ok": false')
                          else res.stdout)
        assert data.get("exit_code") == 0, data
        assert not data.get("timed_out")
        logs = backend.list_logs(since=started)
        assert logs, f"No Nightshift log written in {backend.nightshift_log_dir()}"
        parsed = backend.parse_log(logs[0]["path"])
        assert parsed["ok"] and parsed["actions"][0]["type"] == "OPEN_MODELS", parsed
        print(f"  Nightshift log: {logs[0]['path']} ({logs[0]['size']:,} bytes)")
