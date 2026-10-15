"""Live bridge: act on a BIMcollab Zoom session that is already open.

Zoom has no IPC/API, and starting ``BIMcollab Zoom.exe`` again opens a *new*,
empty instance (no single-instance forwarding). So the only way to change the
user's running session is Windows UI Automation on the existing process.

Zoom 9.x is WinForms + Telerik; the controls used here have stable automation
ids / names (checked on 9.5):
  tabs      tabButtonNav / tabButtonSmart / tabButtonClash / tabButtonTable / tabButtonIssues
  trees     SmartViewSetsTreeView / ConflictRulesTreeView / TableSetsTreeView
  toolbars  tb_* buttons (UIA Custom elements named after the command)
  dialogs   standard #32770 Open/Save dialogs: file name edit 1148 (open) / 1001 (save), OK = 1

What works how (Telerik ignores most UIA patterns):
  * main menu items     -> UIA ExpandCollapse / Invoke (no mouse)
  * tree set expansion  -> LegacyIAccessible DoDefaultAction "Expand" (no mouse)
  * apply a smart view  -> double-click; select a rule/list -> click; toolbar buttons -> click
    (real mouse clicks at the element's UIA position: needs an unlocked desktop)

Nothing here launches an executable. Requires ``pywinauto`` (and Pillow for screenshots).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from cli_anything.bczoom.utils.zoom_backend import ZoomBackendError, running_instances

TITLE_PREFIX = "BIMcollab Zoom"
TABS = {"navigation": "tabButtonNav", "smartviews": "tabButtonSmart", "clashrules": "tabButtonClash",
        "lists": "tabButtonTable", "issues": "tabButtonIssues"}
# kind -> (tree automation id, options drop-down button, run button)
PANELS = {
    "smartviews": ("SmartViewSetsTreeView", "tb_OptionsDropDownSV", None),
    "clashrules": ("ConflictRulesTreeView", "tb_OptionsDropDownCD", "tb_RunSelectedCD"),
    "lists": ("TableSetsTreeView", "tb_OptionsDropDownTable", "tb_RunTables"),
}
EXT_KIND = {".bcsv": "smartviews", ".bcr": "clashrules", ".bcl": "lists", ".bcsp": "smartproperties"}
IMPORT_LABELS = ("Importer", "Import", "Importieren", "Importeren")
EXPORT_LABELS = ("Exporter", "Export", "Exportieren", "Exporteren")
_STATE_SUFFIXES = (" Expanded", " Collapsed")


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

_DPI_SET = False


def _dpi_aware() -> None:
    """Per-monitor DPI awareness, so UIA rectangles and SetCursorPos share physical pixels.
    Without it Windows rescales cursor coordinates on a monitor with another scale factor."""
    global _DPI_SET
    if _DPI_SET:
        return
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_V2
    except Exception:  # noqa: BLE001 - older Windows
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            pass
    _DPI_SET = True


def _pywinauto():
    if sys.platform != "win32":
        raise ZoomBackendError("Live control of Zoom is only available on Windows.")
    _dpi_aware()
    try:
        import pywinauto
    except ImportError as exc:
        raise ZoomBackendError("pywinauto is required for live commands: pip install pywinauto") from exc
    return pywinauto


_MAIN_CLASS = "WindowsForms10.Window.8"


def _top_windows(pid: int | None = None) -> list[tuple[int, int, str, str]]:
    """Visible top-level windows as (hwnd, pid, class, title) via Win32 EnumWindows — micro-
    seconds, vs ~0.6 s for a UIA desktop enumeration."""
    import win32gui
    import win32process
    out = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h):
            p = win32process.GetWindowThreadProcessId(h)[1]
            if pid is None or p == pid:
                out.append((h, p, win32gui.GetClassName(h), win32gui.GetWindowText(h)))
        return True
    win32gui.EnumWindows(cb, None)
    return out


def _session_of(hwnd: int, pid: int, title: str) -> dict:
    model = title.split(":", 1)[1].strip() if ":" in title else ""
    return {"pid": pid, "title": title, "model": model, "hwnd": hwnd}


def sessions() -> list[dict]:
    """Running Zoom instances with their main window title and loaded model."""
    _pywinauto()
    return [{k: v for k, v in s.items() if k != "hwnd"} for s in _sessions_full()]


def _sessions_full() -> list[dict]:
    out = {}
    for h, p, cls, title in _top_windows():
        if cls.startswith(_MAIN_CLASS) and title.startswith(TITLE_PREFIX):
            if p not in out or (":" in title and ":" not in out[p]["title"]):
                out[p] = _session_of(h, p, title)
    return sorted(out.values(), key=lambda s: s["pid"])


def pick_session(pid: int | None = None) -> dict:
    found = _sessions_full()
    if pid is not None:
        for s in found:
            if s["pid"] == pid:
                return s
        raise ZoomBackendError(f"No Zoom main window for PID {pid}. Running: {found}")
    loaded = [s for s in found if s["model"]]
    if len(loaded) == 1:
        return loaded[0]
    if not found:
        raise ZoomBackendError("BIMcollab Zoom is not running. Open your model in Zoom first.")
    raise ZoomBackendError(
        "Several Zoom windows are open; pass --pid. Sessions: "
        + "; ".join(f"PID {s['pid']} '{s['title']}'" for s in found))


_CACHE: dict[int | None, tuple[dict, object]] = {}


def _connect(pid: int | None):
    """(session dict, main window wrapper) for the chosen session, brought to the front.
    Cached per process (the MCP server is long-lived): reconnecting costs ~1-3 s."""
    import win32gui
    _pywinauto()
    hit = _CACHE.get(pid)
    if hit and win32gui.IsWindow(hit[0]["hwnd"]):
        sess, main = hit
        title = win32gui.GetWindowText(sess["hwnd"])
        if title != sess["title"]:  # another model was loaded meanwhile
            sess = _session_of(sess["hwnd"], sess["pid"], title)
            _CACHE[pid] = (sess, main)
    else:
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_element_info import UIAElementInfo
        sess = pick_session(pid)
        main = UIAWrapper(UIAElementInfo(sess["hwnd"]))
        _CACHE[pid] = _CACHE[sess["pid"]] = (sess, main)
    if win32gui.GetForegroundWindow() != sess["hwnd"]:
        try:
            main.set_focus()
        except Exception:  # noqa: BLE001 - focus refused (e.g. modal dialog): clicks still work
            pass
    return sess, main


def _result(sess: dict, **kw) -> dict:
    return {"ok": True, "pid": sess["pid"], "model": sess["model"], **kw}


# ---------------------------------------------------------------------------
# Small UIA helpers
# ---------------------------------------------------------------------------

def _wait(predicate, timeout: float, step: float = 0.1):
    end = time.time() + timeout
    while time.time() < end:
        res = predicate()
        if res:
            return res
        time.sleep(step)
    return None


_CTYPE_IDS = {"Button": 50000, "Edit": 50004, "MenuItem": 50011, "Custom": 50025,
              "TreeItem": 50024, "Tree": 50023, "Window": 50032, "CheckBox": 50002}


def _find_all(parent, auto_id: str | None = None, name: str | None = None,
              control_type: str | None = None, first: bool = False) -> list:
    """Native UIA FindFirst/FindAll with property conditions, evaluated inside the provider:
    ~10-50 ms instead of 0.5-1.5 s for a Python-side descendants() walk."""
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo
    u = IUIA()
    conds = []
    if auto_id is not None:
        conds.append(u.iuia.CreatePropertyCondition(u.UIA_dll.UIA_AutomationIdPropertyId, auto_id))
    if name is not None:
        conds.append(u.iuia.CreatePropertyCondition(u.UIA_dll.UIA_NamePropertyId, name))
    if control_type is not None:
        conds.append(u.iuia.CreatePropertyCondition(u.UIA_dll.UIA_ControlTypePropertyId,
                                                    _CTYPE_IDS[control_type]))
    if not conds:
        cond = u.iuia.CreateTrueCondition()
    elif len(conds) == 1:
        cond = conds[0]
    else:
        cond = u.iuia.CreateAndConditionFromArray(conds)
    root = parent.element_info.element
    scope = u.tree_scope["descendants"]
    try:
        if first:
            el = root.FindFirst(scope, cond)
            els = [el] if el else []
        else:
            arr = root.FindAll(scope, cond)
            els = [arr.GetElement(i) for i in range(arr.Length)]
    except Exception:  # noqa: BLE001 - COM null pointer / element gone
        return []
    return [UIAWrapper(UIAElementInfo(e)) for e in els]


def _by_id(parent, auto_id: str, control_type: str | None = None):
    hits = _find_all(parent, auto_id=auto_id, control_type=control_type, first=True)
    return hits[0] if hits else None


def _button(main, name: str):
    """Telerik toolbar button (UIA Custom named after the command, e.g. tb_ZoomToFit)."""
    for d in _find_all(main, name=name, control_type="Custom"):
        if d.rectangle().width() > 0:
            return d
    return None


def _mouse(x: int, y: int, *events: int) -> None:
    """Win32 SetCursorPos + mouse_event: works on every monitor, including negative virtual-
    desktop coordinates (pywinauto's click_input normalises to the primary screen and misses
    windows on a secondary monitor left of / above it)."""
    import ctypes
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    for ev, data in events:
        user32.mouse_event(ev, 0, 0, data, 0)
        time.sleep(0.03)


_LDOWN, _LUP, _WHEEL = 0x0002, 0x0004, 0x0800


def _click(wrapper, double: bool = False) -> None:
    p = wrapper.rectangle().mid_point()
    seq = [(_LDOWN, 0), (_LUP, 0)] * (2 if double else 1)
    _mouse(p.x, p.y, *seq)


def _wheel(wrapper, notches: int) -> None:
    p = wrapper.rectangle().mid_point()
    _mouse(p.x, p.y, (_WHEEL, 120 * notches))


def _legacy(wrapper):
    from pywinauto import uia_defines
    return uia_defines.get_elem_interface(wrapper.element_info.element, "LegacyIAccessible")


def _node_title(item) -> str:
    # Telerik appends the expand state to set nodes' UIA name ("Escaliers Expanded").
    name = item.window_text()
    for s in _STATE_SUFFIXES:
        if name.endswith(s):
            return name[: -len(s)]
    return name


def _scroll_into(tree, item, max_steps: int = 60) -> bool:
    """Wheel-scroll the tree until the item's row is inside the tree's visible area."""
    for _ in range(max_steps):
        t, r = tree.rectangle(), item.rectangle()
        if r.height() > 0 and r.top >= t.top and r.bottom <= t.bottom:
            return True
        down = r.height() == 0 or r.top >= t.bottom  # off-screen rows may report an empty rect
        _wheel(tree, -1 if down else 1)
        time.sleep(0.1)
    return False


def _process_windows(pid: int):
    """Visible top-level windows of the process, as UIA wrappers (Win32 enumeration)."""
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo
    _pywinauto()
    out = []
    for h, _p, _cls, _t in _top_windows(pid):
        try:
            out.append(UIAWrapper(UIAElementInfo(h)))
        except Exception:  # noqa: BLE001 - window vanished
            continue
    return out


def _file_dialogs(pid: int, main) -> list:
    """Open/Save dialogs of the session (Zoom parents some to the main window, some to the desktop)."""
    found = []
    for c in main.children():  # dialogs of child windows too (e.g. the Smart Properties manager)
        if c.element_info.class_name == "#32770":
            found.append(c)
        elif c.element_info.control_type in ("Window", "Dialog"):
            found += [g for g in c.children() if g.element_info.class_name == "#32770"]
    found += [w for w in _process_windows(pid) if w.element_info.class_name == "#32770"]
    return found


def _dialog_text(dlg) -> str:
    parts = []
    for d in dlg.descendants():
        t = d.window_text()
        if t and d.element_info.control_type in ("Text", "Document"):
            parts.append(t)
    return " | ".join(parts)


def _popup_item(pid: int, labels: tuple[str, ...]):
    # Only popup windows: the main window also holds hidden "Exporter" buttons (list grid).
    for w in _process_windows(pid):
        if w.element_info.automation_id == "MainIfcForm":
            continue
        for label in labels:
            for d in _find_all(w, name=label):
                try:
                    if d.is_visible() and d.rectangle().width() > 0:
                        return d
                except Exception:  # noqa: BLE001 - popup closing
                    continue
    return None


# ---------------------------------------------------------------------------
# Tabs, trees
# ---------------------------------------------------------------------------

def _open_tab(main, name: str):
    aid = TABS.get(name)
    if aid is None:
        raise ZoomBackendError(f"Unknown tab '{name}'; choose from {', '.join(TABS)}")
    tab = _by_id(main, aid)
    if tab is None:
        raise ZoomBackendError(f"Tab {aid} not found in Zoom.")
    try:
        if tab.get_toggle_state() == 1:  # already the active tab: no click, no wait
            return
    except Exception:  # noqa: BLE001 - no Toggle pattern: fall back to clicking
        pass
    _click(tab)
    tree_id = PANELS.get(name, (None,))[0]
    if tree_id:  # wait for the panel instead of a fixed delay
        _wait(lambda: (lambda t: t is not None and t.rectangle().width() > 0)(_by_id(main, tree_id)), 2, 0.05)
    else:
        time.sleep(0.3)


def open_tab(name: str, pid: int | None = None) -> dict:
    sess, main = _connect(pid)
    _open_tab(main, name)
    return _result(sess, tab=name)


def _locate(main, kind: str, title: str, set_name: str | None, root: str):
    """Open the panel, find set/item in its tree, expand the set and scroll the item on screen."""
    _open_tab(main, kind)
    tree_id = PANELS[kind][0]
    tree = _wait(lambda: _by_id(main, tree_id), 5)
    if tree is None:
        raise ZoomBackendError(f"{tree_id} not found in Zoom.")
    roots = [c for c in tree.children() if _node_title(c) == root]
    if not roots:
        raise ZoomBackendError(f"Root '{root}' not found in the {kind} tree.")
    if roots[0].window_text().endswith(" Collapsed"):
        _legacy(roots[0]).DoDefaultAction()
        _wait(lambda: not roots[0].window_text().endswith(" Collapsed"), 2, 0.05)
    hits = []
    for s in roots[0].children():
        if set_name and _node_title(s) != set_name:
            continue
        hits += [s for v in s.children() if _node_title(v) == title]
    if not hits:
        raise ZoomBackendError(f"'{title}' not found under {root}"
                               + (f" / {set_name}" if set_name else "") + f" in the {kind} tree.")
    if len(hits) > 1:
        raise ZoomBackendError(f"Several items named '{title}'; pass --set. Sets: "
                               + ", ".join(_node_title(s) for s in hits))
    s = hits[0]
    if s.window_text().endswith(" Collapsed"):
        _legacy(s).DoDefaultAction()  # default action "Expand"; UIA ExpandCollapse is not exposed
        _wait(lambda: not s.window_text().endswith(" Collapsed"), 2, 0.05)
    item = next(c for c in s.children() if _node_title(c) == title)
    if not _scroll_into(tree, item):
        raise ZoomBackendError(f"Could not scroll '{title}' into view in the {kind} tree.")
    return _node_title(s), item


def show_smartview(view: str, set_name: str | None = None, pid: int | None = None,
                   root: str = "Local") -> dict:
    """Apply a smart view (Zoom applies on double-click only; Select/Invoke are ignored)."""
    sess, main = _connect(pid)
    s, item = _locate(main, "smartviews", view, set_name, root)
    _click(item, double=True)
    return _result(sess, set=s, view=view)


def _run_selected(kind: str, title: str, set_name: str | None, pid: int | None, root: str) -> dict:
    sess, main = _connect(pid)
    s, item = _locate(main, kind, title, set_name, root)
    _click(item)
    time.sleep(1.2)  # Zoom loads the selected rule/list before Run takes effect
    run = _button(main, PANELS[kind][2])
    if run is None:
        raise ZoomBackendError(f"Run button {PANELS[kind][2]} not found.")
    _click(run)
    if kind == "lists":
        # Right after an import the first Run can be ignored: the grid keeps its
        # "run your list" placeholder. Detect it and click Run again.
        for _ in range(2):
            if not _wait(lambda: _list_placeholder(main), 1.5, 0.1):
                break
            time.sleep(0.8)
            _click(run)
    return _result(sess, set=s, item=title, kind=kind)


_PLACEHOLDERS = ("Exécuter votre liste pour voir les résultats", "Run your list to see the results")


def _list_placeholder(main) -> bool:
    return any(d.is_visible() for p in _PLACEHOLDERS for d in _find_all(main, name=p))


DELETE_BUTTONS = {"smartviews": "tb_DeleteSV", "clashrules": "tb_RemoveRuleCD", "lists": "tb_DeleteTable"}


def delete_set(set_name: str, kind: str = "smartviews", pid: int | None = None,
               root: str = "Local", timeout: float = 10) -> dict:
    """Delete a whole set (and its content) from the session's library, via Zoom's Delete button.
    Confirms Zoom's prompt only if it names this exact set."""
    sess, main = _connect(pid)
    pending = [d for d in _file_dialogs(sess["pid"], main)
               if f'"{set_name}"' in _dialog_text(d) and _by_id(d, "1", "Button")]
    if not pending:  # otherwise reuse a prompt left open for this very set
        _open_tab(main, kind)
        tree = _wait(lambda: _by_id(main, PANELS[kind][0]), 5)
        roots = [c for c in tree.children() if _node_title(c) == root]
        node = next((s for r in roots for s in r.children() if _node_title(s) == set_name), None)
        if node is None:
            raise ZoomBackendError(f"Set '{set_name}' not found under {root} in the {kind} tree.")
        if not _scroll_into(tree, node):
            raise ZoomBackendError(f"Could not scroll '{set_name}' into view.")
        _click(node)
        time.sleep(0.5)  # selection must register before Delete acts on it
        btn = _button(main, DELETE_BUTTONS[kind])
        if btn is None:
            raise ZoomBackendError(f"Delete button {DELETE_BUTTONS[kind]} not found.")
        _click(btn)
        pending = _wait(lambda: [d for d in _file_dialogs(sess["pid"], main)
                                 if _by_id(d, "1", "Button")], timeout) or []
    if not pending:
        raise ZoomBackendError("Zoom did not ask for confirmation; nothing deleted.")
    dlg = pending[0]
    text = _dialog_text(dlg)
    if f'"{set_name}"' not in text:
        _by_id(dlg, "2", "Button").invoke()  # Annuler
        raise ZoomBackendError(f"Unexpected confirmation, cancelled: {text}")
    _by_id(dlg, "1", "Button").invoke()
    if not _wait(lambda: set_name not in _set_titles(sess["pid"], kind, root), 5, 0.1):
        raise ZoomBackendError(f"'{set_name}' is still in the {kind} tree after confirming.")
    return _result(sess, deleted=set_name, kind=kind, prompt=text)


