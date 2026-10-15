"""cli-anything-bczoom: agent-usable CLI for BIMcollab Zoom.

Two layers:
  * data layer  - read/write Zoom's native files (smart views .bcsv, clash rules
                  .bcr, lists .bcl, smart properties .bcsp, projects .bcp)
  * backend     - the real BIMcollab Zoom via Nightshift (-robotscript=) and GUI launch

Usage:
  cli-anything-bczoom                         # REPL
  cli-anything-bczoom --json app info
  cli-anything-bczoom --project my.bcsv smartview add-rule --set S --view V --ifc-type Wall
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import click

from cli_anything.bczoom import __version__
from cli_anything.bczoom.core import library as lib
from cli_anything.bczoom.core import nightshift as ns
from cli_anything.bczoom.core import project as prj
from cli_anything.bczoom.core import rules
from cli_anything.bczoom.core import zoomxml as zx
from cli_anything.bczoom.core.session import get_session
from cli_anything.bczoom.utils import zoom_backend as backend

_repl_mode = False


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _json_mode() -> bool:
    ctx = click.get_current_context(silent=True)
    while ctx is not None:
        if ctx.obj and "json" in ctx.obj:
            return ctx.obj["json"]
        ctx = ctx.parent
    return False


def emit(data, human: str | None = None) -> None:
    if _json_mode():
        click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        click.echo(human if human is not None else _pretty(data))


def _pretty(data, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(data, dict):
        lines = []
        for k, v in data.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(_pretty(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {v}")
        return "\n".join(lines)
    if isinstance(data, list):
        lines = []
        for v in data:
            if isinstance(v, dict):
                lines.append(f"{pad}- " + ", ".join(f"{k}={w}" for k, w in v.items()
                                                    if not isinstance(w, (dict, list))))
            else:
                lines.append(f"{pad}- {v}")
        return "\n".join(lines)
    return f"{pad}{data}"


def fail(message: str, code: int = 1):
    if _json_mode():
        click.echo(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    else:
        click.echo(f"Error: {message}", err=True)
    if _repl_mode:
        raise click.exceptions.Exit(code)
    sys.exit(code)


def _author() -> str:
    return os.environ.get("BCZOOM_AUTHOR", "")


def _sess(expected: str | None = None):
    s = get_session()
    if not s.has_project():
        fail("No document open. Pass --project <file> (or 'open <file>' in the REPL).")
    if expected and s.doc_type != expected:
        fail(f"This command needs a {expected} document; current is {s.doc_type} ({s.project_path})")
    return s


def _lib_doc(expected_kind: str | None = None):
    s = _sess("library")
    kind = lib.detect_kind(s.obj, s.project_path)
    if expected_kind and kind.name != expected_kind:
        fail(f"This command needs a {expected_kind} library; current is {kind.name}")
    return s, s.obj, kind


def _guard(fn):
    """Turn domain errors into clean CLI failures."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (lib.LibraryError, ns.NightshiftError, zx.ZoomFormatError,
                backend.ZoomBackendError, FileNotFoundError, ValueError, RuntimeError) as exc:
            fail(str(exc))
    return wrapper


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group(invoke_without_command=True)
@click.option("--json", "use_json", is_flag=True, help="Machine-readable JSON output")
@click.option("--project", "project_path", type=str, default=None,
              help="Document to work on: .bcsv/.bcr/.bcl/.bcsp library, .bcp project or Nightshift .json")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Run the command without saving changes to disk")
@click.version_option(__version__, prog_name="cli-anything-bczoom")
@click.pass_context
def cli(ctx, use_json, project_path, dry_run):
    """Agent CLI for BIMcollab Zoom (smart views, clash rules, lists, projects, Nightshift)."""
    ctx.ensure_object(dict)
    ctx.obj["json"] = use_json
    ctx.obj["dry_run"] = dry_run
    if project_path:
        try:
            get_session().open(project_path)
        except (FileNotFoundError, ValueError, lib.LibraryError, zx.ZoomFormatError,
                ns.NightshiftError) as exc:
            fail(str(exc))
    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


@cli.result_callback()
@click.pass_context
def auto_save_on_exit(ctx, result, use_json, project_path, dry_run, **kwargs):
    """Auto-save the document after one-shot mutations (skipped with --dry-run / in REPL)."""
    if _repl_mode or dry_run:
        return
    s = get_session()
    if s.has_project() and s._modified and s.project_path:
        try:
            s.save_session()
        except Exception as exc:  # noqa: BLE001
            click.echo(f"Warning: auto-save failed: {exc}", err=True)


# ---------------------------------------------------------------------------
# app: the real Zoom
# ---------------------------------------------------------------------------

@cli.group()
def app():
    """Inspect and launch the installed BIMcollab Zoom."""


@app.command("info")
@_guard
def app_info():
    """Executable path, version, running instances, data folders."""
    emit(backend.info())


@app.command("launch")
@click.argument("files", nargs=-1)
@click.option("--splash/--no-splash", default=False, help="Show the splash screen")
@_guard
def app_launch(files, splash):
    """Open Zoom's GUI, optionally with .bcp/.ifc files."""
    res = backend.launch(list(files), nosplash=not splash)
    emit(res, f"Started BIMcollab Zoom (PID {res['pid']})")


@app.command("paths")
def app_paths():
    """Paths of the user's live Zoom libraries."""
    emit({"data_dir": str(lib.zoom_data_dir()), "libraries": lib.user_library_paths()})


# ---------------------------------------------------------------------------
# live: act on the Zoom session already open (UI Automation, no new process)
# ---------------------------------------------------------------------------

@cli.group()
def live():
    """Act on the BIMcollab Zoom window that is already open (never starts Zoom)."""


@live.command("sessions")
@_guard
def live_sessions():
    """Open Zoom windows (PID, title, loaded model)."""
    from cli_anything.bczoom.utils import zoom_live
    emit(zoom_live.sessions())


