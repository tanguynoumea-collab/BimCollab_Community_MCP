"""Stateful editing session over one Zoom document.

The "project" of this CLI is the native file being edited:

* rule libraries  .bcsv / .bcr / .bcl / .bcsp
* Zoom projects   .bcp
* Nightshift scripts  .json

Undo/redo history is persisted per document under
``%LOCALAPPDATA%\\bczoom\\history`` (never next to the user's files), so
one-shot commands can be undone from a later invocation.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from cli_anything.bczoom.core import library, nightshift, zoomxml as zx

MAX_HISTORY = 25
LIBRARY_EXTS = set(library.EXT_TO_KIND)


def doc_type_for(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in LIBRARY_EXTS:
        return "library"
    if ext == ".bcp":
        return "project"
    if ext == ".json":
        return "nightshift"
    raise ValueError(f"Unsupported file type '{ext}'. Expected .bcsv/.bcr/.bcl/.bcsp, .bcp or .json")


def _history_dir() -> Path:
    base = os.environ.get("BCZOOM_STATE_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA", str(Path.home())), "bczoom")
    return Path(base) / "history"


def _locked_save_json(path: Path, data, **dump_kwargs) -> None:
    """Write JSON with an exclusive lock (msvcrt on Windows, fcntl elsewhere)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(path, "r+", encoding="utf-8")
    except FileNotFoundError:
        f = open(path, "w", encoding="utf-8")
    with f:
        locked = None
        try:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            locked = "msvcrt"
        except (ImportError, OSError):
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                locked = "fcntl"
            except (ImportError, OSError):
                pass
        try:
            f.seek(0)
            f.truncate()
            json.dump(data, f, **dump_kwargs)
            f.flush()
        finally:
            if locked == "msvcrt":
                import msvcrt
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            elif locked == "fcntl":
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class Session:
    def __init__(self):
        self.project_path: str | None = None
        self.doc_type: str | None = None
        self.obj = None                  # ZoomDocument or dict (nightshift)
        self._modified = False
        self._undo: list[str] = []
        self._redo: list[str] = []
        self._log: list[dict] = []

    # -- lifecycle ---------------------------------------------------------
    def has_project(self) -> bool:
        return self.obj is not None

    def open(self, path: str, create_if_missing: bool = False) -> None:
        self.doc_type = doc_type_for(path)
        p = Path(path)
        if not p.exists():
            if not create_if_missing:
                raise FileNotFoundError(f"File not found: {path}")
            raise FileNotFoundError(f"File not found: {path} (create it with a 'new' command)")
        self.project_path = str(p.resolve())
        self.obj = self._load(self.project_path)
        self._modified = False
        self._load_history()

    def attach_new(self, path: str, obj) -> None:
        """Attach a freshly created document (written on save)."""
        self.doc_type = doc_type_for(path)
        self.project_path = str(Path(path).resolve())
        self.obj = obj
        self._modified = True
        self._undo, self._redo, self._log = [], [], []
        self._record("new")

    def _load(self, path: str):
        if self.doc_type == "nightshift":
            return nightshift.load(path)
        if self.doc_type == "project":
            from cli_anything.bczoom.core import project
            return project.load(path)
        doc, _ = library.load(path)
        return doc

    # -- serialization helpers --------------------------------------------
    def _dump(self, obj=None) -> str:
        obj = self.obj if obj is None else obj
        if self.doc_type == "nightshift":
            return json.dumps(obj, ensure_ascii=False)
        return zx.serialize(obj)

    def _restore(self, text: str):
        if self.doc_type == "nightshift":
            return json.loads(text)
        return zx.parse_string(text)

    # -- history -----------------------------------------------------------
    def _history_path(self) -> Path:
        h = hashlib.sha1(self.project_path.lower().encode("utf-8")).hexdigest()[:16]
        return _history_dir() / f"{h}.json"

    def _load_history(self) -> None:
        self._undo, self._redo, self._log = [], [], []
        hp = self._history_path()
        if not hp.exists():
            return
        try:
            data = json.loads(hp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        # Discard history if the file was changed outside the CLI.
        if data.get("current") != self._dump():
            return
        self._undo = data.get("undo", [])
        self._redo = data.get("redo", [])
        self._log = data.get("log", [])

    def _save_history(self) -> None:
        _locked_save_json(self._history_path(), {
            "path": self.project_path, "current": self._dump(),
            "undo": self._undo[-MAX_HISTORY:], "redo": self._redo[-MAX_HISTORY:],
            "log": self._log[-200:]}, ensure_ascii=False)

    def _record(self, label: str) -> None:
        self._log.append({"time": datetime.now().replace(microsecond=0).isoformat(),
                          "command": label})

    def snapshot(self, label: str) -> None:
        """Call BEFORE mutating self.obj."""
        self._require()
        self._undo.append(self._dump())
        self._undo = self._undo[-MAX_HISTORY:]
        self._redo.clear()
        self._modified = True
        self._record(label)

    def undo(self) -> bool:
        self._require()
        if not self._undo:
            return False
        self._redo.append(self._dump())
        self.obj = self._restore(self._undo.pop())
        self._modified = True
        self._record("undo")
        return True

    def redo(self) -> bool:
        self._require()
        if not self._redo:
            return False
        self._undo.append(self._dump())
        self.obj = self._restore(self._redo.pop())
        self._modified = True
        self._record("redo")
        return True

    # -- persistence -------------------------------------------------------
    def save_session(self, path: str | None = None) -> str:
        self._require()
        target = str(Path(path).resolve()) if path else self.project_path
        if self.doc_type == "nightshift":
            nightshift.save(self.obj, target)
        else:
            zx.write_file(self.obj, target)
        if target == self.project_path:
            self._save_history()
        self._modified = False
        return target

    def status(self) -> dict:
        d = {"open": self.has_project(), "path": self.project_path, "type": self.doc_type,
             "modified": self._modified, "undo_depth": len(self._undo),
             "redo_depth": len(self._redo)}
        if self.has_project() and self.doc_type == "library":
            d["kind"] = library.detect_kind(self.obj, self.project_path).name
        return d

    def history(self) -> list[dict]:
        return list(self._log)

    def _require(self) -> None:
        if not self.has_project():
            raise RuntimeError("No document open. Use --project <file> or a 'new'/'open' command.")


_SESSION: Session | None = None


def get_session() -> Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = Session()
    return _SESSION


def reset_session() -> None:
    global _SESSION
    _SESSION = None