def run_list(title: str, set_name: str | None = None, pid: int | None = None, root: str = "Local") -> dict:
    """Run a list: results open in Zoom's list grid."""
    return _run_selected("lists", title, set_name, pid, root)


def run_clash(title: str, set_name: str | None = None, pid: int | None = None, root: str = "Local") -> dict:
    """Run a clash rule on the loaded models: results appear in the Conflits panel."""
    return _run_selected("clashrules", title, set_name, pid, root)


# ---------------------------------------------------------------------------
# Import / export through the panel's options menu
# ---------------------------------------------------------------------------

def _panel_menu(main, pid: int, kind: str, labels: tuple[str, ...]):
    item = _popup_item(pid, labels)  # menu possibly left open by an interrupted call
    if item is None:
        _open_tab(main, kind)
        opt = _wait(lambda: _button(main, PANELS[kind][1]), 5)
        if opt is None:
            raise ZoomBackendError(f"Options menu {PANELS[kind][1]} not found in the {kind} panel.")
        item = None
        for _ in range(3):  # the first click is sometimes swallowed by window activation
            _click(opt)
            item = _wait(lambda: _popup_item(pid, labels), 3)
            if item is not None:
                break
        if item is None:
            raise ZoomBackendError(f"'{labels[0]}' entry not found in the {kind} panel menu.")
    # The drop-down animates open; a click during the animation only highlights the entry.
    for _ in range(3):
        time.sleep(0.6)
        _click(item)
        if _wait(lambda: _popup_item(pid, labels) is None, 2):
            return
    raise ZoomBackendError(f"Zoom did not react to '{labels[0]}' in the {kind} panel menu.")