@live.command("import")
@click.argument("path")
@click.option("--pid", type=int, default=None, help="Zoom PID (default: the only window with a model)")
@click.option("--kind", type=click.Choice(["smartviews", "clashrules", "lists", "smartproperties"]), default=None,
              help="Default: from the file extension")
@click.option("--timeout", type=float, default=20)
@click.option("--show", "show", default=None, is_flag=False, flag_value="",
              help="Then display a smart view: --show alone = first view of the file, --show TITLE = that view")
@_guard
def live_import(path, pid, kind, timeout, show):
    """Import a .bcsv/.bcr/.bcl into the open Zoom session via its own Import menu."""
    from cli_anything.bczoom.utils import zoom_live
    res = zoom_live.import_library(path, pid=pid, kind=kind, timeout=timeout, show=show)
    if not res["ok"]:
        if _json_mode():
            click.echo(json.dumps(res, indent=2, ensure_ascii=False))
            sys.exit(1)
        fail(f"{res['error']} {res.get('dialogs')}")
    emit(res, f"Imported {res['file']} into Zoom PID {res['pid']} ({res['model']})"
         + (f"; showing '{res['shown']['view']}'" if res.get("shown") else ""))


@live.command("show-view")
@click.argument("view")
@click.option("--set", "set_name", default=None, help="Smart view set (needed if the title is ambiguous)")
@click.option("--root", default="Local", help="Tree root: Local (default) or the shared one")
@click.option("--pid", type=int, default=None)
@_guard
def live_show_view(view, set_name, root, pid):
    """Display a smart view in the open Zoom session."""
    from cli_anything.bczoom.utils import zoom_live
    res = zoom_live.show_smartview(view, set_name=set_name, pid=pid, root=root)
    emit(res, f"Showing '{res['view']}' ({res['set']}) in Zoom PID {res['pid']}")


@live.command("tab")
@click.argument("name", type=click.Choice(["navigation", "smartviews", "clashrules", "lists", "issues"]))
@click.option("--pid", type=int, default=None)
@_guard
def live_tab(name, pid):
    """Switch the left panel tab."""
    from cli_anything.bczoom.utils import zoom_live
    emit(zoom_live.open_tab(name, pid=pid))


@live.command("delete-set")
@click.argument("set_name")
@click.option("--kind", type=click.Choice(["smartviews", "clashrules", "lists"]), default="smartviews")
@click.option("--root", default="Local")
@click.option("--pid", type=int, default=None)
@_guard
def live_delete_set(set_name, kind, root, pid):
    """Delete a set and its content from the open session (confirms Zoom's prompt for that set only)."""
    from cli_anything.bczoom.utils import zoom_live
    emit(zoom_live.delete_set(set_name, kind=kind, pid=pid, root=root))


@live.command("run-list")
@click.argument("title")
@click.option("--set", "set_name", default=None)
@click.option("--root", default="Local")
@click.option("--pid", type=int, default=None)
@_guard
def live_run_list(title, set_name, root, pid):
    """Run a list in the open session (results open in Zoom's list grid)."""
    from cli_anything.bczoom.utils import zoom_live
    emit(zoom_live.run_list(title, set_name=set_name, pid=pid, root=root))


@live.command("run-clash")
@click.argument("title")
@click.option("--set", "set_name", default=None)
@click.option("--root", default="Local")
@click.option("--pid", type=int, default=None)
@_guard
def live_run_clash(title, set_name, root, pid):
    """Run a clash rule on the loaded models (results in the Conflits panel)."""
    from cli_anything.bczoom.utils import zoom_live
    emit(zoom_live.run_clash(title, set_name=set_name, pid=pid, root=root))


@live.command("export")
@click.argument("kind", type=click.Choice(["smartviews", "clashrules", "lists"]))
@click.option("-o", "--output", required=True, help="File to write (.bcsv/.bcr/.bcl)")
@click.option("--set", "set_refs", multiple=True, help="Keep only these sets (repeatable)")
@click.option("--force", is_flag=True, help="Overwrite the output file")
@click.option("--pid", type=int, default=None)
@_guard
def live_export(kind, output, set_refs, force, pid):
    """Export the session's library through Zoom's Export menu (optionally only some sets)."""
    from cli_anything.bczoom.utils import zoom_live
    res = zoom_live.export_library(kind, output, pid=pid, force=force, sets=list(set_refs) or None)
    if not res["ok"]:
        if _json_mode():
            click.echo(json.dumps(res, indent=2, ensure_ascii=False))
            sys.exit(1)
        fail(f"{res['error']} {res.get('dialogs')}")
    emit(res, f"Exported {kind} from Zoom PID {res['pid']} to {res['file']}")


@live.command("menus")
@click.option("--pid", type=int, default=None)
@_guard
def live_menus(pid):
    """List the main-menu commands (labels in Zoom's UI language)."""
    from cli_anything.bczoom.utils import zoom_live
    emit(zoom_live.list_menus(pid=pid))


@live.command("menu")
@click.argument("path")
@click.option("--pid", type=int, default=None)
@_guard
def live_menu(path, pid):
    """Invoke a main-menu command, e.g. "Naviguer > Zoomer pour ajuster"."""
    from cli_anything.bczoom.utils import zoom_live
    emit(zoom_live.menu(path, pid=pid))


@live.command("tools")
@click.option("--pid", type=int, default=None)
@_guard
def live_tools(pid):
    """List the visible toolbar commands (tb_*)."""
    from cli_anything.bczoom.utils import zoom_live
    emit(zoom_live.list_tools(pid=pid))


@live.command("tool")
@click.argument("name")
@click.option("--pid", type=int, default=None)
@_guard
def live_tool(name, pid):
    """Click a toolbar command, e.g. ZoomToFit, ToggleTransparency, AutoFloorplan."""
    from cli_anything.bczoom.utils import zoom_live
    emit(zoom_live.tool(name, pid=pid))


