"""Q: `llm-router routing-health` counts what matters, over prompts a person typed.

The existing routing-report counts drafts PRODUCED; 0 of 1,191 had been used
(2026-09-24). This report puts USED beside reach, and its denominator excludes
what the 10-day replay showed the hook also sees: /tmp benchmark sessions,
sub-agent reports, background-task notifications and fixture session ids.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

from llm_router import routing_health

ROOT = Path(__file__).resolve().parents[1]
D = "2026-09-24"


def _log(tmp_path) -> Path:
    L = []
    def inv(i, sid, head_tags="", *rest):
        L.append(f"[{D} 10:00:00] [INVOCATION {i}] prompt_len=40 session_id={sid}{head_tags}")
        for r in rest:
            L.append(f"[{D} 10:00:01] [INVOCATION {i}] {r}")
    inv("1.1", "a1b2c3d4", "", "DIRECT: zone=green", "DIRECT SUCCESS: model=ollama/q latency=4000ms files_read=3")
    inv("1.2", "a1b2c3d4", "", "DRAFT USED: the draft from invocation 1.1", "DIRECT: zone=green",
        "DIRECT SUCCESS: model=ollama/q latency=8000ms files_read=0")
    inv("1.3", "a1b2c3d4", "", "DRAFT UNUSED: the draft from invocation 1.2", "DIRECT SKIP: context-dependent prompt")
    inv("1.4", "e5f6a7b8", " sandbox=1", "DIRECT SUCCESS: model=ollama/q latency=1ms files_read=9")
    inv("1.5", "e5f6a7b8", " kind=agent-report", "DIRECT SUCCESS: model=ollama/q latency=1ms files_read=9")
    inv("1.6", "e5f6a7b8", "", "SYSTEM_NOTIFICATION_BYPASS — background-task notification")
    inv("1.7", "sess-abc", "", "DIRECT SUCCESS: model=ollama/q latency=1ms files_read=9")
    p = tmp_path / "auto-route-debug.log"
    p.write_text("\n".join(L) + "\n")
    return p


def test_counts_only_human_prompts_and_reports_use(tmp_path):
    rows = routing_health.summarize(_log(tmp_path), days=1, today=dt.date.fromisoformat(D))
    r = rows[D]
    assert r["prompts"] == 3, "sandbox, agent report, notification and fixture excluded"
    assert r["drafted"] == 2 and r["read_files"] == 1
    assert (r["used"], r["judged"]) == (1, 2)
    assert sorted(r["latencies"]) == [4000, 8000]


def test_the_script_and_the_command_share_one_parser(tmp_path):
    log = _log(tmp_path)
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "routing_rate.py"),
                          "--file", str(log)], capture_output=True, text=True, timeout=60).stdout
    # is_real (shared): the fixture id is excluded, the 6 hex-session prompts are not.
    assert f"{D}" in out and " 6 " in out.split(D)[1].split("\n")[0], out


# ── the hook writes the tags the report depends on ───────────────────────────

HOOK = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"


def _hook_log(monkeypatch, tmp_path, prompt, cwd):
    spec = importlib.util.spec_from_file_location("auto_route_q", HOOK)
    ar = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_q"] = ar
    spec.loader.exec_module(ar)
    monkeypatch.setattr(ar, "log_routing_decision", lambda **kw: None, raising=False)
    for k, v in {"LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1", "LLM_ROUTER_DIRECT_EXECUTION": "1",
                 "LLM_ROUTER_ENFORCE": "suggest"}.items():
        monkeypatch.setenv(k, v)
    import llm_router.hooks.chain_builder as cb
    import llm_router.hooks.direct_executor as de
    model = de.ModelSpec(provider="ollama", model="fake-model")
    monkeypatch.setattr(cb, "get_current_pressure", lambda: ("green", 10.0))
    monkeypatch.setattr(cb, "build_chain", lambda c, z, t: [model])
    monkeypatch.setattr(cb, "needs_claude_tools", lambda p, t: False)
    res = de.DirectResult(text="os.path.join joins path components into one path.",
                          model=model, latency_ms=5, files_read=("read_file(a.py)", "search_files(x)"))
    monkeypatch.setattr(de, "execute_chain", lambda *a, **k: res)
    monkeypatch.setattr(de, "execute_agent", lambda *a, **k: res)
    log: list[str] = []
    monkeypatch.setattr(ar, "_debug_log", log.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": prompt, "session_id": "a1b2c3d4-0000", "cwd": cwd})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        ar.main()
    except SystemExit:
        pass
    return log


def test_the_hook_tags_sandbox_and_agent_reports(monkeypatch, tmp_path):
    head = lambda log: next(x for x in log if "prompt_len=" in x)  # noqa: E731
    assert " sandbox=1" in head(_hook_log(monkeypatch, tmp_path, "What does os.path.join do?",
                                          "/private/tmp/bq_claude"))
    plain = head(_hook_log(monkeypatch, tmp_path, "What does os.path.join do?", "/Users/someone/project"))
    assert "sandbox" not in plain and "kind=" not in plain, "premise: an ordinary prompt is untagged"
    assert " kind=agent-report" in head(_hook_log(
        monkeypatch, tmp_path, "Another Claude session sent a message: <agent-message from=x>", "/Users/x"))


def test_the_hook_logs_how_many_files_a_draft_read(monkeypatch, tmp_path):
    log = _hook_log(monkeypatch, tmp_path, "What does os.path.join do?", "/Users/someone/project")
    ok = [x for x in log if "DIRECT SUCCESS:" in x]
    assert ok and "files_read=2" in ok[0], ok


# ── U2: the llm(...) path — calls Claude routed, by where they ran ───────────

def test_routed_calls_split_local_claude_other_and_skip_simulated(tmp_path):
    import sqlite3
    db = tmp_path / "usage.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE usage (timestamp TEXT, provider TEXT, is_simulated INTEGER)")
    rows = [(f"{D} 10:00:00", "ollama", 0), (f"{D} 10:01:00", "ollama", 0),
            (f"{D} 10:02:00", "cc", 0), (f"{D} 10:03:00", "codex", 0),
            (f"{D} 10:04:00", "ollama", 1)]            # simulated: excluded
    con.executemany("INSERT INTO usage VALUES (?,?,?)", rows)
    con.commit()
    con.close()
    out = routing_health.routed_calls(days=1, db=db, today=dt.date.fromisoformat(D))
    # 'cc' = a Claude Code sub-agent finishing (cc-usage-track.py), not routing
    assert (out[D]["calls"], out[D]["local"], out[D]["other"], out[D]["subagents"]) == (3, 2, 1, 1)