SP_MANAGER = "PropertyQueryManagerForm"


def _sp_manager(main, pid: int):
    """Open (or reuse) Zoom's Smart Properties manager window (menu Valider/Validate)."""
    def find():
        return next((w for w in list(_process_windows(pid)) + list(main.children())
                     if w.element_info.automation_id == SP_MANAGER), None)
    form = find()
    if form is None:
        path = None
        for top in _menu_bar(main).children():  # locate the entry (menu is "Valider"/"Validate"...)
            top.expand()
            time.sleep(0.4)
            entry = next((c.window_text() for c in top.children()
                          if c.window_text().startswith("Smart Properties")), None)
            top.collapse()
            time.sleep(0.2)
            if entry:
                path = f"{top.window_text()} > {entry}"
                break
        if path is None:
            raise ZoomBackendError("'Smart Properties...' entry not found in Zoom's menus.")
        menu(path, pid=pid)  # a fresh expand + invoke, as in live menu
        form = _wait(find, 10)
    if form is None:
        raise ZoomBackendError("Could not open Zoom's Smart Properties manager.")
    return form


def _options_menu(window, pid: int, labels: tuple[str, ...]):
    """Click a window's options drop-down (Custom 'optionsButton') then the entry in `labels`."""
    opt = next(iter(_find_all(window, name="optionsButton", control_type="Custom", first=True)), None)
    if opt is None:
        raise ZoomBackendError("Options button not found in the Smart Properties manager.")
    item = None
    for _ in range(3):
        _click(opt)
        item = _wait(lambda: _popup_item(pid, labels), 3)
        if item is not None:
            break
    if item is None:
        raise ZoomBackendError(f"'{labels[0]}' entry not found in the options menu.")
    for _ in range(3):
        time.sleep(0.6)
        _click(item)
        if _wait(lambda: _popup_item(pid, labels) is None, 2):
            return
    raise ZoomBackendError(f"Zoom did not react to '{labels[0]}'.")


