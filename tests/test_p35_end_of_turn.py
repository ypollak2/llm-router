"""P35-3 carry-forward (rv-628): one line, three surfaces.

``codex-stop`` systemMessage == the snapshot line == the statusline row == what the Pi extension prints,
for the same session fixture. The Pi extension's logic (extensions/lib/summary.mjs, registered by
summary.ts) runs under a mock Pi (integrations/pi/tests/summary_harness.mjs) whenever ``node`` is on PATH;
without node the structure of the shim is still checked."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router.commands import pi as pic
from tests.test_p34a_ledger_view_identity import NOW, home  # noqa: F401  (home is a fixture)
from tests.test_p35_fixtures import (  # noqa: F401  (p35 and six are fixtures)
    HOOKS, REAL_TIME, SID, _snapshot_shell, p35, seed_snapshot_extras, six,
)

ROOT = Path(__file__).resolve().parents[1]
PI = ROOT / "integrations" / "pi"
HARNESS = PI / "tests" / "summary_harness.mjs"
NODE = shutil.which("node")


def _publish(host: str, now_wall: float) -> dict:
    """Render the snapshot for ``host`` from the frozen-clock fixture, then stamp it with the wall clock
    (the readers judge staleness on the wall clock; the fixture's clock is frozen)."""
    from llm_router import statusline_snapshot as snap

    data = snap.refresh(host=host, session_id=SID, now=NOW)
    path = snap.snapshot_path(host, SID)
    on_disk = json.loads(path.read_text())
    on_disk["written_at"] = now_wall
    path.write_text(json.dumps(on_disk))
    return data


def _codex_stop(env) -> str:
    env = {**env, "LLM_ROUTER_STATUSLINE_REFRESH_CMD": "true"}
    r = subprocess.run([sys.executable, str(HOOKS / "codex-stop.py")], input=json.dumps({"session_id": SID}),
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["systemMessage"]


def _pi(env, session=SID, ui="1") -> dict:
    env = {**env, "PI_HARNESS_UI": ui, "LLM_ROUTER_STATUSLINE_REFRESH_CMD": "true"}
    r = subprocess.run([NODE, str(HARNESS), session or "-"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


@needs_node
async def test_codex_stop_equals_snapshot_line_equals_statusline_row_equals_pi(p35):  # noqa: F811 -- pytest fixture name
    seed_snapshot_extras(p35.home)
    rows = _snapshot_shell(p35)                      # writes the claude_code snapshot, runs the real shell
    shell_rows = [r for r in rows if r.startswith("router:")]
    assert len(shell_rows) == 1, rows
    wall = REAL_TIME()
    codex = _publish("codex", wall)
    pi = _publish("pi", wall)
    claude = _publish("claude_code", wall)
    assert codex["line"] == pi["line"] == claude["line"], "one renderer: the host does not change the line"
    assert claude["line"].count(" | ") == 4 and "n/a" not in claude["line"].split(" | ")[0]

    stop_msg = _codex_stop(p35.env)
    pi_out = _pi(p35.env)
    assert stop_msg == codex["line"], (stop_msg, codex["line"])
    assert shell_rows[0] == codex["line"], (shell_rows[0], codex["line"])
    assert pi_out["notices"] == [[codex["line"], "info"]], pi_out


@needs_node
async def test_pi_prints_the_line_on_stderr_without_a_ui_and_never_on_stdout(p35):  # noqa: F811 -- pytest fixture name
    seed_snapshot_extras(p35.home)
    data = _publish("pi", REAL_TIME())
    out = _pi(p35.env, ui="0")
    assert out["notices"] == []
    assert out["stderr"].splitlines()[0] == data["line"], out


@needs_node
async def test_pi_prints_na_without_a_snapshot_stale_or_session(p35):  # noqa: F811 -- pytest fixture name
    assert _pi(p35.env)["notices"] == [["n/a", "info"]]                         # no snapshot
    _publish("pi", REAL_TIME() - 16 * 60)
    assert _pi(p35.env)["notices"] == [["n/a", "info"]]                         # older than 15 minutes
    _publish("pi", REAL_TIME() - 14 * 60)
    assert _pi(p35.env)["notices"][0][0].startswith("router:")                  # just inside the bar
    assert _pi(p35.env, session=None)["notices"] == [["n/a", "info"]]           # Pi gave no session id


@needs_node
async def test_pi_starts_one_detached_refresher_for_host_pi_and_rate_limits_it(p35):  # noqa: F811 -- pytest fixture name
    first = _pi(p35.env)
    assert first["spawned"] == [["true", ["--host", "pi", "--session", SID]]], first
    again = _pi(p35.env)
    assert again["spawned"] == [], "one refresher per 10 s"
    old = time.time() - 60
    stamp = p35.home / ".pi-summary-refresh"
    os.utime(stamp, (old, old))
    assert len(_pi(p35.env)["spawned"]) == 1
    odd = _pi(p35.env, session="../x")
    assert odd["spawned"] == [] and "not a plain id" in odd["stderr"], odd


def test_summary_extension_is_registered_for_the_main_agent_only():
    assert "summary.ts" in pic.EXTENSIONS and "summary.ts" not in pic.CHILD_EXTENSIONS
    ts = (PI / "extensions" / "summary.ts").read_text()
    assert re.search(r'from "\./lib/summary\.mjs"', ts) and "register(pi)" in ts
    argv, _ = pic.build_invocation(model="m", profile=PI, agent_dir=Path("/a"), pi_cmd=["pi"],
                                   event_log=Path("/e"), pi_args=[])
    assert str(PI / "extensions" / "summary.ts") in argv


def test_summary_module_reads_the_file_and_imports_no_llm_router():
    """Without node the shim is still checked: the module is read-only on the snapshot (no llm_router,
    no python), and names the same stale bar and refresher flags the Python side defines."""
    from llm_router import statusline_snapshot as snap

    js = (PI / "extensions" / "lib" / "summary.mjs").read_text()
    assert snap.STALE_S == 15 * 60 and "STALE_S = 15 * 60;" in js
    assert "llm_router" not in re.sub(r"//.*", "", js).replace("LLM_ROUTER_", "")
    assert '"--host", HOST, "--session"' in js and 'HOST = "pi"' in js
