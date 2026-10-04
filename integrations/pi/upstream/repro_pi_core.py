#!/usr/bin/env python3
"""Minimal reproductions of two Pi core bugs, against a scripted server (no model).

    LLM_ROUTER_PI_BIN=$(which pi) python3 repro_pi_core.py sigint
    LLM_ROUTER_PI_BIN=$(which pi) python3 repro_pi_core.py compaction

sigint      print/JSON mode: SIGINT exits Pi but the running bash command keeps going.
compaction  default compaction of a split turn keeps the huge tool results verbatim,
            moves the user's prompt into the summary, and barely shrinks the context.
Both run stock Pi: no extension is loaded.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import pi_mock_harness as h  # noqa: E402


def sigint() -> int:
    d = h.tmpdir()
    ws = d / "ws"
    ws.mkdir()
    marker = "sleep 31.5"
    srv = h.MockServer([{"tool_calls": [{"name": "bash", "arguments": {"command": f"{marker} && touch after.txt"}}]},
                        {"text": "finished"}], d)
    try:
        h.write_agent_dir(d / "agent", srv.port)
        r = h.run_pi("run it", ws, d / "agent", [], sigint_after_tool="bash")
        time.sleep(1)
        alive = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
        print(f"pi exit status: {r['rc']} (negative = killed by that signal)")
        print(f"bash command still running after Pi exited: {bool(alive)} (pids {alive})")
        print("expected: the command's process group is killed, as it is for SIGTERM and SIGHUP")
        return 1 if alive else 0
    finally:
        subprocess.run(["pkill", "-f", marker])
        srv.close()


def compaction() -> int:
    d = h.tmpdir()
    ws = d / "ws"
    ws.mkdir()
    for name, at, needle in (("a.log", 20, "AAAA1111"), ("b.log", 620, "BBBB2222")):
        lines = [f"2026-10-04T10:{i % 60:02d}:00 INFO worker-{i % 23} processed batch {1000 + i} in {i % 97}ms status=ok"
                 for i in range(640)]
        lines[at] = f"CHECKSUM={needle}"
        (ws / name).write_text("\n".join(lines) + "\n")
    request = "SENTINEL-7: read a.log and b.log fully and report both CHECKSUM= values."
    script = [{"tool_calls": [{"name": "read", "arguments": {"path": "a.log"}}, {"name": "read", "arguments": {"path": "b.log"}}]}]
    script += [{"text": "## Goal\n(summary written by the scripted server)"}] * 4 + [{"text": "final"}]
    srv = h.MockServer(script, d)
    try:
        h.write_agent_dir(d / "agent", srv.port)
        (d / "agent" / "settings.json").write_text("{}")  # stock compaction settings
        r = h.run_pi(request, ws, d / "agent", [], timeout_s=120)
        ends = [e for e in r["events"] if e.get("type") == "compaction_end" and e.get("result")]
        if not ends:
            print("no compaction happened; stderr:", r["stderr"][-800:])
            return 2
        res = ends[0]["result"]
        # The agent's requests carry tool declarations; the summarizer's do not.
        reqs = [x for x in srv.requests() if x["request"].get("tools")]
        after = reqs[-1]["request"]["messages"]
        user_texts = [m for m in after if m.get("role") == "user"]
        verbatim = any(request in str(m.get("content")) for m in user_texts)
        print(f"tokensBefore={res['tokensBefore']} estimatedTokensAfter={res['estimatedTokensAfter']}")
        print(f"next request: {len(after)} messages, {reqs[-1]['bytes']} bytes; user prompt present verbatim: {verbatim}")
        print("expected: the context shrinks well below the threshold and the user prompt survives verbatim")
        return 0 if (verbatim and res["estimatedTokensAfter"] < res["tokensBefore"] / 2) else 1
    finally:
        srv.close()


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit({"sigint": sigint, "compaction": compaction}.get(which, lambda: (print(__doc__), 2)[1])())
