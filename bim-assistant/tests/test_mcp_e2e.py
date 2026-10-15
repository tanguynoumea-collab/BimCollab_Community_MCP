"""End-to-end: a real MCP client talks to the server over stdio."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import build_model

SERVER = [sys.executable, "-m", "bim_assistant.server"]


async def _session(fn, env):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=SERVER[0], args=SERVER[1:], env=env,
                                   cwd=str(Path(__file__).parent.parent))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


def _payload(res):
    if getattr(res, "structured_content", None) is not None:
        sc = res.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    sc = getattr(res, "structuredContent", None)
    if sc is not None:
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


def _env(tmp_path):
    env = dict(os.environ)
    env.update({"BCZOOM_STATE_DIR": str(tmp_path / "state"), "PYTHONIOENCODING": "utf-8"})
    return env


def test_tools_listed_and_workflow(tmp_path):
    ifc = build_model(tmp_path / "demo.ifc")
    lib_path = tmp_path / "assistant.bcsv"

    async def run(s):
        tools = {t.name for t in (await s.list_tools()).tools}
        out = {"tools": tools}
        out["open"] = _payload(await s.call_tool("ifc_open", {"path": str(ifc)}))
        out["count"] = _payload(await s.call_tool("ifc_count", {
            "model": "demo", "selector": "IfcDoor", "group_by": ["Données.#EI_FEU_CIBLE"]}))
        out["sv"] = _payload(await s.call_tool("zoom_smartview_color_by_values", {
            "library": str(lib_path), "set_name": "Assistant", "title": "Portes EI", "model": "demo",
            "ifc_class": "IfcDoor", "pset": "Données", "prop": "#EI_FEU_CIBLE"}))
        err = await s.call_tool("ifc_summary", {"model": "inconnu"})
        out["err"] = (err.is_error if hasattr(err, "is_error") else err.isError, err.content[0].text)
        return out

    t = time.time()
    out = asyncio.run(_session(run, _env(tmp_path)))
    print(f"\n  MCP round-trip {time.time() - t:.1f}s, {len(out['tools'])} tools")
    assert {"ifc_open", "ifc_audit", "ifc_compare", "zoom_smartview_color_by_values",
            "zoom_nightshift_run", "zoom_install_library"} <= out["tools"]
    assert out["open"]["element_count"] == 8
    assert out["count"]["counts"] == {"EI30": 2, "NC": 1, "(vide)": 1}
    assert out["sv"]["legend"]["EI30"] and lib_path.exists()
    assert out["err"][0] and "not loaded" in out["err"][1]


def test_real_zoom_status(tmp_path):
    """Uses the real BIMcollab Zoom install (fails, not skips, if Zoom is missing)."""
    async def run(s):
        return _payload(await s.call_tool("zoom_status", {}))
    st = asyncio.run(_session(run, _env(tmp_path)))
    assert Path(st["exe"]).exists() and st["version"] != "unknown"


@pytest.mark.gui
def test_real_nightshift_open(tmp_path):
    """Starts the real Zoom via Nightshift (no cloud action). Zoom must be closed."""
    ifc = Path(__file__).parent.parent.parent / "bimcollab-zoom" / "agent-harness" / \
        "cli_anything" / "bczoom" / "tests" / "fixtures" / "sample.ifc"

    async def run(s):
        return _payload(await s.call_tool("zoom_nightshift_run", {"models": [str(ifc)], "timeout_s": 600}))
    res = asyncio.run(_session(run, _env(tmp_path)))
    assert res["ok"] and res["exit_code"] == 0, res
    assert res["logs"][0]["actions"][0]["execution_status"] == "OK"
