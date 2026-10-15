# BCZOOM.md — SOP: BIMcollab Zoom harness

## Phase 1 — Analysis (closed-source target)

No source code is available, so analysis was done on the installed product
(BIMcollab Zoom 9.5.3, `C:\Program Files\KUBUS\BIMcollab Zoom`).

| Finding | Evidence |
|---|---|
| Only automation entry point is **Nightshift**: `"BIMcollab Zoom.exe" -robotscript=<json>` | `robotscript` string in `BIMcollab.Zoom.GUI.dll`; BIMcollab help center |
| Nightshift actions = exactly 7: OPEN_MODELS, ADD_TO_PROJECT, CONNECT_TO_PROJECT, FILTER_ISSUES, UPDATE_ISSUES, RUN_AND_SHARE_LISTS, SYNCHRONIZE_PROJECT | string table of `BIMcollab.Zoom.Core.dll` (no hidden actions) |
| Other switches: `-nosplash`, `-noscaling` | `BIMcollab.Zoom.GUI.dll` |
| `BIMcollab.ZoomCLI.exe` is only a support tool (`!report`, `!reset`) | running it |
| Nightshift logs: `%APPDATA%\KUBUS\BIMcollab Zoom\Nightshift` | help center |
| Libraries: `smartviews.bcsv`, `clashrules.bcr`, `lists.bcl`, `smartproperties.bcsp` in `%APPDATA%\KUBUS\BIMcollab Zoom` | file system |
| Format: UTF-8, CRLF, 4-space indent, **several root elements** (header + body), no self-closing tags, raw C0 control chars possible | byte analysis; 18 real files round-trip byte-identical |
| Rule enums (27 conditions, 12 actions, 3 explode modes, units) | `BIMcollab.Zoom.Core.dll` + corpus statistics |

### GUI → CLI mapping

| Zoom GUI | CLI |
|---|---|
| Smart Views panel: new set / view / rule, colours | `library add-set`, `smartview add`, `smartview add-rule`, `color-by-values` |
| Clash Rules panel: source/target sets, tolerances | `clash add`, `clash add-rule --side`, `clash set` |
| Import/Export sets | `library extract`, `library merge` (files import in Zoom) |
| Open/Save project | `project new/info/add-model/relink/open` |
| Nightshift + Task Scheduler | `nightshift new … run / schedule` |

## Phase 2 — Architecture

* Document = native file passed with `--project` (.bcsv/.bcr/.bcl/.bcsp/.bcp/.json).
* `core/zoomxml.py` multi-root parser/serializer (byte-exact).
* `core/library.py` generic set/item model for the 4 library kinds.
* `core/rules.py` rule / smart view / clash rule builders with validated enums.
* `core/project.py` .bcp + IFC STEP header probe.
* `core/nightshift.py` script model + validation; `utils/zoom_backend.py` runs the real exe.
* `core/session.py` snapshots, undo/redo persisted in `%LOCALAPPDATA%\bczoom\history`.
* Safety: no run while Zoom is open (unless `--allow-running`); cloud-writing
  scripts need `--yes`; `schedule`/`unschedule` honour `--dry-run`.

## Not possible with Zoom (documented, not faked)

Headless clash execution, BCF/IFC/list export, screenshots, issue creation
outside BIMcollab sync. A later harness could reach BIMcollab Nexus issues via
the BCF-API (REST) for those.