def _fill_file_dialog(pid: int, main, path: str, edit_id: str, timeout: float) -> dict | None:
    """Type a path in the Open/Save dialog and confirm. Returns None, or an error dict."""
    dlg = _wait(lambda: next((d for d in _file_dialogs(pid, main) if _by_id(d, edit_id, "Edit")), None),
                timeout)
    if dlg is None:
        raise ZoomBackendError("Zoom did not open its file dialog.")
    edit, ok_btn = _by_id(dlg, edit_id, "Edit"), _by_id(dlg, "1", "Button")
    if ok_btn is None:
        raise ZoomBackendError("Unexpected file dialog layout (OK button not found).")
    edit.set_edit_text(path)
    ok_btn.invoke()
    # Re-query instead of polling the old wrapper: UIA keeps answering for a stale element.
    closed = _wait(lambda: not any(_by_id(d, edit_id, "Edit") for d in _file_dialogs(pid, main)), timeout)
    followup = [{"title": d.window_text(), "text": _dialog_text(d)} for d in _file_dialogs(pid, main)]
    if not closed or followup:
        return {"dialogs": followup, "error": "Zoom shows a dialog; answer it in Zoom."}
    return None


def import_library(path: str, pid: int | None = None, kind: str | None = None,
                   timeout: float = 20, show: str | None = None) -> dict:
    """Import a .bcsv/.bcr/.bcl into the running Zoom session through its own Import menu.

    show: None = just import; "" = then display the file's first smart view; "Title" = that view.
    """
    pick = None
    if show is not None:
        if (kind or EXT_KIND.get(Path(path).suffix.lower())) != "smartviews":
            raise ZoomBackendError("show only applies to smart view libraries (.bcsv)")
        from cli_anything.bczoom.core import library as lib
        doc, lkind = lib.load(path)
        pairs = [(lib.title(s, lkind), lib.title(v, lkind))
                 for s in lib.sets(doc, lkind) for v in lib.items(s, lkind)]
        pick = next((p for p in pairs if not show or p[1] == show), None)
        if pick is None:
            raise ZoomBackendError(f"Smart view '{show}' is not in {path}")
        before = _set_titles(pid)
    res = _import(path, pid, kind, timeout)
    if not res["ok"] or pick is None:
        return res
    # Zoom never overwrites: a set whose title already exists is imported as "Title (1)", "(2)"...
    added = _wait(lambda: [t for t in _set_titles(res["pid"]) if t not in before], 3, 0.1) or []
    set_title = next((t for t in added if t == pick[0] or t.startswith(pick[0] + " (")), pick[0])
    if set_title != pick[0]:
        res["renamed_set"] = {"from": pick[0], "to": set_title}
    res["shown"] = show_smartview(pick[1], set_name=set_title, pid=res["pid"])
    return res