@live.command("search")
@click.argument("text")
@click.option("--pid", type=int, default=None)
@_guard
def live_search(text, pid):
    """Type in Zoom's search box and press Enter."""
    from cli_anything.bczoom.utils import zoom_live
    emit(zoom_live.search(text, pid=pid))


@live.command("screenshot")
@click.option("-o", "--output", required=True, help="PNG file")
@click.option("--full", is_flag=True, help="Whole Zoom window instead of the 3D view")
@click.option("--pid", type=int, default=None)
@_guard
def live_screenshot(output, full, pid):
    """Save a PNG of the 3D view of the open session."""
    from cli_anything.bczoom.utils import zoom_live
    emit(zoom_live.screenshot(output, pid=pid, full=full))


# ---------------------------------------------------------------------------
# library: generic set/item operations on any rule library
# ---------------------------------------------------------------------------

@cli.group()
def library():
    """Generic operations on rule libraries (.bcsv .bcr .bcl .bcsp)."""


@library.command("new")
@click.argument("kind", type=click.Choice(list(lib.KINDS)))
@click.option("-o", "--output", required=True, help="File to create")
@click.option("--force", is_flag=True, help="Overwrite an existing file")
@_guard
def library_new(kind, output, force):
    """Create an empty library file."""
    k = lib.KINDS[kind]
    if Path(output).suffix.lower() != k.ext:
        fail(f"A {kind} library must use the {k.ext} extension")
    if Path(output).exists() and not force:
        fail(f"{output} exists (use --force)")
    s = get_session()
    s.attach_new(output, lib.new_library(k))
    if not click.get_current_context().find_root().obj.get("dry_run"):
        s.save_session()
    emit({"ok": True, "path": s.project_path, "kind": kind}, f"Created {kind} library {s.project_path}")


@library.command("open")
@click.argument("path")
@_guard
def library_open(path):
    """Open a document in the current session (mostly for the REPL)."""
    get_session().open(path)
    emit(get_session().status(), f"Opened {get_session().project_path}")


@library.command("info")
@_guard
def library_info():
    """Summary: kind, version, sets and item counts."""
    _, doc, kind = _lib_doc()
    data = lib.summarize(doc, kind)
    if _json_mode():
        emit(data)
    else:
        click.echo(f"{kind.name}  (format v{data['format_version']}, {data['application_version']})")
        click.echo(f"{data['set_count']} sets, {data['item_count']} items")
        for s in data["sets"]:
            click.echo(f"  [{s['index']}] {s['title']}  ({s['items']})")


@library.command("items")
@click.option("--set", "set_ref", required=True, help="Set title, GUID or index")
@_guard
def library_items(set_ref):
    """List the items of one set."""
    _, doc, kind = _lib_doc()
    s = lib.find_set(doc, kind, set_ref)
    data = [lib.item_summary(it, kind, i) for i, it in enumerate(lib.items(s, kind))]
    emit({"set": lib.title(s, kind), "items": data})


@library.command("show")
@click.option("--set", "set_ref", required=True)
@click.option("--item", "item_ref", required=True)
@_guard
def library_show(set_ref, item_ref):
    """Full detail of one item (rules, colors, clash settings...)."""
    _, doc, kind = _lib_doc()
    it = lib.find_item(lib.find_set(doc, kind, set_ref), kind, item_ref)
    emit(lib.item_detail(it, kind))


@library.command("search")
@click.argument("text")
@_guard
def library_search(text):
    """Find items whose title or description contains TEXT (case-insensitive)."""
    _, doc, kind = _lib_doc()
    t = text.lower()
    hits = []
    for s in lib.sets(doc, kind):
        for i, it in enumerate(lib.items(s, kind)):
            if t in lib.title(it, kind).lower() or t in zx.text_of(it, "DESCRIPTION").lower():
                hits.append({"set": lib.title(s, kind), "index": i, "title": lib.title(it, kind)})
    emit({"query": text, "count": len(hits), "hits": hits})


@library.command("add-set")
@click.argument("name")
@click.option("--description", default="")
@_guard
def library_add_set(name, description):
    """Add an empty set."""
    s, doc, kind = _lib_doc()
    s.snapshot(f"add-set {name}")
    el = lib.add_set(doc, kind, name, description)
    emit({"ok": True, "set": name, "guid": zx.text_of(el, "GUID")}, f"Added set '{name}'")


@library.command("rename-set")
@click.option("--set", "set_ref", required=True)
@click.argument("new_name")
@_guard
def library_rename_set(set_ref, new_name):
    """Rename a set."""
    s, doc, kind = _lib_doc()
    el = lib.find_set(doc, kind, set_ref)
    s.snapshot(f"rename-set {set_ref} -> {new_name}")
    el.find(kind.title_tag).text = new_name
    lib.touch(el)
    emit({"ok": True, "set": new_name}, f"Renamed to '{new_name}'")


@library.command("remove-set")
@click.option("--set", "set_ref", required=True)
@_guard
def library_remove_set(set_ref):
    """Delete a whole set."""
    s, doc, kind = _lib_doc()
    lib.find_set(doc, kind, set_ref)
    s.snapshot(f"remove-set {set_ref}")
    name = lib.remove_set(doc, kind, set_ref)
    emit({"ok": True, "removed": name}, f"Removed set '{name}'")


@library.command("remove-item")
@click.option("--set", "set_ref", required=True)
@click.option("--item", "item_ref", required=True)
@_guard
def library_remove_item(set_ref, item_ref):
    """Delete one item from a set."""
    s, doc, kind = _lib_doc()
    lib.find_item(lib.find_set(doc, kind, set_ref), kind, item_ref)
    s.snapshot(f"remove-item {set_ref}/{item_ref}")
    name = lib.remove_item(doc, kind, set_ref, item_ref)
    emit({"ok": True, "removed": name}, f"Removed '{name}'")


