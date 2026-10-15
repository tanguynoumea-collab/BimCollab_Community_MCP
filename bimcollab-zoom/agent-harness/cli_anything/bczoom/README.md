# cli-anything-bczoom

Agent-usable CLI for **BIMcollab Zoom** (KUBUS), built with the
[CLI-Anything](https://github.com/HKUDS/CLI-Anything) methodology.

Zoom is closed-source and exposes no API. This harness uses the two real
interfaces it has:

1. **Native files** — Smart Views `.bcsv`, Clash Rules `.bcr`, Lists `.bcl`,
   Smart Properties `.bcsp`, projects `.bcp`. Read and written byte-for-byte in
   Zoom's own format, so they import straight into Zoom.
2. **Nightshift** — `BIMcollab Zoom.exe -robotscript=<script.json>`, Zoom's
   built-in automation (open models, connect to a BIMcollab project, filter/update
   issues, run & share lists, synchronize).

## Requirements

* Windows, Python ≥ 3.10
* BIMcollab Zoom ≥ 5.2 with an active licence (tested with 9.5)
  (https://www.bimcollab.com), or set `BCZOOM_EXE` to the executable
* Cloud Nightshift actions: the project must be in Zoom's recent list with saved
  credentials ("Remember me") and you need edit rights

## Install

```bash
cd bimcollab-zoom/agent-harness
pip install -e .
cli-anything-bczoom --help
```

## Usage

```bash
cli-anything-bczoom                       # REPL
cli-anything-bczoom --json app info       # real Zoom: exe, version, running PIDs

# Smart views
cli-anything-bczoom library new smartviews -o qa.bcsv
cli-anything-bczoom --project qa.bcsv smartview add --set "QA" --title "Murs porteurs" --create-set
cli-anything-bczoom --project qa.bcsv smartview add-rule --set QA --view "Murs porteurs" --ifc-type Wall
cli-anything-bczoom --project qa.bcsv smartview add-rule --set QA --view "Murs porteurs" \
    --ifc-type Wall --prop "Load Bearing" --condition BooleanTrue --action SetColored --color "#FF8800"
cli-anything-bczoom --project qa.bcsv smartview color-by-values --set QA --title "Portes" \
    --ifc-type Door --prop Type --values "P1,P2,P3"
cli-anything-bczoom --project qa.bcsv library validate

# Clash rules
cli-anything-bczoom library new clashrules -o synth.bcr
cli-anything-bczoom --project synth.bcr clash add --set Synthèse --title "CVC vs STR" \
    --min-distance 0.05 --no-same-model --create-set
cli-anything-bczoom --project synth.bcr clash add-rule --set 0 --rule 0 --side source --ifc-type "Duct Segment"
cli-anything-bczoom --project synth.bcr clash add-rule --set 0 --rule 0 --side target --ifc-type Beam

# Projects and IFC
cli-anything-bczoom ifc probe model.ifc
cli-anything-bczoom project new -o coord.bcp --model ARC.ifc --model STR.ifc
cli-anything-bczoom --project coord.bcp project relink --search "D:/Maquettes"
cli-anything-bczoom --project coord.bcp project open          # opens the GUI

# Nightshift
cli-anything-bczoom nightshift new -o nightly.json --title Nightly
cli-anything-bczoom --project nightly.json nightshift open-models ARC.ifc STR.ifc
cli-anything-bczoom --project nightly.json nightshift connect --space acme.bimcollab.com --project-name "Projet X"
cli-anything-bczoom --project nightly.json nightshift update-issues --comment "auto"
cli-anything-bczoom --project nightly.json nightshift sync
cli-anything-bczoom --project nightly.json nightshift validate
cli-anything-bczoom --project nightly.json nightshift run --yes          # real Zoom
cli-anything-bczoom --project nightly.json nightshift schedule nightly --at 02:00
cli-anything-bczoom nightshift logs --read
```

Global flags: `--json` (machine output), `--project <file>` (document),
`--dry-run` (do not save). Sets/items are addressed by title, GUID or index.
`session undo|redo|history` works across invocations (history in
`%LOCALAPPDATA%\bczoom\history`, never next to your files).

### Using the files in Zoom

Files written by the CLI are standard Zoom exports: import them in Zoom
(Smart Views / Clash Rules / Lists panel ▸ *Import*). The live libraries in
`%APPDATA%\KUBUS\BIMcollab Zoom` can be read (`app paths`), but edit a copy
while Zoom is open: Zoom rewrites them on exit.

## Limits (what Zoom does not expose)

* No headless clash run, BCF export, screenshot or list export: Nightshift only
  offers its 7 actions. Clash results come back through BIMcollab issues
  (`UPDATE_ISSUES` + `SYNCHRONIZE_PROJECT`).
* Nightshift opens a Zoom window and needs an interactive Windows session.
* `nightshift run` refuses to start if Zoom is already open (`OPEN_MODELS`
  replaces the loaded models) unless `--allow-running`, and refuses scripts that
  write to the cloud unless `--yes`.

## Tests

```bash
CLI_ANYTHING_FORCE_INSTALLED=1 python -m pytest cli_anything/bczoom/tests -v -s
python -m pytest cli_anything/bczoom/tests -m gui -s     # real Nightshift run, Zoom must be closed
```