def _set_titles(pid: int | None, kind: str = "smartviews", root: str = "Local") -> list[str]:
    """Set titles currently under `root` in a panel tree."""
    _sess, main = _connect(pid)
    _open_tab(main, kind)  # a hidden panel's tree is not always exposed
    tree = _wait(lambda: _by_id(main, PANELS[kind][0]), 5)
    if tree is None:
        return []
    return [_node_title(s) for r in tree.children() if _node_title(r) == root for s in r.children()]


def _import(path: str, pid: int | None, kind: str | None, timeout: float) -> dict:
    src = Path(path).resolve()
    if not src.exists():
        raise ZoomBackendError(f"File not found: {src}")
    kind = kind or EXT_KIND.get(src.suffix.lower())
    if kind not in PANELS and kind != "smartproperties":
        raise ZoomBackendError(f"Cannot import {src.suffix} live; supported: {', '.join(EXT_KIND)}")
    sess, main = _connect(pid)
    manager = None
    if not any(_by_id(d, "1148", "Edit") for d in _file_dialogs(sess["pid"], main)):
        if kind == "smartproperties":
            manager = _sp_manager(main, sess["pid"])
            _options_menu(manager, sess["pid"], IMPORT_LABELS)
        else:
            _panel_menu(main, sess["pid"], kind, IMPORT_LABELS)
    # else: an Open dialog is already up (e.g. an interrupted import) -> reuse it
    err = _fill_file_dialog(sess["pid"], main, str(src), "1148", timeout)
    if manager is not None and not err:
        time.sleep(0.5)
        close = _by_id(manager, "buttonClose")
        if close is not None:
            close.invoke()
    if err:
        return {"ok": False, "pid": sess["pid"], "model": sess["model"], "file": str(src), "kind": kind, **err}
    return _result(sess, file=str(src), kind=kind)