@library.command("extract")
@click.option("--set", "set_refs", multiple=True, required=True, help="Repeatable")
@click.option("-o", "--output", required=True)
@_guard
def library_extract(set_refs, output):
    """Write selected sets to a new file (importable in Zoom)."""
    _, doc, kind = _lib_doc()
    if Path(output).suffix.lower() != kind.ext:
        fail(f"Output must use the {kind.ext} extension")
    out = lib.extract_sets(doc, kind, list(set_refs))
    n = zx.write_file(out, output)
    emit({"ok": True, "output": str(Path(output).resolve()), "sets": len(set_refs), "bytes": n},
         f"Wrote {len(set_refs)} set(s) to {output}")


@library.command("merge")
@click.argument("files", nargs=-1, required=True)
@click.option("-o", "--output", required=True)
@click.option("--on-conflict", type=click.Choice(["rename", "skip", "replace"]), default="rename")
@_guard
def library_merge(files, output, on_conflict):
    """Merge several libraries of the same kind into one file."""
    docs = [lib.load(f) for f in files]
    kinds = {k.name for _, k in docs}
    if len(kinds) != 1:
        fail(f"Files are of different kinds: {sorted(kinds)}")
    kind = docs[0][1]
    out, report = lib.merge([d for d, _ in docs], kind, on_conflict)
    n = zx.write_file(out, output)
    emit({"ok": True, "output": str(Path(output).resolve()), "bytes": n, "report": report})


@library.command("validate")
@_guard
def library_validate():
    """Structural checks before importing into Zoom."""
    _, doc, kind = _lib_doc()
    issues = lib.validate(doc, kind)
    errors = [i for i in issues if i["level"] == "error"]
    emit({"ok": not errors, "errors": len(errors), "warnings": len(issues) - len(errors),
          "issues": issues})
    if errors:
        fail(f"{len(errors)} error(s)")


# ---------------------------------------------------------------------------
# smartview
# ---------------------------------------------------------------------------

def _rule_options(fn):
    opts = [
        click.option("--ifc-type", default="Any", show_default=True,
                     help="IFC type as shown in Zoom (Wall, Door, Building Story, Any...)"),
        click.option("--prop", default=None, help="Property name (omit for a pure type filter)"),
        click.option("--pset", default=None, help="Property set (Summary, Location or a Pset name)"),
        click.option("--prop-type", type=click.Choice(rules.PROPERTY_TYPES), default=None),
        click.option("--value-type", type=click.Choice(rules.VALUE_TYPES), default=None),
        click.option("--unit", type=click.Choice(rules.UNITS), default="None"),
        click.option("--condition", type=click.Choice(rules.CONDITIONS), default=None),
        click.option("--value", default=None, help="Comparison value (Or/Nor: comma-separated)"),
        click.option("--action", "rule_action", type=click.Choice(rules.ACTIONS), default="Add",
                     show_default=True),
        click.option("--color", default=None, help="red | #RRGGBB[AA] | R,G,B[,A]"),
    ]
    for o in reversed(opts):
        fn = o(fn)
    return fn


def _build_rule(kw) -> "rules.ET.Element":
    return rules.build_rule(ifc_type=kw["ifc_type"], prop=kw["prop"], pset=kw["pset"],
                            prop_type=kw["prop_type"], value_type=kw["value_type"],
                            unit=kw["unit"], condition=kw["condition"], value=kw["value"],
                            action=kw["rule_action"], color=kw["color"])


@cli.group()
def smartview():
    """Create and edit Smart Views (.bcsv)."""


@smartview.command("add")
@click.option("--set", "set_ref", required=True, help="Target set (created if --create-set)")
@click.option("--title", required=True)
@click.option("--description", default="")
@click.option("--explode", type=click.Choice(rules.EXPLODE_MODES), default="KeepParentsAndChildren")
@click.option("--create-set", is_flag=True, help="Create the set if it does not exist")
@_guard
def smartview_add(set_ref, title, description, explode, create_set):
    """Add an empty Smart View to a set."""
    s, doc, kind = _lib_doc("smartviews")
    s.snapshot(f"smartview add {set_ref}/{title}")
    try:
        target = lib.find_set(doc, kind, set_ref)
    except lib.LibraryError:
        if not create_set:
            raise
        target = lib.add_set(doc, kind, set_ref)
    if any(lib.title(i, kind) == title for i in lib.items(target, kind)):
        fail(f"Smart view '{title}' already exists in set '{set_ref}'")
    sv = rules.build_smartview(title, description, _author(), explode)
    lib.append_item(target, kind, sv)
    emit({"ok": True, "set": lib.title(target, kind), "smartview": title,
          "guid": zx.text_of(sv, "GUID")}, f"Added smart view '{title}'")


@smartview.command("add-rule")
@click.option("--set", "set_ref", required=True)
@click.option("--view", "view_ref", required=True, help="Smart view title, GUID or index")
@click.option("--position", type=int, default=None, help="Insert at index (default: append)")
@_rule_options
@_guard
def smartview_add_rule(set_ref, view_ref, position, **kw):
    """Append a rule (rules are evaluated top-down in Zoom)."""
    s, doc, kind = _lib_doc("smartviews")
    sv = lib.find_item(lib.find_set(doc, kind, set_ref), kind, view_ref)
    rule = _build_rule(kw)
    s.snapshot(f"smartview add-rule {set_ref}/{view_ref}")
    rs = rules.rules_container(sv, "smartviews")
    if position is None:
        rs.append(rule)
    else:
        rs.insert(position, rule)
    rules.touch_item(sv, _author())
    emit({"ok": True, "rule": lib.rule_to_dict(rule), "rule_count": len(rs)},
         f"Rule added ({len(rs)} rules)")


