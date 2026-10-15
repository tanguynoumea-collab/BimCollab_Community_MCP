# TEST.md — cli-anything-bczoom

## Part 1 — Test plan

### Test inventory

| File | Planned | Scope |
|------|---------|-------|
| `test_core.py` | ~45 unit tests | Synthetic data only, no Zoom needed |
| `test_full_e2e.py` | ~16 E2E tests | Real Zoom files on this machine, real IFC, installed CLI, real Zoom (Nightshift) |

### Unit test plan (`test_core.py`)

| Module | What is tested | Edge cases |
|--------|----------------|------------|
| `core/zoomxml.py` (6) | multi-root parse, Zoom-style serialization, CRLF/UTF-8, no self-closing tags, escaping, control-char protection | missing body, invalid XML, 0x1F in a title |
| `core/library.py` (12) | new library per kind, add/find/remove set & item by title/GUID/index, extract, merge (rename/skip/replace), validate, summarize | duplicate set name, unknown set, mixed kinds, bad colour channel, duplicate GUID |
| `core/rules.py` (12) | type-only rule, property rule, valueless/state conditions, numeric formatting, colours (name/hex/RGB/RGBA), smart view & clash rule structure | invalid action/condition/unit, value missing, non-numeric value for DoubleValue, colour on non-colour action |
| `core/nightshift.py` (9) | each action builder, SYNCHRONIZE kept last, filter defaults omitted + priority kept, validation rules, describe() cloud flags | backslashes, missing fields, bad deadline, file name with spaces |
| `core/project.py` (4) | IFC header probe, new .bcp, add/remove model, relink | non-IFC file, duplicate model |
| `core/session.py` (4) | snapshot/undo/redo, history persisted across sessions, history discarded if file changed externally | undo on empty stack |

### E2E test plan (`test_full_e2e.py`)

* **Native format fidelity** – every library and `.bcp` in `%APPDATA%\KUBUS\BIMcollab Zoom`
  (copied to a temp dir) is parsed and re-serialized **byte-identical**. This is the
  proof that files the CLI writes are in Zoom's exact format.
* **Real IFC** – probe `fixtures/sample.ifc` (Revit IFC4 export) and build a `.bcp` from it.
* **Installed CLI subprocess** (`_resolve_cli("cli-anything-bczoom")`): `--help`,
  `app info --json` (real exe + version), library new/add/validate, undo across processes,
  `--dry-run` leaves the file untouched, clash rule workflow, Nightshift build/validate,
  cloud-write guard, `schedule --dry-run`.
* **True backend (marker `gui`)** – run a Nightshift script (`OPEN_MODELS` on the sample
  IFC, no cloud action) with the real `BIMcollab Zoom.exe -robotscript=`; verify the exit
  code and that a new Nightshift log was written. Requires Zoom to be **closed** (an
  open session would have its models replaced), so it is selected explicitly:
  `pytest -m gui`.

### Realistic workflow scenarios

1. **QA smart view set** — *Simulates*: a BIM manager preparing a model-checking set.
   *Ops*: new .bcsv → set → view "Murs porteurs" (Add Wall + SetColored LoadBearing) →
   `color-by-values` doors by type → validate → undo/redo. *Verified*: rule XML, colours,
   GUIDs, validation clean, byte-stable re-parse.
2. **Clash matrix** — *Simulates*: MEP vs structure coordination. *Ops*: new .bcr →
   rule "CVC vs STR" min distance 5 cm, not same model → source Duct Segment / target Beam →
   `clash set`. *Verified*: SOURCESET/TARGETSET rules, tolerance flags.
3. **Library housekeeping** — *Simulates*: sharing one set with a colleague. *Ops*: copy
   of the live smartviews library → extract one set → merge with another file.
   *Verified*: output parses, set counts, conflict renaming.
4. **Nightly run** — *Simulates*: overnight model update. *Ops*: project from IFC →
   Nightshift script open-models (+ connect/filter/update/sync in dry form) → validate →
   schedule (dry-run) → real run (`-m gui`).

## Part 2 — Test results

Run: `CLI_ANYTHING_FORCE_INSTALLED=1 python -m pytest cli_anything/bczoom/tests -v --tb=no` (2026-10-14, Windows 10, Python 3.14.4, BIMcollab Zoom 9.5.3)

