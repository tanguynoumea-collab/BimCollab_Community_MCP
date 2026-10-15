---
name: cli-anything-bczoom
description: >-
  Drive BIMcollab Zoom (IFC model checking) from the command line: create/edit Smart Views
  (.bcsv), Clash Rules (.bcr), Lists (.bcl), Smart Properties (.bcsp) and projects (.bcp) in
  Zoom's native format, probe IFC headers, and build/run/schedule Nightshift automation
  scripts with the real Zoom. Use when asked to prepare Zoom smart views or clash rules,
  set up a Zoom coordination project, or automate Zoom/BIMcollab issue updates.
---

# cli-anything-bczoom

Executable: `.venv\Scripts\cli-anything-bczoom.exe` (repository root) — created by `install.ps1`;
or `cli-anything-bczoom` when that venv is active. Windows only; BIMcollab Zoom 9.x installed.

## Rules for agents

* Always pass `--json` and read `ok` / `error` in the output. Exit code ≠ 0 means failure.
* Edit **copies**, never `%APPDATA%\KUBUS\BIMcollab Zoom\*.bcsv|bcr|bcl|bcsp` while Zoom is open.
* Inspect before mutating: `library info`, `library items --set S`, `library show --set S --item I`.
* **Zoom already open (the usual case): never start `BIMcollab Zoom.exe` again** (it opens a second,
  empty instance). After writing a smart view file, finish with
  `live import FILE --show` so the user sees the result in their session without any clicks;
  `live screenshot` can then capture the result for the answer. Menu labels follow Zoom's UI
  language (French here): check them with `live menus`. Live commands move the real mouse for a
  moment (Telerik ignores UIA clicks): desktop must be unlocked.
* Zoom import never overwrites: a set with an existing title comes in as "Title (1)". To replace,
  `live delete-set` then `live import`. Zoom's window title gives only the model name: several
  versions of the same IFC may exist on disk, so check the file really is the one loaded (e.g.
  compare type names) before counting. In smart views, stairs are IFC type `Stair` (flights and
  landings included); `Summary` / `Type` holds the type name.
* A List's filter is a full Smart View: put the requested colour presentation (SetColored,
  AddSetColored, SetAutoColored, transparency) directly in the list's filter rules. Smart
  Properties are the recommended way to channel information Zoom lacks natively.
* Lists (`.bcl`, bim-assistant `zoom_list_create`) key properties **by name**: NetVolume from
  several Qto_* psets collide (only the last is read). Use a Smart Property (`.bcsp`,
  `zoom_smartproperty_create`, MapValue from each source) to get one column, import it live
  (`live import x.bcsp`). IFC4 quantities live in `Qto_<Class>BaseQuantities`, not `BaseQuantities`.
* Zoom's `Material Name` is the occurrence material only: Revit multi-piece elements (`Name:…:2`)
  or badly set types can lack it. Cross-check list totals against the IFC and add a type-name
  rule when needed.
* **Smart Views and Lists use generic rules, never element-by-element.** Filter by IFC type +
  property (type name, family, pset value, storey, material…), not by enumerating element names,
  GUIDs or IDs: such rules break at every model update. First find the discriminating property
  (bim-assistant `ifc_properties_catalog` / `ifc_distinct_values`); colour by value with
  `smartview color-by-values`; keep the rule count minimal (one rule per value, `Or`/`Contains`
  on a shared root). If no property separates the groups, tell the user and suggest adding a
  parameter in the authoring tool; per-element rules are a last resort, stated explicitly.
* Sets/items accept title, GUID or 0-based index. Rules are evaluated top-down in Zoom.
* Mutations auto-save; add `--dry-run` to preview. `session undo` reverts the last change (works across calls).
* `nightshift run` starts the real Zoom: it refuses if Zoom is open (would replace loaded models)
  and refuses cloud-writing scripts without `--yes`. Ask the user before using `--yes` or `--allow-running`.

## Commands