@smartview.command("remove-rule")
@click.option("--set", "set_ref", required=True)
@click.option("--view", "view_ref", required=True)
@click.option("--index", type=int, required=True)
@_guard
def smartview_remove_rule(set_ref, view_ref, index):
    """Remove the rule at INDEX."""
    s, doc, kind = _lib_doc("smartviews")
    sv = lib.find_item(lib.find_set(doc, kind, set_ref), kind, view_ref)
    rs = rules.rules_container(sv, "smartviews")
    if not 0 <= index < len(rs):
        fail(f"No rule at index {index} ({len(rs)} rules)")
    s.snapshot(f"smartview remove-rule {index}")
    removed = rs[index]
    rs.remove(removed)
    rules.touch_item(sv, _author())
    emit({"ok": True, "removed": lib.rule_to_dict(removed)}, f"Removed rule {index}")


@smartview.command("takeoff")
@click.option("--set", "set_ref", required=True)
@click.option("--view", "view_ref", required=True)
@click.option("--pset", required=True, help="e.g. BaseQuantities")
@click.option("--prop", required=True, help="e.g. NetVolume")
@click.option("--operation", type=click.IntRange(0, 3), default=0,
              help="Zoom operation code (0=sum seen most often)")
@_guard
def smartview_takeoff(set_ref, view_ref, pset, prop, operation):
    """Set the information take-off property of a Smart View."""
    s, doc, kind = _lib_doc("smartviews")
    sv = lib.find_item(lib.find_set(doc, kind, set_ref), kind, view_ref)
    s.snapshot("smartview takeoff")
    rules.set_takeoff(sv, pset, prop, operation)
    rules.touch_item(sv, _author())
    emit({"ok": True, "pset": pset, "prop": prop, "operation": operation})


@smartview.command("color-by-values")
@click.option("--set", "set_ref", required=True)
@click.option("--title", required=True)
@click.option("--ifc-type", default="Any")
@click.option("--prop", required=True)
@click.option("--pset", default="Summary")
@click.option("--values", required=True, help="Comma-separated values, one color each")
@click.option("--colors", default=None, help="Semicolon-separated colors (default: palette)")
@click.option("--create-set", is_flag=True)
@_guard
def smartview_color_by_values(set_ref, title, ifc_type, prop, pset, values, colors, create_set):
    """Build a complete 'one color per value' Smart View in one command."""
    s, doc, kind = _lib_doc("smartviews")
    vals = [v.strip() for v in values.split(",") if v.strip()]
    palette = ["red", "green", "blue", "orange", "purple", "cyan", "magenta", "yellow", "grey"]
    cols = [c.strip() for c in colors.split(";")] if colors else palette
    if len(cols) < len(vals):
        fail(f"{len(vals)} values but only {len(cols)} colors")
    built = [rules.build_rule(ifc_type=ifc_type, prop=prop, pset=pset, condition="Is",
                              value=v, action="AddSetColored", color=c)
             for v, c in zip(vals, cols)]
    s.snapshot(f"smartview color-by-values {title}")
    try:
        target = lib.find_set(doc, kind, set_ref)
    except lib.LibraryError:
        if not create_set:
            raise
        target = lib.add_set(doc, kind, set_ref)
    sv = rules.build_smartview(title, f"{prop} colored by value", _author(), rules=built)
    lib.append_item(target, kind, sv)
    emit({"ok": True, "smartview": title, "rules": [lib.rule_to_dict(r) for r in built]},
         f"Added '{title}' with {len(built)} colored rules")


# ---------------------------------------------------------------------------
# clash
# ---------------------------------------------------------------------------

@cli.group()
def clash():
    """Create and edit Clash Rules (.bcr)."""


@clash.command("add")
@click.option("--set", "set_ref", required=True)
@click.option("--title", required=True)
@click.option("--description", default="")
@click.option("--min-distance", type=float, default=0.0, help="Clearance in metres")
@click.option("--clashes/--no-clashes", default=True)
@click.option("--duplicates/--no-duplicates", default=True)
@click.option("--distance/--no-distance", "check_distance", default=False,
              help="Also report elements closer than --min-distance")
@click.option("--same-model/--no-same-model", default=True)
@click.option("--same-system/--no-same-system", default=False)
@click.option("--subparts/--no-subparts", default=False)
@click.option("--penetration-tolerance", type=float, default=None)
@click.option("--volume-tolerance", type=float, default=None)
@click.option("--create-set", is_flag=True)
@_guard
def clash_add(set_ref, title, description, min_distance, clashes, duplicates, check_distance,
              same_model, same_system, subparts, penetration_tolerance, volume_tolerance,
              create_set):
    """Add a clash rule (then fill source/target with 'clash add-rule')."""
    s, doc, kind = _lib_doc("clashrules")
    s.snapshot(f"clash add {set_ref}/{title}")
    try:
        target = lib.find_set(doc, kind, set_ref)
    except lib.LibraryError:
        if not create_set:
            raise
        target = lib.add_set(doc, kind, set_ref)
    cr = rules.build_clashrule(title, description, _author(), min_distance=min_distance,
                               check_clashes=clashes, check_duplicates=duplicates,
                               check_distance=check_distance, same_model=same_model,
                               same_system=same_system, subparts=subparts,
                               penetration_tolerance=penetration_tolerance,
                               volume_tolerance=volume_tolerance)
    lib.append_item(target, kind, cr)
    emit({"ok": True, "clashrule": title, "guid": zx.text_of(cr, "GUID")},
         f"Added clash rule '{title}'")