# Issues panel (research axis 2: names read from Zoom 9.5's resources; to confirm live)
ISSUES_MENU = "ddb_IssuesManagement"
IMPORT_BCF_ID = "tbmi_BC_ImportBCF"
IMPORT_BCF_LABELS = ("Importer un fichier BCF", "Importer un fichier BCF...", "Import BCF file",
                     "Import BCF file...", "BCF-Datei importieren", "BCF-bestand importeren")
NO_LABELS = ("Non", "No", "Nein", "Nee")
CANCEL_LABELS = ("Annuler", "Cancel", "Abbrechen", "Annuleren")
DISCONNECT_LABELS = ("Se déconnecter", "Déconnecter", "Disconnect", "Log out", "Abmelden", "Trennen",
                     "Afmelden", "Verbinding verbreken")


def _cloud_connected(main) -> bool:
    """Heuristic: a visible disconnect/log-out control means a BIMcollab cloud project is open."""
    for label in DISCONNECT_LABELS:
        for d in _find_all(main, name=label):
            try:
                if d.is_visible() and d.rectangle().width() > 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _message_boxes(pid: int, main) -> list:
    """Modal dialogs that are not file dialogs (questions, warnings)."""
    return [d for d in _file_dialogs(pid, main) if not _by_id(d, "1148", "Edit") and not _by_id(d, "1001", "Edit")]