| Group | Commands |
|---|---|
| `app` | `info` (exe, version, running PIDs), `launch [files]`, `paths` (live libraries) |
| `live` | Acts on the **already open** Zoom session (UI Automation, needs `pywinauto` + `pillow`; never starts Zoom; all accept `--pid`): `sessions`, `import FILE [--kind] [--show [TITLE]]`, `export KIND -o F [--set S..] [--force]`, `show-view TITLE [--set S]`, `delete-set NAME [--kind]` (destructive, on request only), `run-list TITLE [--set S]`, `run-clash TITLE [--set S]`, `tab navigation\|smartviews\|clashrules\|lists\|issues`, `menus`, `menu "Naviguer > Zoomer pour ajuster"`, `tools`, `tool ZoomToFit\|ToggleTransparency\|AutoFloorplan…`, `search TEXT` (Zoom syntax: `/ps <val>` psets, `/vi` visible only, `/mv` My view, `/cs` case-sensitive), `screenshot -o F.png [--full]` |
| `library` | `new <smartviews\|clashrules\|lists\|smartproperties> -o F`, `info`, `items --set`, `show --set --item`, `search TEXT`, `add-set NAME`, `rename-set`, `remove-set`, `remove-item`, `extract --set.. -o`, `merge FILES -o [--on-conflict rename\|skip\|replace]`, `validate` |
| `smartview` | `add --set --title [--create-set] [--explode]`, `add-rule --set --view <rule opts>`, `remove-rule --index`, `takeoff --pset --prop`, `color-by-values --prop --values a,b,c [--colors "red;#00FF00"]` |
| `clash` | `add --set --title [--min-distance m] [--no-same-model] [--same-system] [--subparts] [--penetration-tolerance] [--volume-tolerance]`, `add-rule --set --rule --side source\|target <rule opts>`, `remove-rule`, `set` |
| `project` | `new -o F.bcp --model a.ifc..`, `info`, `add-model`, `remove-model`, `relink --search DIR`, `open` (GUI) |
| `ifc` | `probe FILES` (schema, MVD, authoring tool) |
| `nightshift` | `new -o F.json --title`, `open-models FILES [--add]`, `connect --space --project-name`, `filter-issues [...]`, `update-issues [--comment]`, `run-lists [--list-set]`, `sync`, `remove-action I`, `show`, `validate`, `command`, `run [--timeout] [--yes]`, `logs [--read]`, `schedule NAME --at HH:MM [--frequency]`, `tasks`, `unschedule NAME` |
| `session` | `status`, `undo`, `redo`, `history`, `save [-o]` |

Rule options: `--ifc-type` (Zoom names: Wall, Door, Duct Segment, Building Story, Any…),
`--prop`, `--pset` (Summary | Location | Pset name), `--condition` (Is, IsNot, Contains,
DoesNotContain, StartsWith, EndsWith, Or, Nor, StringIsDefined, StringIsNotDefined, Equals,
NotEquals, Less, Greater, LessEqual, GreaterEqual, NumericIsDefined, BooleanTrue, BooleanFalse,
…), `--value`, `--action` (Add, Remove, RemoveOthers, AddSetColored, SetColored, SetAutoColored,
AddSetTransparent, SetTransparent, SetOpaque), `--color` (red | #RRGGBB[AA] | R,G,B[,A]).
Without `--prop` the rule is a pure IFC-type filter.

## Examples

```bash
B=cli-anything-bczoom
$B --json library new smartviews -o qa.bcsv
$B --json --project qa.bcsv smartview add --set QA --title "Murs porteurs" --create-set
$B --json --project qa.bcsv smartview add-rule --set QA --view "Murs porteurs" --ifc-type Wall
$B --json --project qa.bcsv smartview add-rule --set QA --view "Murs porteurs" --ifc-type Wall \
   --prop "Load Bearing" --condition BooleanTrue --action SetColored --color "#FF8800"
$B --json --project qa.bcsv library validate

$B --json library new clashrules -o synth.bcr
$B --json --project synth.bcr clash add --set Synthese --title "CVC vs STR" --min-distance 0.05 --create-set
$B --json --project synth.bcr clash add-rule --set 0 --rule 0 --side source --ifc-type "Duct Segment"
$B --json --project synth.bcr clash add-rule --set 0 --rule 0 --side target --ifc-type Beam

$B --json project new -o coord.bcp --model ARC.ifc --model STR.ifc
$B --json nightshift new -o nightly.json --title Nightly
$B --json --project nightly.json nightshift open-models ARC.ifc STR.ifc
$B --json --project nightly.json nightshift validate
```

Produced files import in Zoom via the Smart Views / Clash Rules / Lists panel ▸ Import.

## Limits

Zoom exposes no headless clash run, BCF/list export or screenshots: only the 7 Nightshift
actions. Nightshift needs an interactive Windows session and an active Zoom licence.