```
platform win32 -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0 -- <repo>\bimcollab-zoom\agent-harness\.venv\Scripts\python.exe
collecting ... collected 74 items / 1 deselected / 73 selected
cli_anything/bczoom/tests/test_core.py::TestZoomXml::test_parse_multi_root PASSED [  1%]
cli_anything/bczoom/tests/test_core.py::TestZoomXml::test_roundtrip_identical_lf PASSED [  2%]
cli_anything/bczoom/tests/test_core.py::TestZoomXml::test_bytes_are_crlf_utf8 PASSED [  4%]
cli_anything/bczoom/tests/test_core.py::TestZoomXml::test_no_self_closing_and_escaping PASSED [  5%]
cli_anything/bczoom/tests/test_core.py::TestZoomXml::test_control_char_roundtrip PASSED [  6%]
cli_anything/bczoom/tests/test_core.py::TestZoomXml::test_invalid PASSED [  8%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_new_library_each_kind[smartviews] PASSED [  9%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_new_library_each_kind[clashrules] PASSED [ 10%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_new_library_each_kind[lists] PASSED [ 12%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_new_library_each_kind[smartproperties] PASSED [ 13%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_add_and_find_set PASSED [ 15%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_duplicate_set_rejected PASSED [ 16%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_unknown_set_lists_available PASSED [ 17%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_smartproperties_set_uses_name PASSED [ 19%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_remove_item PASSED [ 20%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_extract PASSED [ 21%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_merge_rename_skip_replace PASSED [ 23%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_merge_mixed_kinds_rejected PASSED [ 24%]
cli_anything/bczoom/tests/test_core.py::TestLibrary::test_validate_flags_bad_color_and_dup_guid PASSED [ 26%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_type_only_rule PASSED [ 27%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_property_rule_infers_types PASSED [ 28%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_pset_property_type PASSED [ 30%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_valueless_condition_writes_empty_value PASSED [ 31%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_state_condition PASSED [ 32%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_numeric_value_format PASSED [ 34%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_numeric_value_rejects_text PASSED [ 35%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_value_required PASSED [ 36%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_colors[red-expected0] PASSED [ 38%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_colors[#00FF0080-expected1] PASSED [ 39%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_colors[1,2,3-expected2] PASSED [ 41%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_colors[1,2,3,4-expected3] PASSED [ 42%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_bad_inputs PASSED [ 43%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_smartview_structure PASSED [ 45%]
cli_anything/bczoom/tests/test_core.py::TestRules::test_clashrule_structure PASSED [ 46%]
cli_anything/bczoom/tests/test_core.py::TestNightshift::test_open_models_forward_slashes PASSED [ 47%]
cli_anything/bczoom/tests/test_core.py::TestNightshift::test_add_to_project PASSED [ 49%]
cli_anything/bczoom/tests/test_core.py::TestNightshift::test_missing_model PASSED [ 50%]
cli_anything/bczoom/tests/test_core.py::TestNightshift::test_sync_kept_last PASSED [ 52%]
cli_anything/bczoom/tests/test_core.py::TestNightshift::test_filter_defaults_omitted_priority_kept PASSED [ 53%]
cli_anything/bczoom/tests/test_core.py::TestNightshift::test_filter_bad_deadline PASSED [ 54%]
cli_anything/bczoom/tests/test_core.py::TestNightshift::test_validate_errors PASSED [ 56%]
cli_anything/bczoom/tests/test_core.py::TestNightshift::test_describe_cloud_flags PASSED [ 57%]
cli_anything/bczoom/tests/test_core.py::TestNightshift::test_save_rejects_spaces PASSED [ 58%]
cli_anything/bczoom/tests/test_core.py::TestProject::test_probe_ifc PASSED [ 60%]
cli_anything/bczoom/tests/test_core.py::TestProject::test_probe_rejects_non_ifc PASSED [ 61%]
cli_anything/bczoom/tests/test_core.py::TestProject::test_new_add_remove PASSED [ 63%]
cli_anything/bczoom/tests/test_core.py::TestProject::test_relink PASSED  [ 64%]
cli_anything/bczoom/tests/test_core.py::TestSession::test_undo_redo PASSED [ 65%]
cli_anything/bczoom/tests/test_core.py::TestSession::test_undo_empty PASSED [ 67%]
cli_anything/bczoom/tests/test_core.py::TestSession::test_history_persists_across_sessions PASSED [ 68%]
cli_anything/bczoom/tests/test_core.py::TestSession::test_history_dropped_after_external_edit PASSED [ 69%]
cli_anything/bczoom/tests/test_full_e2e.py::TestNativeFidelity::test_zoom_data_dir_has_libraries PASSED [ 71%]
cli_anything/bczoom/tests/test_full_e2e.py::TestNativeFidelity::test_byte_identical_roundtrip[bdc9acfb-0f13-4024-9325-e219fd182552.bcp] PASSED [ 72%]
cli_anything/bczoom/tests/test_full_e2e.py::TestNativeFidelity::test_byte_identical_roundtrip[clashrules.bcr] PASSED [ 73%]
cli_anything/bczoom/tests/test_full_e2e.py::TestNativeFidelity::test_byte_identical_roundtrip[clashrules.v5.bcr] PASSED [ 75%]
cli_anything/bczoom/tests/test_full_e2e.py::TestNativeFidelity::test_byte_identical_roundtrip[lists.bcl] PASSED [ 76%]
cli_anything/bczoom/tests/test_full_e2e.py::TestNativeFidelity::test_byte_identical_roundtrip[lists.v4.bcl] PASSED [ 78%]
cli_anything/bczoom/tests/test_full_e2e.py::TestNativeFidelity::test_byte_identical_roundtrip[smartproperties.bcsp] PASSED [ 79%]
cli_anything/bczoom/tests/test_full_e2e.py::TestNativeFidelity::test_byte_identical_roundtrip[smartproperties.v7.bcsp] PASSED [ 80%]
cli_anything/bczoom/tests/test_full_e2e.py::TestNativeFidelity::test_byte_identical_roundtrip[smartviews.bcsv] PASSED [ 82%]
cli_anything/bczoom/tests/test_full_e2e.py::TestNativeFidelity::test_byte_identical_roundtrip[smartviews.v7.bcsv] PASSED [ 83%]
cli_anything/bczoom/tests/test_full_e2e.py::TestNativeFidelity::test_live_libraries_validate PASSED [ 84%]
cli_anything/bczoom/tests/test_full_e2e.py::TestRealIfc::test_probe_sample PASSED [ 86%]
cli_anything/bczoom/tests/test_full_e2e.py::TestRealIfc::test_project_from_ifc_parses_back PASSED [ 87%]
cli_anything/bczoom/tests/test_full_e2e.py::TestCLISubprocess::test_help PASSED [ 89%]
cli_anything/bczoom/tests/test_full_e2e.py::TestCLISubprocess::test_app_info_real_exe PASSED [ 90%]
cli_anything/bczoom/tests/test_full_e2e.py::TestCLISubprocess::test_smartview_workflow PASSED [ 91%]
cli_anything/bczoom/tests/test_full_e2e.py::TestCLISubprocess::test_dry_run_does_not_write PASSED [ 93%]
cli_anything/bczoom/tests/test_full_e2e.py::TestCLISubprocess::test_clash_workflow PASSED [ 94%]
cli_anything/bczoom/tests/test_full_e2e.py::TestCLISubprocess::test_extract_and_merge_live_copy PASSED [ 95%]
cli_anything/bczoom/tests/test_full_e2e.py::TestCLISubprocess::test_project_new_and_info PASSED [ 97%]
cli_anything/bczoom/tests/test_full_e2e.py::TestCLISubprocess::test_nightshift_build_validate_guard PASSED [ 98%]
cli_anything/bczoom/tests/test_full_e2e.py::TestCLISubprocess::test_schedule_dry_run PASSED [100%]
====================== 73 passed, 1 deselected in 15.03s ======================
```

**Summary:** 73 passed / 0 failed, 1 deselected (≈15 s).

**Follow-up runs (same day)**
* `pytest -m gui` after the user closed Zoom: **1 passed** (31 s). Real
  `BIMcollab Zoom.exe -robotscript=` run: exit code 0 in 28.6 s, log
  `Nightshift\<timestamp>_RobotScriptOutput.JSON` with `status: OK` and
  `OPEN_MODELS execution_status: OK` (1.9 s).
* That run showed the log is structured JSON; `parse_log` now reads it (2 unit tests added,
  and the gui test asserts the parsed status).
* Full suite `pytest -m "gui or not gui"`: **76 passed / 0 failed** (48.6 s).

**Coverage notes**
* Cloud actions (CONNECT/FILTER/UPDATE/RUN_AND_SHARE_LISTS/SYNCHRONIZE) are built and
  validated but never executed by the suite (they write to the BIMcollab project).
* Import of CLI-written files through Zoom's GUI is not automated (Zoom has no API for it);
  fidelity is proven by byte-identical round-trips of 9 live Zoom files.