@clash.command("add-rule")
@click.option("--set", "set_ref", required=True)
@click.option("--rule", "rule_ref", required=True, help="Clash rule title, GUID or index")
@click.option("--side", type=click.Choice(["source", "target"]), required=True)
@_rule_options
@_guard
def clash_add_rule(set_ref, rule_ref, side, **kw):
    """Add a selection rule to the source or target set of a clash rule."""
    s, doc, kind = _lib_doc("clashrules")
    cr = lib.find_item(lib.find_set(doc, kind, set_ref), kind, rule_ref)
    rule = _build_rule(kw)
    s.snapshot(f"clash add-rule {side}")
    rs = rules.rules_container(cr, "clashrules", side)
    rs.append(rule)
    rules.touch_item(cr, _author())
    emit({"ok": True, "side": side, "rule": lib.rule_to_dict(rule), "rule_count": len(rs)},
         f"Rule added to {side} ({len(rs)} rules)")


@clash.command("remove-rule")
@click.option("--set", "set_ref", required=True)
@click.option("--rule", "rule_ref", required=True)
@click.option("--side", type=click.Choice(["source", "target"]), required=True)
@click.option("--index", type=int, required=True)
@_guard
def clash_remove_rule(set_ref, rule_ref, side, index):
    """Remove a selection rule from the source or target set."""
    s, doc, kind = _lib_doc("clashrules")
    cr = lib.find_item(lib.find_set(doc, kind, set_ref), kind, rule_ref)
    rs = rules.rules_container(cr, "clashrules", side)
    if not 0 <= index < len(rs):
        fail(f"No rule at index {index} ({len(rs)} rules)")
    s.snapshot(f"clash remove-rule {side} {index}")
    removed = rs[index]
    rs.remove(removed)
    rules.touch_item(cr, _author())
    emit({"ok": True, "removed": lib.rule_to_dict(removed)})


@clash.command("set")
@click.option("--set", "set_ref", required=True)
@click.option("--rule", "rule_ref", required=True)
@click.option("--min-distance", type=float, default=None)
@click.option("--same-model", type=bool, default=None)
@click.option("--same-system", type=bool, default=None)
@click.option("--subparts", type=bool, default=None)
@click.option("--duplicates", type=bool, default=None)
@_guard
def clash_set(set_ref, rule_ref, min_distance, same_model, same_system, subparts, duplicates):
    """Change settings of an existing clash rule."""
    s, doc, kind = _lib_doc("clashrules")
    cr = lib.find_item(lib.find_set(doc, kind, set_ref), kind, rule_ref)
    changes = {"MINIMUMDISTANCE": None if min_distance is None else f"{min_distance:g}",
               "CLASHSAMEMODEL": same_model, "CLASHSAMESYSTEM": same_system,
               "CLASHSUBPARTS": subparts, "DUPLICATETYPE": duplicates}
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        fail("Nothing to change")
    s.snapshot("clash set")
    for tag, v in changes.items():
        cr.find(tag).text = ("true" if v else "false") if isinstance(v, bool) else v
    rules.touch_item(cr, _author())
    emit({"ok": True, "changed": {k.lower(): v for k, v in changes.items()}})


# ---------------------------------------------------------------------------
# project (.bcp) and IFC
# ---------------------------------------------------------------------------

@cli.group()
def project():
    """Zoom project files (.bcp): create from IFCs, inspect, relink."""


@project.command("new")
@click.option("-o", "--output", required=True, help=".bcp file to create")
@click.option("--model", "models", multiple=True, required=True, help="IFC file (repeatable)")
@click.option("--force", is_flag=True)
@_guard
def project_new(output, models, force):
    """Create a local Zoom project from IFC files."""
    if Path(output).suffix.lower() != ".bcp":
        fail("Output must be a .bcp file")
    if Path(output).exists() and not force:
        fail(f"{output} exists (use --force)")
    doc = prj.new_project()
    added = [prj.add_model(doc, m) for m in models]
    s = get_session()
    s.attach_new(output, doc)
    if not click.get_current_context().find_root().obj.get("dry_run"):
        s.save_session()
    emit({"ok": True, "path": s.project_path,
          "models": [{"file": Path(a["path"]).name, "schema": a["schema"]} for a in added]},
         f"Created {s.project_path} with {len(added)} model(s)")


@project.command("info")
@_guard
def project_info():
    """Models, connection and origin of the .bcp."""
    s = _sess("project")
    emit(prj.info(s.obj))


@project.command("add-model")
@click.argument("ifc")
@click.option("--prefix", default="")
@click.option("--discipline", default="")
@_guard
def project_add_model(ifc, prefix, discipline):
    s = _sess("project")
    s.snapshot(f"add-model {ifc}")
    info = prj.add_model(s.obj, ifc, prefix, discipline)
    emit({"ok": True, "model": info})


@project.command("remove-model")
@click.argument("ref")
@_guard
def project_remove_model(ref):
    """Remove a model by index, file name or full path."""
    s = _sess("project")
    s.snapshot(f"remove-model {ref}")
    emit({"ok": True, "removed": prj.remove_model(s.obj, ref)})


@project.command("relink")
@click.option("--search", "dirs", multiple=True, required=True, help="Folder to search (repeatable)")
@_guard
def project_relink(dirs):
    """Repoint missing models to same-named files found in --search folders."""
    s = _sess("project")
    s.snapshot("relink")
    report = prj.relink(s.obj, list(dirs))
    if not any(r["to"] for r in report):
        s.undo()
        s._modified = False
    emit({"ok": True, "report": report})


@project.command("open")
@_guard
def project_open_gui():
    """Open this .bcp in the Zoom GUI."""
    s = _sess("project")
    emit(backend.launch([s.project_path]))


@cli.group()
def ifc():
    """IFC helpers."""


@ifc.command("probe")
@click.argument("files", nargs=-1, required=True)
@_guard
def ifc_probe(files):
    """Read IFC header data (schema, authoring tool, MVD) without loading the model."""
    emit([prj.probe_ifc(f) for f in files])


# ---------------------------------------------------------------------------
# nightshift
# ---------------------------------------------------------------------------