def _answer(dlg, labels: tuple[str, ...]) -> bool:
    for label in labels:
        b = next(iter(_find_all(dlg, name=label, control_type="Button", first=True)), None)
        if b is not None:
            b.invoke()
            return True
    return False


def import_bcf(path: str, pid: int | None = None, timeout: float = 60, on_unsaved: str = "abort",
               allow_cloud: bool = False) -> dict:
    """Import a BCF (.bcf/.bcfzip, 2.0/2.1) into the Issues panel of the open session.

    Zoom keeps imported issues locally (offline BCF mode); it cannot mix them with a connected
    BIMcollab cloud project, so this refuses when one seems connected (allow_cloud=True to force).
    If Zoom asks to save the current issues first: on_unsaved="abort" cancels the import,
    "discard" answers No (the current offline issues are replaced). Never answers Yes.
    """
    src = Path(path).resolve()
    if not src.exists():
        raise ZoomBackendError(f"File not found: {src}")
    if src.suffix.lower() not in (".bcf", ".bcfzip"):
        raise ZoomBackendError("Expected a .bcf or .bcfzip file")
    if on_unsaved not in ("abort", "discard"):
        raise ZoomBackendError("on_unsaved must be 'abort' or 'discard'")
    sess, main = _connect(pid)
    pid_ = sess["pid"]
    _open_tab(main, "issues")
    time.sleep(0.5)
    if not allow_cloud and _cloud_connected(main):
        raise ZoomBackendError("A BIMcollab cloud project seems connected in Zoom: importing a BCF would "
                               "switch the session to offline issues. Disconnect first, or pass "
                               "allow_cloud=True after the user agreed.")
    if not any(_by_id(d, "1148", "Edit") for d in _file_dialogs(pid_, main)):
        dd = _wait(lambda: _by_id(main, ISSUES_MENU), 5)
        if dd is None:
            raise ZoomBackendError(f"Issues menu {ISSUES_MENU} not found (open the Problèmes tab once?).")
        item = None
        for _ in range(3):
            _click(dd)
            item = _wait(lambda: _popup_item(pid_, IMPORT_BCF_LABELS) or next(
                (x for w in _process_windows(pid_) for x in _find_all(w, auto_id=IMPORT_BCF_ID, first=True)
                 if x.is_visible()), None), 3)
            if item is not None:
                break
        if item is None:
            raise ZoomBackendError("'Import BCF file' entry not found in the issues menu.")
        time.sleep(0.6)
        _click(item)
        # optional question: save the current issues to BCF before continuing?
        q = _wait(lambda: next(iter(_message_boxes(pid_, main)), None) or
                  next((d for d in _file_dialogs(pid_, main) if _by_id(d, "1148", "Edit")), None), 10)
        if q is not None and not _by_id(q, "1148", "Edit"):
            text = _dialog_text(q)
            if on_unsaved == "discard" and _answer(q, NO_LABELS):
                pass
            else:
                _answer(q, CANCEL_LABELS) or _answer(q, NO_LABELS)
                return {"ok": False, "pid": pid_, "model": sess["model"], "file": str(src),
                        "dialog": text, "error": "Zoom asked to save the current issues first; import "
                        "cancelled. Retry with on_unsaved='discard' if the user agrees to replace them."}
    err = _fill_file_dialog(pid_, main, str(src), "1148", timeout)
    if err:
        return {"ok": False, "pid": pid_, "model": sess["model"], "file": str(src), **err}
    return _result(sess, file=str(src), kind="bcf")


def export_library(kind: str, output: str, pid: int | None = None, force: bool = False,
                   timeout: float = 20, sets: list[str] | None = None) -> dict:
    """Export the session's local library of `kind` through Zoom's Export menu
    (Zoom always exports everything; `sets` then keeps only those sets in the file)."""
    res = _export(kind, output, pid, force, timeout)
    if res["ok"] and sets:
        from cli_anything.bczoom.core import library as lib
        from cli_anything.bczoom.core import zoomxml as zx
        doc, lkind = lib.load(res["file"])
        zx.write_file(lib.extract_sets(doc, lkind, list(sets)), res["file"])
        res["sets"] = list(sets)
    return res


