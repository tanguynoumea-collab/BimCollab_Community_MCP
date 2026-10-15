"""BIMcollab Zoom Nightshift scripts (JSON consumed by ``-robotscript=``).

The seven action types are the complete set implemented by
BIMcollab.Zoom.Core.dll (Zoom 9.5); see
https://helpcenter.bimcollab.com/en/articles/356460-create-a-json-script-for-zoom-nightshift
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ACTION_TYPES = ["OPEN_MODELS", "ADD_TO_PROJECT", "CONNECT_TO_PROJECT", "FILTER_ISSUES",
                "UPDATE_ISSUES", "RUN_AND_SHARE_LISTS", "SYNCHRONIZE_PROJECT"]
# Actions that touch the BIMcollab Nexus cloud project (need connection + rights).
CLOUD_ACTIONS = {"CONNECT_TO_PROJECT", "FILTER_ISSUES", "UPDATE_ISSUES",
                 "RUN_AND_SHARE_LISTS", "SYNCHRONIZE_PROJECT"}
WRITE_ACTIONS = {"UPDATE_ISSUES", "RUN_AND_SHARE_LISTS", "SYNCHRONIZE_PROJECT"}

FILTER_DEFAULTS = {
    "filter_type": "All", "area": "All", "milestone": "All", "label": "All", "priority": "All",
    "show_active": True, "show_resolved": True, "show_closed": False,
    "hide_invisible": False, "show_to_approve": False,
    "assigned_to_user_email": "", "deadline": "", "created_by_user_email": "",
    "custom1": "", "custom2": "",
}
UPDATE_DEFAULTS = {"close_where_clashes_solved": True,
                   "reopen_previously_closed_issues": True, "comment": ""}


class NightshiftError(ValueError):
    pass


def new_script(title: str, version: str = "0.1") -> dict:
    if not title:
        raise NightshiftError("A script needs a title")
    return {"title": title, "version": version, "actions": []}


def load(path: str | os.PathLike) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Script not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise NightshiftError(f"Invalid JSON in {p}: {exc}") from exc
    if not isinstance(data, dict) or "actions" not in data:
        raise NightshiftError(f"{p} is not a Nightshift script (missing 'actions')")
    return data


def save(script: dict, path: str | os.PathLike) -> None:
    p = Path(path)
    if " " in p.name:
        raise NightshiftError("Zoom requires a script file name without spaces")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(script, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def zoom_path(path: str) -> str:
    """Zoom requires forward slashes in model paths."""
    return str(Path(path).resolve()).replace("\\", "/")


# ---------------------------------------------------------------------------
# Action builders
# ---------------------------------------------------------------------------

def action_open_models(models: list[str], add: bool = False, check_exists: bool = True) -> dict:
    if not models:
        raise NightshiftError("At least one model path is required")
    paths = []
    for m in models:
        if check_exists and not Path(m).exists():
            raise NightshiftError(f"Model not found: {m}")
        paths.append(zoom_path(m))
    return {"type": "ADD_TO_PROJECT" if add else "OPEN_MODELS", "models": paths}


def action_connect(space_name: str, project_name: str) -> dict:
    if not space_name or not project_name:
        raise NightshiftError("CONNECT_TO_PROJECT needs space_name and project_name")
    return {"type": "CONNECT_TO_PROJECT", "space_name": space_name, "project_name": project_name}


def action_filter_issues(**kwargs) -> dict:
    act = {"type": "FILTER_ISSUES"}
    for key, val in kwargs.items():
        if key not in FILTER_DEFAULTS:
            raise NightshiftError(f"Unknown FILTER_ISSUES option '{key}'")
        if val is None or val == FILTER_DEFAULTS[key]:
            continue
        act[key] = val
    if act.get("deadline") and not re.fullmatch(r"\d{2}-\d{2}-\d{4}", act["deadline"]):
        raise NightshiftError("deadline must be dd-mm-yyyy")
    # Zoom docs: the priority line may not be omitted.
    act.setdefault("priority", "All")
    return act


def action_update_issues(close_where_clashes_solved: bool = True,
                         reopen_previously_closed_issues: bool = True,
                         comment: str = "") -> dict:
    return {"type": "UPDATE_ISSUES",
            "close_where_clashes_solved": close_where_clashes_solved,
            "reopen_previously_closed_issues": reopen_previously_closed_issues,
            "comment": comment}


def action_run_lists(list_set_name: str | None = None) -> dict:
    act = {"type": "RUN_AND_SHARE_LISTS"}
    if list_set_name:
        act["list_set_name"] = list_set_name
    return act


def action_sync() -> dict:
    return {"type": "SYNCHRONIZE_PROJECT"}


def append(script: dict, action: dict) -> int:
    acts = script.setdefault("actions", [])
    if acts and acts[-1].get("type") == "SYNCHRONIZE_PROJECT" and action["type"] != "SYNCHRONIZE_PROJECT":
        # Keep SYNCHRONIZE_PROJECT last, as Zoom requires.
        acts.insert(len(acts) - 1, action)
        return len(acts) - 2
    acts.append(action)
    return len(acts) - 1


def remove(script: dict, index: int) -> dict:
    acts = script.get("actions", [])
    if not 0 <= index < len(acts):
        raise NightshiftError(f"No action at index {index} (script has {len(acts)})")
    return acts.pop(index)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(script: dict, check_files: bool = True) -> list[dict]:
    issues = []

    def add(level, where, msg):
        issues.append({"level": level, "where": where, "message": msg})

    if not script.get("title"):
        add("error", "script", "Missing 'title'")
    acts = script.get("actions")
    if not isinstance(acts, list) or not acts:
        add("error", "script", "'actions' must be a non-empty list")
        return issues
    types = [a.get("type") for a in acts]
    connected = False
    for i, a in enumerate(acts):
        where = f"action {i} ({a.get('type')})"
        t = a.get("type")
        if t not in ACTION_TYPES:
            add("error", where, f"Unknown type. Allowed: {ACTION_TYPES}")
            continue
        if t in ("OPEN_MODELS", "ADD_TO_PROJECT"):
            models = a.get("models")
            if not isinstance(models, list) or not models:
                add("error", where, "'models' must be a non-empty list")
            else:
                for m in models:
                    if "\\" in m:
                        add("error", where, f"Use forward slashes in model path: {m}")
                    if check_files and not Path(m).exists():
                        add("warning", where, f"Model file not found on this machine: {m}")
        if t == "CONNECT_TO_PROJECT":
            connected = True
            for k in ("space_name", "project_name"):
                if not a.get(k):
                    add("error", where, f"Missing '{k}'")
        if t in CLOUD_ACTIONS - {"CONNECT_TO_PROJECT"} and not connected:
            add("warning", where, "Runs before any CONNECT_TO_PROJECT: relies on Zoom's current connection")
        if t == "FILTER_ISSUES":
            for k in a:
                if k != "type" and k not in FILTER_DEFAULTS:
                    add("error", where, f"Unknown option '{k}'")
            if "priority" not in a:
                add("error", where, "'priority' must be present (Zoom requirement)")
            dl = a.get("deadline")
            if dl and not re.fullmatch(r"\d{2}-\d{2}-\d{4}", dl):
                add("error", where, "deadline must be dd-mm-yyyy")
        if t == "RUN_AND_SHARE_LISTS" and "list_set-name" in a:
            add("warning", where, "Key 'list_set-name' (hyphen) appears in Zoom docs; "
                                  "'list_set_name' is the documented parameter")
    if "SYNCHRONIZE_PROJECT" in types and types[-1] != "SYNCHRONIZE_PROJECT":
        add("error", "script", "SYNCHRONIZE_PROJECT must be the last action")
    return issues


def describe(script: dict) -> dict:
    acts = script.get("actions", [])
    types = {a.get("type") for a in acts}
    return {"title": script.get("title"), "version": script.get("version"),
            "action_count": len(acts),
            "actions": [{"index": i, **a} for i, a in enumerate(acts)],
            "touches_cloud": bool(types & CLOUD_ACTIONS),
            "writes_to_cloud": bool(types & WRITE_ACTIONS)}