@cli.group()
def nightshift():
    """Build, validate, run and schedule Zoom Nightshift scripts."""


def _ns():
    return _sess("nightshift")


@nightshift.command("new")
@click.option("-o", "--output", required=True, help=".json script (no spaces in file name)")
@click.option("--title", required=True)
@click.option("--force", is_flag=True)
@_guard
def ns_new(output, title, force):
    """Create an empty Nightshift script."""
    if Path(output).suffix.lower() != ".json":
        fail("Script must be a .json file")
    if " " in Path(output).name:
        fail("Zoom requires a script file name without spaces")
    if Path(output).exists() and not force:
        fail(f"{output} exists (use --force)")
    s = get_session()
    s.attach_new(output, ns.new_script(title))
    if not click.get_current_context().find_root().obj.get("dry_run"):
        s.save_session()
    emit({"ok": True, "path": s.project_path}, f"Created Nightshift script {s.project_path}")


def _ns_add(action: dict, label: str):
    s = _ns()
    s.snapshot(label)
    idx = ns.append(s.obj, action)
    emit({"ok": True, "index": idx, "action": action}, f"Added {action['type']} at {idx}")


@nightshift.command("open-models")
@click.argument("models", nargs=-1, required=True)
@click.option("--add", is_flag=True, help="ADD_TO_PROJECT (keep loaded models)")
@click.option("--no-check", is_flag=True, help="Do not require the files to exist here")
@_guard
def ns_open_models(models, add, no_check):
    """OPEN_MODELS / ADD_TO_PROJECT."""
    _ns_add(ns.action_open_models(list(models), add=add, check_exists=not no_check), "open-models")


@nightshift.command("connect")
@click.option("--space", "space_name", required=True, help="Exactly as typed when first connecting")
@click.option("--project-name", required=True)
@_guard
def ns_connect(space_name, project_name):
    """CONNECT_TO_PROJECT (needs saved credentials in Zoom)."""
    _ns_add(ns.action_connect(space_name, project_name), "connect")


@nightshift.command("filter-issues")
@click.option("--filter-type", default=None)
@click.option("--area", default=None)
@click.option("--milestone", default=None)
@click.option("--label", default=None)
@click.option("--priority", default=None)
@click.option("--show-active/--hide-active", default=None)
@click.option("--show-resolved/--hide-resolved", default=None)
@click.option("--show-closed/--hide-closed", default=None)
@click.option("--hide-invisible/--show-invisible", default=None)
@click.option("--show-to-approve/--hide-to-approve", default=None)
@click.option("--assigned-to", "assigned_to_user_email", default=None)
@click.option("--created-by", "created_by_user_email", default=None)
@click.option("--deadline", default=None, help="dd-mm-yyyy")
@click.option("--custom1", default=None)
@click.option("--custom2", default=None)
@_guard
def ns_filter(**kw):
    """FILTER_ISSUES (only non-default values are written)."""
    _ns_add(ns.action_filter_issues(**kw), "filter-issues")


@nightshift.command("update-issues")
@click.option("--close-solved/--keep-solved", default=True,
              help="Close issues whose clashes are solved")
@click.option("--reopen/--no-reopen", default=True, help="Reopen previously closed issues")
@click.option("--comment", default="")
@_guard
def ns_update(close_solved, reopen, comment):
    """UPDATE_ISSUES on all visible issues."""
    _ns_add(ns.action_update_issues(close_solved, reopen, comment), "update-issues")


@nightshift.command("run-lists")
@click.option("--list-set", default=None, help="Only this list set (default: all shared)")
@_guard
def ns_run_lists(list_set):
    """RUN_AND_SHARE_LISTS."""
    _ns_add(ns.action_run_lists(list_set), "run-lists")


@nightshift.command("sync")
@_guard
def ns_sync():
    """SYNCHRONIZE_PROJECT (always kept last)."""
    _ns_add(ns.action_sync(), "sync")


@nightshift.command("remove-action")
@click.argument("index", type=int)
@_guard
def ns_remove(index):
    s = _ns()
    s.snapshot(f"remove-action {index}")
    emit({"ok": True, "removed": ns.remove(s.obj, index)})


@nightshift.command("show")
@_guard
def ns_show():
    """Show the script's actions."""
    emit(ns.describe(_ns().obj))


@nightshift.command("validate")
@click.option("--no-file-check", is_flag=True)
@_guard
def ns_validate(no_file_check):
    """Check the script against Zoom's rules."""
    issues = ns.validate(_ns().obj, check_files=not no_file_check)
    errors = [i for i in issues if i["level"] == "error"]
    emit({"ok": not errors, "issues": issues})
    if errors:
        fail(f"{len(errors)} error(s)")


@nightshift.command("run")
@click.option("--timeout", type=int, default=1800, show_default=True, help="Seconds")
@click.option("--allow-running", is_flag=True,
              help="Run even if a Zoom instance is open (it may replace its models)")
@click.option("--yes", is_flag=True, help="Confirm scripts that write to the BIMcollab cloud")
@_guard
def ns_run(timeout, allow_running, yes):
    """Run the script with the real BIMcollab Zoom and collect the Nightshift log."""
    s = _ns()
    if s._modified:
        s.save_session()
    issues = ns.validate(s.obj)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        fail("Script is invalid: " + "; ".join(f"{e['where']}: {e['message']}" for e in errors))
    if ns.describe(s.obj)["writes_to_cloud"] and not yes:
        fail("This script writes to the BIMcollab cloud (issues/lists/sync). Re-run with --yes.")
    res = backend.run_nightshift(s.project_path, timeout=timeout, allow_running=allow_running)
    emit(res, None if _json_mode() else
         f"{'OK' if res['ok'] else 'FAILED'}  exit={res['exit_code']}  {res['duration_s']}s  "
         f"logs={len(res['logs'])}\n" + "\n".join(l["text"] for l in res["logs"]))
    if not res["ok"]:
        fail("Nightshift run failed")