def _export(kind: str, output: str, pid: int | None, force: bool, timeout: float) -> dict:
    if kind not in PANELS:
        raise ZoomBackendError(f"Unknown library kind '{kind}'; choose from {', '.join(PANELS)}")
    out = Path(output).resolve()
    if out.exists():
        if not force:
            raise ZoomBackendError(f"{out} exists; pass --force to overwrite.")
        out.unlink()  # avoid Windows' "replace?" prompt
    out.parent.mkdir(parents=True, exist_ok=True)
    sess, main = _connect(pid)
    _panel_menu(main, sess["pid"], kind, EXPORT_LABELS)
    err = _fill_file_dialog(sess["pid"], main, str(out), "1001", timeout)
    if err:
        return {"ok": False, "pid": sess["pid"], "model": sess["model"], "file": str(out), "kind": kind, **err}
    if not _wait(out.exists, timeout):
        raise ZoomBackendError(f"Zoom did not write {out}.")
    return _result(sess, file=str(out), kind=kind)


# ---------------------------------------------------------------------------
# Main menu, toolbar, search, screenshot
# ---------------------------------------------------------------------------

def _menu_bar(main):
    mm = _by_id(main, "MainMenu")
    if mm is None:
        raise ZoomBackendError("Zoom main menu not found.")
    return mm


def list_menus(pid: int | None = None) -> dict:
    sess, main = _connect(pid)
    menus = {}
    for top in _menu_bar(main).children():
        top.expand()
        _wait(lambda: top.children(), 1, 0.05)
        menus[top.window_text()] = [c.window_text() for c in top.children()
                                    if c.element_info.control_type == "MenuItem"]
        top.collapse()
    return _result(sess, menus=menus)


def menu(path: str, pid: int | None = None) -> dict:
    """Invoke a main-menu command, path 'Top > Item [> Sub]' (labels as shown in Zoom's language)."""
    parts = [p.strip() for p in path.replace("/", ">").split(">") if p.strip()]
    if len(parts) < 2:
        raise ZoomBackendError("Menu path needs at least 'Menu > Item'.")
    sess, main = _connect(pid)
    node = next((c for c in _menu_bar(main).children() if c.window_text() == parts[0]), None)
    if node is None:
        raise ZoomBackendError(f"Menu '{parts[0]}' not found.")
    for i, label in enumerate(parts[1:], start=1):
        node.expand()
        _wait(lambda: node.children(), 1, 0.05)
        child = next((c for c in node.children() if c.window_text() == label), None)
        if child is None:
            node.collapse()
            raise ZoomBackendError(f"'{label}' not found under '{' > '.join(parts[:i])}'. Entries: "
                                   + ", ".join(c.window_text() for c in node.children() if c.window_text()))
        node = child
    node.invoke()
    return _result(sess, menu=" > ".join(parts))


def list_tools(pid: int | None = None) -> dict:
    sess, main = _connect(pid)
    names = sorted({d.window_text() for d in _find_all(main, control_type="Custom")
                    if d.window_text().startswith("tb_") and d.rectangle().width() > 0})
    return _result(sess, tools=names)


def tool(name: str, pid: int | None = None) -> dict:
    """Click a toolbar command by its Zoom name (e.g. ZoomToFit or tb_ZoomToFit)."""
    name = name if name.startswith("tb_") else "tb_" + name
    sess, main = _connect(pid)
    btn = _button(main, name)
    if btn is None:
        raise ZoomBackendError(f"Toolbar command {name} not found or not visible (see 'live tools').")
    _click(btn)  # Telerik command bar buttons expose no actionable UIA pattern
    return _result(sess, tool=name)


def search(text: str, pid: int | None = None) -> dict:
    """Type in Zoom's search box (top right) and press Enter."""
    sess, main = _connect(pid)
    box = _button(main, "tb_SearchTextBox")
    if box is None:
        raise ZoomBackendError("Search box not found.")
    b = box.rectangle()
    edit = next((d for d in _find_all(main, control_type="Edit")
                 if b.left <= d.rectangle().mid_point().x <= b.right
                 and b.top <= d.rectangle().mid_point().y <= b.bottom), None)
    if edit is None:
        raise ZoomBackendError("Search text field not found.")
    edit.set_edit_text(text)
    _click(edit)
    edit.type_keys("{ENTER}")
    return _result(sess, search=text)


def screenshot(output: str, pid: int | None = None, full: bool = False) -> dict:
    """Save a PNG of the 3D view (or the whole Zoom window with full=True)."""
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise ZoomBackendError("Pillow is required for screenshots: pip install pillow") from exc
    sess, main = _connect(pid)
    time.sleep(0.15)  # let the window repaint if it just came to the front
    target = main if full else (_by_id(main, "ModelPanel") or main)
    r = target.rectangle()
    out = Path(output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True).save(out)
    return _result(sess, file=str(out), size=[r.width(), r.height()])
