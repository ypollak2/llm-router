"""P2.12-a3 (P212-8, D-315): the REAL Pi smoke, run in CI where it must never skip.

tests/test_p212_pi_mcp.py proves the installer against a fake ``pi``. This file runs the
installer as a child process (``llm-router install --auto --no-plugin``) against the real
``pi`` CLI with a temp HOME, then runs ``pi mcp list`` and checks the receipt's pi smoke.

Offline by construction: ``pi mcp list`` starts the llm_router stdio server and reads its
tool list over MCP; no provider key, no network, no model call. What it verifies: mcp.json
has the llm_router entry, Pi exits 0 and lists llm_router as connected with its tools, and
the receipt row says ``smoke.status == "passed"``.

Without ``pi`` on PATH the tests skip locally. With ``PI_SMOKE_REQUIRED=1`` (set
only by the ``pi-smoke`` CI job) a missing ``pi`` FAILS: a skip there would let the gate
row pass on nothing. conftest.py scrubs every LLM_ROUTER_* variable at import, so the flag is deliberately not
named that. No source file reads it, so it is not in env_registry.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REQUIRE = os.environ.get("PI_SMOKE_REQUIRED", "") not in ("", "0", "false", "no")
PI = shutil.which("pi")
EXPECTED_TOOLS = {"llm_route", "llm_edit", "llm", "llm_act"}  # a subset; the server may add more


@pytest.fixture(scope="module", autouse=True)
def _pi_present():
    if PI is None:
        if REQUIRE:
            pytest.fail("PI_SMOKE_REQUIRED is set but `pi` is not on PATH: the CI Pi smoke must not skip")
        pytest.skip("pi CLI not on PATH (set PI_SMOKE_REQUIRED=1 to make this a failure)")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    home = tmp_path_factory.mktemp("pi_smoke_home")
    # llm_router must resolve as a command, the way mcp.json names it ("llm-router").
    bindirs = [str(Path(sys.executable).parent), str(Path(PI).parent)]
    env = {
        "HOME": str(home),
        "LLM_ROUTER_HOME": str(home / ".llm-router"),
        "PATH": os.pathsep.join(bindirs + ["/usr/bin", "/bin"]),
        "LLM_ROUTER_NO_DETACHED_SPAWN": "1",  # the installer's background model probe never starts
        # every Ollama base-URL variable the installer or router reads, pointed at a dead port
        **{k: "http://127.0.0.1:9" for k in ("OLLAMA_HOST", "OLLAMA_URL", "OLLAMA_BASE_URL", "OLLAMA_API_BASE", "OLLAMA_BASE")},
        "NO_COLOR": "1",
    }
    assert Path(shutil.which("llm-router", path=env["PATH"]) or "").exists(), "llm-router not on the child PATH"
    install = subprocess.run(["llm-router", "install", "--auto", "--no-plugin"], env=env, cwd=home,
                             capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    listing = subprocess.run([PI, "mcp", "list", "--json"], env=env, cwd=home, capture_output=True,
                             text=True, timeout=120, stdin=subprocess.DEVNULL)
    text = subprocess.run([PI, "mcp", "list"], env=env, cwd=home, capture_output=True, text=True,
                          timeout=120, stdin=subprocess.DEVNULL)
    print("pi mcp list:\n" + text.stdout)  # lands in the CI log and the junit system-out
    return {"home": home, "install": install, "list": listing, "text": text}


def test_installer_wrote_mcp_json_in_the_temp_home(run):
    path = run["home"] / ".pi" / "agent" / "mcp.json"
    assert path.exists(), run["install"].stdout[-800:] + run["install"].stderr[-400:]
    assert json.loads(path.read_text())["mcpServers"]["llm_router"] == {"command": "llm-router", "args": []}


def test_pi_mcp_list_exits_zero_and_lists_llm_router_tools(run):
    assert run["list"].returncode == 0, run["list"].stdout + run["list"].stderr
    servers = {s["name"]: s for s in json.loads(run["list"].stdout)["servers"]}
    assert servers["llm_router"]["state"] == "connected", servers["llm_router"]
    assert EXPECTED_TOOLS <= set(servers["llm_router"]["tools"]), servers["llm_router"]["tools"]
    assert run["text"].returncode == 0
    assert "llm_router: connected" in run["text"].stdout


def test_receipt_pi_smoke_passed_not_skipped(run):
    receipt = json.loads((run["home"] / ".llm-router" / "install_receipt.json").read_text())
    row = receipt["hosts"]["pi"]
    assert row["status"] == "wired", row
    assert row["smoke"]["status"] == "passed", row
    assert "llm_router connected" in row["smoke"]["detail"], row