@nightshift.command("command")
@_guard
def ns_command():
    """Print the exact Zoom command line (for Task Scheduler or manual use)."""
    s = _ns()
    if s._modified:
        s.save_session()
    cmd = backend.build_command(s.project_path)
    emit({"command": cmd}, " ".join(f'"{c}"' if " " in c else c for c in cmd))


@nightshift.command("logs")
@click.option("--limit", type=int, default=5)
@click.option("--read", "read_latest", is_flag=True, help="Print the latest log")
@_guard
def ns_logs(limit, read_latest):
    """List (or read) Nightshift log files."""
    logs = backend.list_logs(limit=limit)
    if read_latest:
        if not logs:
            fail(f"No logs in {backend.nightshift_log_dir()}")
        emit(backend.parse_log(logs[0]["path"]))
    else:
        emit({"dir": str(backend.nightshift_log_dir()), "logs": logs})


@nightshift.command("schedule")
@click.argument("name")
@click.option("--at", required=True, help="HH:MM")
@click.option("--frequency", type=click.Choice(["ONCE", "DAILY", "WEEKLY"]), default="DAILY")
@click.option("--days", default=None, help="WEEKLY only, e.g. MON,WED,FRI")
@_guard
def ns_schedule(name, at, frequency, days):
    """Create a Windows scheduled task 'bczoom\\NAME' running this script (honours --dry-run)."""
    s = _ns()
    if s._modified:
        s.save_session()
    cmd = backend.schedule_command(name, s.project_path, at, frequency, days)
    if click.get_current_context().find_root().obj.get("dry_run"):
        emit({"dry_run": True, "command": cmd})
        return
    res = backend.run_schtasks(cmd)
    emit(res)
    if res["exit_code"] != 0:
        fail(res["stderr"] or "schtasks failed")


@nightshift.command("tasks")
@_guard
def ns_tasks():
    """List scheduled tasks created by this CLI."""
    emit(backend.list_tasks())


@nightshift.command("unschedule")
@click.argument("name")
@_guard
def ns_unschedule(name):
    """Delete scheduled task 'bczoom\\NAME' (honours --dry-run)."""
    cmd = ["schtasks", "/Delete", "/F", "/TN", f"{backend.TASK_FOLDER}\\{name}"]
    if click.get_current_context().find_root().obj.get("dry_run"):
        emit({"dry_run": True, "command": cmd})
        return
    res = backend.run_schtasks(cmd)
    emit(res)
    if res["exit_code"] != 0:
        fail(res["stderr"] or "schtasks failed")


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------

@cli.group()
def session():
    """Undo/redo, status, history, save."""


@session.command("status")
def session_status():
    emit(get_session().status())


@session.command("undo")
@_guard
def session_undo():
    ok = _sess().undo()
    emit({"ok": ok, **get_session().status()}, "Undone" if ok else "Nothing to undo")


@session.command("redo")
@_guard
def session_redo():
    ok = _sess().redo()
    emit({"ok": ok, **get_session().status()}, "Redone" if ok else "Nothing to redo")


@session.command("history")
@_guard
def session_history():
    emit(_sess().history())


@session.command("save")
@click.option("-o", "--output", default=None, help="Save a copy elsewhere")
@_guard
def session_save(output):
    path = _sess().save_session(output)
    emit({"ok": True, "path": path}, f"Saved {path}")


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

@cli.command("open")
@click.argument("path")
@_guard
def open_cmd(path):
    """Open a document (REPL convenience; same as --project)."""
    get_session().open(path)
    emit(get_session().status(), f"Opened {get_session().project_path}")


@cli.command()
def repl():
    """Interactive mode (default when no command is given)."""
    global _repl_mode
    from cli_anything.bczoom.utils.repl_skin import ReplSkin

    _repl_mode = True
    skin = ReplSkin("bczoom", version=__version__)
    skin.print_banner()
    pt = skin.create_prompt_session()
    commands = {
        "open <file>": "Open a .bcsv/.bcr/.bcl/.bcsp/.bcp/.json document",
        "app info|launch|paths": "Installed Zoom, launch GUI, live library paths",
        "library ...": "info, items, show, search, add-set, extract, merge, validate",
        "smartview ...": "add, add-rule, remove-rule, takeoff, color-by-values",
        "clash ...": "add, add-rule, remove-rule, set",
        "project ...": "new, info, add-model, remove-model, relink, open",
        "ifc probe <files>": "Read IFC headers",
        "nightshift ...": "new, open-models, connect, filter-issues, update-issues, run-lists, "
                          "sync, show, validate, run, logs, schedule",
        "session ...": "status, undo, redo, history, save",
        "help": "This help", "quit": "Exit (unsaved changes are kept in memory only)",
    }
    while True:
        s = get_session()
        name = Path(s.project_path).name if s.project_path else ""
        try:
            line = skin.get_input(pt, project_name=name, modified=s._modified)
        except (EOFError, KeyboardInterrupt):
            break
        line = (line or "").strip()
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            if s._modified:
                skin.warning("Unsaved changes. Use 'session save' or 'quit' again.")
                s._modified = False
                continue
            break
        if line == "help":
            skin.help(commands)
            continue
        try:
            args = shlex.split(line, posix=False)
            args = [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in args]
            cli.main(args=args, prog_name="bczoom", standalone_mode=False,
                     obj={"json": False, "dry_run": False})
        except click.exceptions.Exit:
            pass
        except click.ClickException as exc:
            skin.error(exc.format_message())
        except SystemExit:
            pass
        except Exception as exc:  # noqa: BLE001
            skin.error(str(exc))
    skin.print_goodbye()
    _repl_mode = False


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cli(obj={})


if __name__ == "__main__":
    main()
