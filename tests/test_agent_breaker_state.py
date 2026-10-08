"""Agent breaker state: pending-entry hygiene, locking, coverage gaps (AB-2).

Follow-up to AB-1 / #334. Each test names the review finding it closes and fails
on the hooks as of 070f94f9.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_agent_route_hook import _depth_path_for, _load_hook_module, _run

HOOKS = Path(__file__).parent.parent / "src" / "llm_router" / "hooks"
REPO_HOOKS = Path(__file__).parent.parent / "hooks"
ROUTE = HOOKS / "agent-route.py"
RELEASE = HOOKS / "agent-depth-release.py"
START = HOOKS / "subagent-start.py"
SESSION_END = HOOKS / "session-end.py"
RETRIEVAL = "list all files in src/"  # approved without routing: a real spawn


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {**os.environ, "HOME": str(tmp_path), "LLM_ROUTER_HOME": str(tmp_path / ".llm-router"),
           "LLM_ROUTER_SUBAGENT_DIRECT": "off", "LLM_ROUTER_AGENT_ROUTE_CODEX": "off",
           "LLM_ROUTER_SUBAGENT_MODEL_PIN": "off", "CLAUDE_CODE_SESSION_ID": "sess",
           # CI runners have 2 vCPUs: give the lock a long wait so a stalled holder cannot
           # turn a contention test into a fail-open one (production default is 0.25 s).
           "LLM_ROUTER_BREAKER_LOCK_WAIT_S": "10", **extra}
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
    return env


def _state(tmp_path: Path, sid: str = "sess") -> dict:
    return json.loads(_depth_path_for(tmp_path, sid).read_text())


def _seed(tmp_path: Path, sid: str = "sess", **state) -> None:
    (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
    _depth_path_for(tmp_path, sid).write_text(json.dumps({"depth": 0, "session_id": sid, "ts": 0, **state}))


def _spawn(script: Path, tmp_path: Path, payload: dict, **extra: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=_env(tmp_path, **extra))


def _run_parallel(script: Path, tmp_path: Path, payloads: list[dict]) -> list[subprocess.Popen]:
    procs = [_spawn(script, tmp_path, p) for p in payloads]
    for p, payload in zip(procs, payloads):
        p.stdin.write(json.dumps(payload))
        p.stdin.close()
    for p in procs:
        p.wait(timeout=60)
    return procs


def _pre(i: int) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": f"tu{i}",
            "tool_input": {"prompt": f"{RETRIEVAL} ({i})", "subagent_type": "general-purpose"}}


def _start_claim(tmp_path: Path, agent_id: str) -> None:
    subprocess.run([sys.executable, str(START)], env=_env(tmp_path), text=True, capture_output=True,
                   input=json.dumps({"hook_event_name": "SubagentStart", "agent_id": agent_id,
                                     "agent_type": "general-purpose"}))


# ── 1. codex-delegation branch leaked its pending entry ──────────────────────

def test_codex_delegation_drops_its_pending_entry(tmp_path, monkeypatch, capsys):
    mod = _load_hook_module()
    _seed(tmp_path, agents={"a1": 1})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess")
    for var in ("LLM_ROUTER_SUBAGENT_CLI_DELEGATION", "LLM_ROUTER_SUBAGENT_DIRECT",
                "LLM_ROUTER_QA_ROUTING", "LLM_ROUTER_ALLOW_SUBAGENTS", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
    monkeypatch.setattr(mod, "_log_cli_savings", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_try_codex_subagent_delegation", lambda *a, **k: "codex answer")
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "agent_id": "a1",
               "tool_input": {"prompt": "implement a fix for the off-by-one bug",
                              "subagent_type": "general-purpose"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    mod.main()
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
    st = _state(tmp_path)
    assert st["depth"] == 0 and st["pending"] == []  # nothing spawned: nothing queued
    _start_claim(tmp_path, "fresh")  # an unrelated depth-1 agent must not inherit depth 2
    assert "fresh" not in _state(tmp_path).get("agents", {})


# ── 2. drop by token, not "newest" ───────────────────────────────────────────

def test_drop_pending_removes_this_spawns_entry_not_a_siblings(tmp_path, monkeypatch):
    mod = _load_hook_module()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    (tmp_path / ".llm-router").mkdir()
    mod._push_pending("s", 1, "mine")
    mod._push_pending("s", 1, "sibling")  # pushed after mine, i.e. the newest
    mod._drop_pending("s", "mine")
    toks = [p[2] for p in mod._read_state("s")["pending"]]
    assert toks == ["sibling"]


def test_pending_token_is_tool_use_id_when_given(tmp_path):
    (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
    p = _spawn(ROUTE, tmp_path, _pre(7))
    p.communicate(json.dumps(_pre(7)), timeout=60)
    assert [e[2] for e in _state(tmp_path)["pending"]][-1] == "tu7"


# ── 3. concurrency: locked, atomic read-modify-write ─────────────────────────

@pytest.mark.parametrize("run", range(20))
def test_12_parallel_pretooluse_hooks_lose_no_update(tmp_path, run):
    _run_parallel(ROUTE, tmp_path, [_pre(i) for i in range(12)])
    st = _state(tmp_path)
    assert st["depth"] == 12 and len(st["pending"]) == 12
    assert {e[2] for e in st["pending"]} == {f"tu{i}" for i in range(12)}
    mode = os.stat(_depth_path_for(tmp_path, "sess")).st_mode & 0o777
    assert mode == 0o600


@pytest.mark.parametrize("run", range(20))
def test_parallel_release_and_claim_lose_no_update(tmp_path, run):
    now = time.time()
    _seed(tmp_path, depth=4, pending=[[now, 2, f"t{i}"] for i in range(4)])
    rel = {"hook_event_name": "PostToolUse", "tool_name": "Agent"}
    starts = [{"hook_event_name": "SubagentStart", "agent_id": f"c{i}", "agent_type": "Explore"}
              for i in range(4)]
    procs = [_spawn(RELEASE, tmp_path, rel) for _ in range(4)]
    procs += [_spawn(START, tmp_path, s) for s in starts]
    for p, payload in zip(procs, [rel] * 4 + starts):
        p.stdin.write(json.dumps(payload))
        p.stdin.close()
    for p in procs:
        p.wait(timeout=60)
    errs = [e for e in (p.stderr.read() for p in procs) if e]
    st = _state(tmp_path)
    assert st["depth"] == 0 and st["pending"] == [] and len(st["agents"]) == 4, errs


def test_lock_unavailable_fails_open_and_logs(tmp_path):
    _seed(tmp_path)
    lock = open(f"{_depth_path_for(tmp_path, 'sess')}.lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)  # someone else holds the lock for the whole run
    try:
        t0 = time.monotonic()
        p = _spawn(ROUTE, tmp_path, _pre(1), LLM_ROUTER_BREAKER_LOCK_WAIT_S="0.25")
        out, err = p.communicate(json.dumps(_pre(1)), timeout=60)
        assert p.returncode == 0 and "decision" not in out  # spawn approved, not stalled/blocked
        assert "lock unavailable" in err
        assert time.monotonic() - t0 < 10
    finally:
        lock.close()


# ── 4. coverage gaps ─────────────────────────────────────────────────────────

def test_claim_is_fifo_oldest_first(tmp_path):
    now = time.time()
    _seed(tmp_path, pending=[[now - 2, 2], [now - 1, 3]])
    _start_claim(tmp_path, "first")
    _start_claim(tmp_path, "second")
    ag = _state(tmp_path)["agents"]
    assert ag["first"] == 2 and ag["second"] == 3


def test_expired_pending_entry_is_not_claimed(tmp_path):
    _seed(tmp_path, pending=[[time.time() - 500, 3]])
    _start_claim(tmp_path, "late")
    assert "late" not in _state(tmp_path).get("agents", {})


def test_push_discards_expired_entries(tmp_path, monkeypatch):
    mod = _load_hook_module()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    _seed(tmp_path, "s", pending=[[time.time() - 500, 3, "stale"]])
    mod._push_pending("s", 1, "fresh")
    assert [p[2] for p in mod._read_state("s")["pending"]] == ["fresh"]


def test_release_keeps_the_registry(tmp_path):
    now = time.time()
    _seed(tmp_path, depth=2, agents={"a1": 1, "a2": 2}, pending=[[now, 3]])
    p = _spawn(RELEASE, tmp_path, {})
    p.communicate(json.dumps({"tool_name": "Agent"}), timeout=60)
    st = _state(tmp_path)
    assert st["depth"] == 1 and st["agents"] == {"a1": 1, "a2": 2} and len(st["pending"]) == 1


def test_top_level_is_depth_zero_at_max_depth_one(tmp_path):
    _, out = _run(RETRIEVAL, session_id="sess", max_depth="1", tmp_path=tmp_path)
    assert out is None  # top-level (depth 0) -> child depth 1 == limit
    _, out = _run(RETRIEVAL, session_id="sess", max_depth="1", tmp_path=tmp_path,
                  agent_id="a1", registry={"a1": 1})
    assert out is not None and out["decision"] == "block"  # depth-1 agent may not spawn


def test_nesting_block_drops_its_pending_entry_and_keeps_in_flight(tmp_path):
    _, out = _run("analyze the codebase", session_id="sess", max_depth="3", tmp_path=tmp_path,
                  agent_id="a3", registry={"a3": 3}, agent_depth=2)
    assert out["decision"] == "block"
    st = _state(tmp_path)
    assert st["pending"] == [] and st["depth"] == 2


def test_concurrency_block_drops_its_pending_entry(tmp_path):
    _, out = _run(RETRIEVAL, session_id="sess", agent_depth=16, tmp_path=tmp_path,
                  extra_env={"LLM_ROUTER_MAX_CONCURRENT_AGENTS": "16"})
    assert out["decision"] == "block"
    st = _state(tmp_path)
    assert st["pending"] == [] and st["depth"] == 16


def test_reasoning_block_gives_back_slot_and_pending(tmp_path):
    # the final "route this to a cheap model" block spawns nothing and no PostToolUse follows
    _, out = _run("analyze the architecture tradeoffs in depth", session_id="sess", tmp_path=tmp_path,
                  extra_env={"LLM_ROUTER_ALLOW_SUBAGENTS": "off"})
    assert out is not None and out["decision"] == "block"
    st = _state(tmp_path)
    assert st["depth"] == 0 and st["pending"] == []


# ── 6. cleanup ───────────────────────────────────────────────────────────────

def test_session_end_removes_breaker_state_and_lock(tmp_path):
    _seed(tmp_path, "gone", depth=1)
    lock = Path(f"{_depth_path_for(tmp_path, 'gone')}.lock")
    lock.write_text("")
    _seed(tmp_path, "other", depth=1)
    r = subprocess.run([sys.executable, str(SESSION_END)], env=_env(tmp_path, CLAUDE_CODE_SESSION_ID="gone"), text=True,
                       capture_output=True, input=json.dumps(
                           {"hook_event_name": "SessionEnd", "session_id": "gone"}))
    assert r.returncode == 0, r.stderr
    assert not _depth_path_for(tmp_path, "gone").exists() and not lock.exists()
    assert _depth_path_for(tmp_path, "other").exists()  # another session is untouched


def test_registry_growth_caps_hold(tmp_path, monkeypatch):
    mod = _load_hook_module()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    (tmp_path / ".llm-router").mkdir()
    for i in range(250):
        mod._push_pending("s", 1, f"t{i}")
    assert len(mod._read_state("s")["pending"]) == 200


# ── hook copies ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["agent-route.py", "agent-depth-release.py", "subagent-start.py",
                                  "session-end.py"])
def test_hook_copies_are_byte_identical(name):
    assert (REPO_HOOKS / name).read_bytes() == (HOOKS / name).read_bytes()


# ── 5. doctor flags a missing SubagentStart registration ─────────────────────

def _settings(tmp_path: Path, hooks: dict) -> Path:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": hooks}))
    return p


def _cmd(script: str) -> list:
    return [{"hooks": [{"type": "command", "command": f"/usr/bin/python3 /h/llm_router-{script}.py"}]}]


def test_doctor_warns_when_subagent_start_is_missing(tmp_path):
    from llm_router.commands.doctor import _subagent_start_gap
    msg = _subagent_start_gap(_settings(tmp_path, {"PreToolUse": _cmd("agent-route")}))
    assert msg and "depth 1" in msg and "LLM_ROUTER_MAX_CONCURRENT_AGENTS" in msg


def test_doctor_quiet_when_subagent_start_registered_or_no_breaker(tmp_path):
    from llm_router.commands.doctor import _subagent_start_gap
    both = {"PreToolUse": _cmd("agent-route"), "SubagentStart": _cmd("subagent-start")}
    assert _subagent_start_gap(_settings(tmp_path, both)) is None
    assert _subagent_start_gap(_settings(tmp_path, {})) is None
    assert _subagent_start_gap(tmp_path / "missing.json") is None
