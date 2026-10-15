"""Backend: drive the real BIMcollab Zoom executable.

Zoom has no headless/scripting API except **Nightshift**:
``"BIMcollab Zoom.exe" -robotscript=<script.json>`` starts Zoom, runs the
script's actions, writes a log under
``%APPDATA%\\KUBUS\\BIMcollab Zoom\\Nightshift`` and exits. Other switches found
in the binaries: ``-nosplash``, ``-noscaling``.

Requirements: Windows, BIMcollab Zoom >= 5.2 with an active licence. Cloud
actions additionally need the project in Zoom's recent list with saved
credentials ("Remember me").
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from cli_anything.bczoom.core.library import zoom_data_dir

DEFAULT_EXE = r"C:\Program Files\KUBUS\BIMcollab Zoom\BIMcollab Zoom.exe"
INSTALL_HELP = (
    "BIMcollab Zoom is not installed or not found.\n"
    "  Download: https://www.bimcollab.com/en/products/bimcollab-zoom/\n"
    "  Or set BCZOOM_EXE to the full path of 'BIMcollab Zoom.exe'."
)
PROCESS_NAME = "BIMcollab Zoom.exe"
TASK_FOLDER = "bczoom"


class ZoomBackendError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_zoom() -> str:
    env = os.environ.get("BCZOOM_EXE")
    if env:
        if Path(env).exists():
            return env
        raise ZoomBackendError(f"BCZOOM_EXE points to a missing file: {env}\n{INSTALL_HELP}")
    if sys.platform != "win32":
        raise ZoomBackendError("BIMcollab Zoom only runs on Windows.\n" + INSTALL_HELP)
    candidates = [DEFAULT_EXE]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if base:
            candidates.append(os.path.join(base, "KUBUS", "BIMcollab Zoom", "BIMcollab Zoom.exe"))
    for c in candidates:
        if Path(c).exists():
            return c
    which = shutil.which("BIMcollab Zoom.exe")
    if which:
        return which
    raise ZoomBackendError(INSTALL_HELP)


def zoom_version(exe: str | None = None) -> str:
    exe = exe or find_zoom()
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Item -LiteralPath '{exe}').VersionInfo.ProductVersion"],
                capture_output=True, text=True, timeout=30)
            v = out.stdout.strip()
            if v:
                return v
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "unknown"


def running_instances() -> list[int]:
    if sys.platform != "win32":
        return []
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {PROCESS_NAME}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True)
    pids = []
    for line in out.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == PROCESS_NAME.lower():
            try:
                pids.append(int(parts[1].strip('"')))
            except ValueError:
                pass
    return pids


def info() -> dict:
    exe = find_zoom()
    data = zoom_data_dir()
    return {"exe": exe, "version": zoom_version(exe), "running_pids": running_instances(),
            "data_dir": str(data), "nightshift_log_dir": str(nightshift_log_dir()),
            "licence_files": [p.name for p in data.glob("*.lic")] if data.exists() else []}


# ---------------------------------------------------------------------------
# GUI launch
# ---------------------------------------------------------------------------

def launch(files: list[str] | None = None, nosplash: bool = True) -> dict:
    exe = find_zoom()
    args = [exe]
    if nosplash:
        args.append("-nosplash")
    for f in files or []:
        if not Path(f).exists():
            raise ZoomBackendError(f"File not found: {f}")
        args.append(str(Path(f).resolve()))
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(args, creationflags=flags, close_fds=True)
    return {"pid": proc.pid, "command": args}


# ---------------------------------------------------------------------------
# Nightshift
# ---------------------------------------------------------------------------

def nightshift_log_dir() -> Path:
    return zoom_data_dir() / "Nightshift"


def _short_path(path: str) -> str:
    """8.3 short path on Windows, to keep spaces out of -robotscript=."""
    if sys.platform != "win32" or " " not in path:
        return path
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
    return buf.value if 0 < n < 1024 else path


def stage_script(script_path: str) -> str:
    """Copy the script to a space-free staging folder; return the path to pass to Zoom."""
    src = Path(script_path).resolve()
    if not src.exists():
        raise ZoomBackendError(f"Script not found: {src}")
    stage = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "bczoom" / "run"
    stage.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", src.name)
    dst = stage / name
    shutil.copyfile(src, dst)
    return _short_path(str(dst))


def build_command(script_path: str, nosplash: bool = True) -> list[str]:
    exe = find_zoom()
    args = [exe, f"-robotscript={stage_script(script_path)}"]
    if nosplash:
        args.append("-nosplash")
    return args


def list_logs(limit: int = 20, since: float | None = None) -> list[dict]:
    d = nightshift_log_dir()
    if not d.exists():
        return []
    files = [p for p in d.rglob("*") if p.is_file()]
    if since is not None:
        files = [p for p in files if p.stat().st_mtime >= since - 1]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"path": str(p), "name": p.name, "size": p.stat().st_size,
             "modified": datetime.fromtimestamp(p.stat().st_mtime).replace(microsecond=0).isoformat()}
            for p in files[:limit]]


_FAIL_RE = re.compile(r"(?i)\b(fail(ed|ure)?|error|exception|could not|incorrect|missing)\b")


def parse_log(path: str) -> dict:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    # Zoom 9.x writes <timestamp>_RobotScriptOutput.JSON:
    # {status, error_code, total_time-sec, actions: [{original_type, output: {parsing_status,
    #  execution_status, error_code, time-elapsed-ms}}]}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and "status" in data:
        actions = [{"type": a.get("original_type"), **(a.get("output") or {})}
                   for a in data.get("actions", [])]
        failures = [a for a in actions
                    if a.get("parsing_status") != "OK" or a.get("execution_status") != "OK"
                    or a.get("error_code", 0) != 0]
        return {"path": path, "title": data.get("original_title"), "status": data.get("status"),
                "error_code": data.get("error_code"), "total_time_s": data.get("total_time-sec"),
                "actions": actions, "failures": failures,
                "ok": data.get("status") == "OK" and data.get("error_code", 0) == 0 and not failures,
                "text": "\n".join(lines[-200:])}
    failures = [l for l in lines if _FAIL_RE.search(l)]
    return {"path": path, "lines": len(lines), "failures": failures,
            "ok": not failures, "text": "\n".join(lines[-200:])}


def run_nightshift(script_path: str, timeout: int = 1800, allow_running: bool = False,
                   nosplash: bool = True) -> dict:
    """Run a Nightshift script with the real Zoom and wait for it to exit."""
    running = running_instances()
    if running and not allow_running:
        raise ZoomBackendError(
            f"BIMcollab Zoom is already running (PID {running}). Nightshift actions such as "
            "OPEN_MODELS replace the loaded models. Close Zoom first, or pass --allow-running.")
    args = build_command(script_path, nosplash=nosplash)
    started = time.time()
    proc = subprocess.Popen(args)
    try:
        code = proc.wait(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        code = None
        timed_out = True
    duration = round(time.time() - started, 1)
    logs = list_logs(limit=10, since=started)
    parsed = [parse_log(l["path"]) for l in logs]
    ok = (not timed_out) and code == 0 and all(p["ok"] for p in parsed)
    return {"command": args, "exit_code": code, "timed_out": timed_out,
            "duration_s": duration, "logs": parsed, "ok": ok}


# ---------------------------------------------------------------------------
# Windows Task Scheduler
# ---------------------------------------------------------------------------

def schedule_command(name: str, script_path: str, at: str, frequency: str = "DAILY",
                     days: str | None = None) -> list[str]:
    if not re.fullmatch(r"\d{2}:\d{2}", at):
        raise ZoomBackendError("--at must be HH:MM (24h)")
    frequency = frequency.upper()
    if frequency not in ("ONCE", "DAILY", "WEEKLY"):
        raise ZoomBackendError("--frequency must be ONCE, DAILY or WEEKLY")
    exe = find_zoom()
    script = _short_path(str(Path(script_path).resolve()))
    if " " in script:
        raise ZoomBackendError("Script path contains spaces and no 8.3 short name is available")
    tr = f'"{exe}" -robotscript={script} -nosplash'
    cmd = ["schtasks", "/Create", "/F", "/TN", f"{TASK_FOLDER}\\{name}", "/TR", tr,
           "/SC", frequency, "/ST", at]
    if frequency == "WEEKLY" and days:
        cmd += ["/D", days]
    return cmd


def run_schtasks(cmd: list[str]) -> dict:
    out = subprocess.run(cmd, capture_output=True, text=True)
    return {"command": cmd, "exit_code": out.returncode,
            "stdout": out.stdout.strip(), "stderr": out.stderr.strip()}


def list_tasks() -> list[dict]:
    out = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"], capture_output=True, text=True)
    tasks = []
    for line in out.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if parts and parts[0].lstrip("\\").lower().startswith(TASK_FOLDER + "\\"):
            tasks.append({"name": parts[0], "next_run": parts[1] if len(parts) > 1 else "",
                          "status": parts[2].strip('"') if len(parts) > 2 else ""})
    return tasks
