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
    if "slots" in state:  # the file's depth is derived from its slots
        state.setdefault("depth", len(state["slots"]))
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
    assert st["depth"] == 0 and st.get("pending", []) == []  # nothing spawned: nothing queued
    _start_claim(tmp_path, "fresh")  # an unrelated depth-1 agent must not inherit depth 2
    assert "fresh" not in _state(tmp_path).get("agents", {})


# ── 2. a commit records the spawn's token in both lists ──────────────────────

def test_commit_spawn_records_pending_and_slot_by_token(tmp_path, monkeypatch):
    mod = _load_hook_module()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    (tmp_path / ".llm-router").mkdir()
    mod._commit_spawn("s", "mine", 1, take_slot=True)
    mod._commit_spawn("s", "explore", 1, take_slot=False)  # no slot: nothing will release it
    st = mod._read_state("s")
    assert [p[2] for p in st["pending"]] == ["mine", "explore"]
    assert [e[0] for e in st["slots"]] == ["mine"] and st["depth"] == 1


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
    _seed(tmp_path, depth=4, slots=[[f"s{i}", now] for i in range(4)],
          pending=[[now, 2, f"t{i}"] for i in range(4)])
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
        # the fallback is also logged to hook_errors.log: exactly one line (one lock
        # attempt per PreToolUse run), schema of llm_router.hook_health, no prompt text
        log = (tmp_path / ".llm-router" / "hook_errors.log").read_text()
        rows = [json.loads(line) for line in log.splitlines()]
        assert len(rows) == 1 and rows[0]["hook"] == "agent-route"
        assert "lock unavailable" in rows[0]["error"] and RETRIEVAL not in log
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


def test_commit_discards_expired_entries(tmp_path, monkeypatch):
    mod = _load_hook_module()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    _seed(tmp_path, "s", pending=[[time.time() - 500, 3, "stale"]])
    mod._commit_spawn("s", "fresh", 1, take_slot=False)
    assert [p[2] for p in mod._read_state("s")["pending"]] == ["fresh"]


def test_release_keeps_the_registry(tmp_path):
    now = time.time()
    _seed(tmp_path, depth=2, slots=[["x", now], ["y", now]], agents={"a1": 1, "a2": 2},
          pending=[[now, 3]])
    p = _spawn(RELEASE, tmp_path, {})
    p.communicate(json.dumps({"tool_name": "Agent", "tool_use_id": "x"}), timeout=60)
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
    assert st.get("pending", []) == [] and st["depth"] == 2


def test_concurrency_block_drops_its_pending_entry(tmp_path):
    _, out = _run(RETRIEVAL, session_id="sess", agent_depth=16, tmp_path=tmp_path,
                  extra_env={"LLM_ROUTER_MAX_CONCURRENT_AGENTS": "16"})
    assert out["decision"] == "block"
    st = _state(tmp_path)
    assert st.get("pending", []) == [] and st["depth"] == 16


def test_reasoning_block_gives_back_slot_and_pending(tmp_path):
    # the final "route this to a cheap model" block spawns nothing and no PostToolUse follows
    _, out = _run("analyze the architecture tradeoffs in depth", session_id="sess", tmp_path=tmp_path,
                  extra_env={"LLM_ROUTER_ALLOW_SUBAGENTS": "off"})
    assert out is not None and out["decision"] == "block"
    # a blocked spawn commits nothing: no state file at all on a fresh session
    assert not _depth_path_for(tmp_path, "sess").exists()


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
        mod._commit_spawn("s", f"t{i}", 1, take_slot=True)
    st = mod._read_state("s")
    assert len(st["pending"]) == 200 and len(st["slots"]) == 200


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


# ── review of #341, finding 1: leaked slots expire; release is by token ──────

def _slots(n: int, age: float = 0.0, prefix: str = "s") -> list:
    return [[f"{prefix}{i}", time.time() - age] for i in range(n)]


def _pre_with_cap(tmp_path: Path, cap: int, **extra: str) -> dict | None:
    p = _spawn(ROUTE, tmp_path, _pre(1), LLM_ROUTER_MAX_CONCURRENT_AGENTS=str(cap), **extra)
    out, _ = p.communicate(json.dumps(_pre(1)), timeout=60)
    return json.loads(out) if out.strip() else None


def test_leaked_slot_expires_and_a_live_one_does_not(tmp_path):
    _seed(tmp_path, slots=_slots(16, age=7200), depth=16)  # PostToolUse never fired
    assert _pre_with_cap(tmp_path, 16) is None  # approved: every slot is past the 3600 s TTL
    assert [e[0] for e in _state(tmp_path)["slots"]] == ["tu1"]  # pruned, then this spawn's
    _seed(tmp_path, slots=_slots(16, age=60), depth=16)
    out = _pre_with_cap(tmp_path, 16)
    assert out and out["decision"] == "block" and "16/16" in out["reason"]


def test_cap_counts_only_live_entries(tmp_path):
    _seed(tmp_path, slots=_slots(15, age=0) + _slots(30, age=7200, prefix="old"))
    assert _pre_with_cap(tmp_path, 16) is None  # 15 live of 45 stored: room for one
    _seed(tmp_path, slots=_slots(16, age=0) + _slots(5, age=7200, prefix="old"))
    assert _pre_with_cap(tmp_path, 16)["decision"] == "block"


def test_slot_ttl_is_configurable(tmp_path):
    _seed(tmp_path, slots=_slots(2, age=10))
    assert _pre_with_cap(tmp_path, 2, LLM_ROUTER_AGENT_SLOT_TTL_S="5") is None  # 10 s > 5 s
    _seed(tmp_path, slots=_slots(2, age=10))
    assert _pre_with_cap(tmp_path, 2, LLM_ROUTER_AGENT_SLOT_TTL_S="60")["decision"] == "block"


def test_legacy_bare_depth_count_ages_from_the_files_ts(tmp_path):
    # a state file written before slots existed: the count leaked by the old code ages out
    (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
    _depth_path_for(tmp_path, "sess").write_text(json.dumps(
        {"depth": 16, "session_id": "sess", "ts": time.time() - 7200}))
    assert _pre_with_cap(tmp_path, 16) is None
    _depth_path_for(tmp_path, "sess").write_text(json.dumps(
        {"depth": 16, "session_id": "sess", "ts": time.time()}))
    assert _pre_with_cap(tmp_path, 16)["decision"] == "block"


def _release(tmp_path: Path, **payload) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RELEASE)], env=_env(tmp_path), text=True,
                          capture_output=True, input=json.dumps({"tool_name": "Agent", **payload}))


def test_release_removes_its_own_slot_by_token(tmp_path):
    _seed(tmp_path, slots=_slots(3))
    _release(tmp_path, tool_use_id="s1")
    st = _state(tmp_path)
    assert [e[0] for e in st["slots"]] == ["s0", "s2"] and st["depth"] == 2


def test_release_of_a_call_that_held_no_slot_frees_nothing(tmp_path):
    _seed(tmp_path, slots=_slots(2))  # an Explore/routed-away call completes: it held no slot
    _release(tmp_path, tool_use_id="explore-call")
    assert [e[0] for e in _state(tmp_path)["slots"]] == ["s0", "s1"]


def test_release_without_a_tool_use_id_frees_the_oldest(tmp_path):
    _seed(tmp_path, slots=[["new", time.time()], ["old", time.time() - 100]])
    _release(tmp_path)
    assert [e[0] for e in _state(tmp_path)["slots"]] == ["new"]


def test_pretooluse_then_release_round_trip(tmp_path):
    p = _spawn(ROUTE, tmp_path, _pre(5))
    p.communicate(json.dumps(_pre(5)), timeout=60)
    assert [e[0] for e in _state(tmp_path)["slots"]] == ["tu5"]
    _release(tmp_path, tool_use_id="tu5")
    assert _state(tmp_path)["slots"] == [] and _state(tmp_path)["depth"] == 0


# ── finding 2: the budget blocks spawn nothing and commit nothing ────────────

def _main_to_budget_block(tmp_path, monkeypatch, capsys, estimated: float, remaining: float):
    mod = _load_hook_module()
    for k, v in {"HOME": str(tmp_path), "LLM_ROUTER_HOME": str(tmp_path / ".llm-router"),
                 "CLAUDE_CODE_SESSION_ID": "sess", "LLM_ROUTER_ALLOW_SUBAGENTS": "off",
                 "LLM_ROUTER_SUBAGENT_DIRECT": "off", "LLM_ROUTER_AGENT_ROUTE_CODEX": "off"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(mod, "_try_codex_subagent_delegation", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_try_cli_delegation", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_estimate_agent_cost", lambda *a, **k: estimated)
    monkeypatch.setattr(mod, "_get_remaining_budget", lambda *a, **k: remaining)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": "tu-budget",
               "tool_input": {"prompt": "analyze the architecture tradeoffs in depth",
                              "subagent_type": "general-purpose"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    mod.main()
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("estimated, remaining, text", [
    (5.0, 1.0, "exceed session budget"),        # the remaining-budget block
    (100.0, 1000.0, "per-agent limit"),         # the per-agent maximum block
])
def test_budget_blocks_leave_the_breaker_state_untouched(tmp_path, monkeypatch, capsys,
                                                         estimated, remaining, text):
    now = time.time()
    _seed(tmp_path, slots=[["held", now]], pending=[[now, 1, "held"]])
    before = _state(tmp_path)
    out = _main_to_budget_block(tmp_path, monkeypatch, capsys, estimated, remaining)
    assert out["decision"] == "block" and text in out["reason"]  # the branch under test ran
    after = _state(tmp_path)
    assert after["slots"] == before["slots"] and after["pending"] == before["pending"]
    assert after["depth"] == 1  # no slot or pending entry was taken for the blocked spawn


# ── finding 3: lock files are 0600 whatever the umask ────────────────────────

def _run_umask0(script: Path, tmp_path: Path, payload: dict) -> None:
    subprocess.run([sys.executable, str(script)], env=_env(tmp_path), text=True, capture_output=True,
                   input=json.dumps(payload), preexec_fn=lambda: os.umask(0))


@pytest.mark.parametrize("script, payload, seed", [
    (ROUTE, _pre(1), False),
    (RELEASE, {"tool_name": "Agent", "tool_use_id": "s0"}, True),
    (START, {"hook_event_name": "SubagentStart", "agent_id": "c1", "agent_type": "Explore"}, True),
])
def test_lock_file_is_0600_under_umask_000(tmp_path, script, payload, seed):
    if seed:
        _seed(tmp_path, slots=_slots(1), pending=[[time.time(), 1, "t"]])
    _run_umask0(script, tmp_path, payload)
    lock = Path(f"{_depth_path_for(tmp_path, 'sess')}.lock")
    assert lock.exists()
    assert os.stat(lock).st_mode & 0o777 == 0o600
    assert os.stat(_depth_path_for(tmp_path, "sess")).st_mode & 0o777 == 0o600


# ── finding 4: one PreToolUse run takes the lock at most once ────────────────

def _count_lock_acquisitions(tmp_path, monkeypatch, capsys, prompt: str, subagent_type: str):
    mod = _load_hook_module()
    for k, v in {"HOME": str(tmp_path), "LLM_ROUTER_HOME": str(tmp_path / ".llm-router"),
                 "CLAUDE_CODE_SESSION_ID": "sess", "LLM_ROUTER_SUBAGENT_DIRECT": "off",
                 "LLM_ROUTER_AGENT_ROUTE_CODEX": "off"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
    taken = []
    real = mod._state_lock

    def counting(sid):
        taken.append(sid)
        return real(sid)

    monkeypatch.setattr(mod, "_state_lock", counting)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": "tu",
               "tool_input": {"prompt": prompt, "subagent_type": subagent_type}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    try:
        mod.main()
    except SystemExit:
        pass
    capsys.readouterr()
    return len(taken)


@pytest.mark.parametrize("prompt, subagent_type, expected", [
    (RETRIEVAL, "general-purpose", 1),                    # approved real spawn: one RMW
    ("review the plan", "Explore", 1),                    # pending entry only
    ("analyze the architecture tradeoffs in depth", "general-purpose", 1),  # routed spawn (default)
])
def test_an_approved_pretooluse_run_takes_the_lock_exactly_once(
        tmp_path, monkeypatch, capsys, prompt, subagent_type, expected):
    assert _count_lock_acquisitions(tmp_path, monkeypatch, capsys, prompt, subagent_type) == expected


def test_a_blocked_pretooluse_run_takes_no_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_ALLOW_SUBAGENTS", "off")  # reasoning block, nothing spawns
    assert _count_lock_acquisitions(tmp_path, monkeypatch, capsys,
                                    "analyze the architecture tradeoffs in depth",
                                    "general-purpose") == 0


# ── finding 5: SessionEnd racing a background agent's release ────────────────

def test_release_after_session_end_recreates_neither_state_nor_lock(tmp_path):
    _seed(tmp_path, "gone", slots=_slots(1))
    Path(f"{_depth_path_for(tmp_path, 'gone')}.lock").write_text("")
    r = subprocess.run([sys.executable, str(SESSION_END)], env=_env(tmp_path, CLAUDE_CODE_SESSION_ID="gone"),
                       text=True, capture_output=True,
                       input=json.dumps({"hook_event_name": "SessionEnd", "session_id": "gone"}))
    assert r.returncode == 0, r.stderr
    p = subprocess.run([sys.executable, str(RELEASE)], env=_env(tmp_path, CLAUDE_CODE_SESSION_ID="gone"),
                       text=True, capture_output=True,
                       input=json.dumps({"tool_name": "Agent", "tool_use_id": "s0"}))  # the late release
    assert p.returncode == 0 and p.stderr == ""
    assert sorted(f.name for f in (tmp_path / ".llm-router").glob("agent_depth_gone*")) == []


def test_release_that_loses_the_race_after_the_exists_check_leaves_nothing(tmp_path, monkeypatch):
    # SessionEnd removes the state between release's exists() check and its read.
    spec = __import__("importlib.util").util.spec_from_file_location("release_hook", RELEASE)
    mod = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess")
    _seed(tmp_path, slots=_slots(1))
    state = _depth_path_for(tmp_path, "sess")
    real_lock = mod._lock

    def lock_then_vanish(depth_file):
        fh = real_lock(depth_file)
        state.unlink()  # SessionEnd wins the race
        return fh

    monkeypatch.setattr(mod, "_lock", lock_then_vanish)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Agent", "tool_use_id": "s0"})))
    mod.main()
    assert sorted(f.name for f in (tmp_path / ".llm-router").glob("agent_depth_sess*")) == []


# ── finding 6: fail-open lock fallbacks reach hook_errors.log ────────────────

@pytest.mark.parametrize("script, payload, hook", [
    (RELEASE, {"tool_name": "Agent", "tool_use_id": "s0"}, "agent-depth-release"),
    (START, {"hook_event_name": "SubagentStart", "agent_id": "c1", "agent_type": "Explore"},
     "subagent-start"),
])
def test_release_and_start_log_their_lock_fallback(tmp_path, script, payload, hook):
    _seed(tmp_path, slots=_slots(1), pending=[[time.time(), 1, "t"]])
    lock = open(f"{_depth_path_for(tmp_path, 'sess')}.lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        p = _spawn(script, tmp_path, payload, LLM_ROUTER_BREAKER_LOCK_WAIT_S="0.1")
        p.communicate(json.dumps(payload), timeout=60)
    finally:
        lock.close()
    rows = [json.loads(line) for line in (tmp_path / ".llm-router" / "hook_errors.log").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["hook"] == hook and "lock unavailable" in rows[0]["error"]


# ── review gaps: release TTL prune, start's exists() guard, secret-free fallback log ──

def test_release_prunes_expired_slots_and_keeps_live_ones(tmp_path):
    """Mutant "release TTL never" survived: a no-match release must still drop slots
    older than the TTL (a leaked slot) while a live sibling stays."""
    _seed(tmp_path, slots=[["stale", time.time() - 7200], ["live", time.time()]])
    p = subprocess.run([sys.executable, str(RELEASE)], env=_env(tmp_path), text=True, capture_output=True,
                       input=json.dumps({"tool_name": "Agent", "tool_use_id": "unrelated"}))
    assert p.returncode == 0, p.stderr
    st = _state(tmp_path)
    assert [e[0] for e in st["slots"]] == ["live"] and st["depth"] == 1


def test_subagent_start_after_session_end_creates_no_state_and_no_lock(tmp_path):
    """Mutant dropping start's path.exists() guard left a .lock file behind."""
    assert not _depth_path_for(tmp_path, "sess").exists()
    p = subprocess.run([sys.executable, str(START)], env=_env(tmp_path), text=True, capture_output=True,
                       input=json.dumps({"hook_event_name": "SubagentStart", "agent_id": "late",
                                         "agent_type": "general-purpose"}))
    assert p.returncode == 0, p.stderr
    assert sorted(f.name for f in (tmp_path / ".llm-router").glob("agent_depth_*")) == []


_SECRET = "sk-FAKE-SECRET-do-not-log-9f3a"


@pytest.mark.parametrize("script, payload, hook", [
    (ROUTE, _pre(1), "agent-route"),
    (RELEASE, {"tool_name": "Agent", "tool_use_id": "s0"}, "agent-depth-release"),
    (START, {"hook_event_name": "SubagentStart", "agent_id": "c1", "agent_type": "Explore"},
     "subagent-start"),
])
def test_lock_fallback_logs_the_error_class_never_its_message(tmp_path, script, payload, hook):
    """Mutant logging str(exc) instead of type(exc).__name__ survived: the lock error
    here carries a secret-looking message that must reach neither log nor stderr."""
    _seed(tmp_path, slots=_slots(1), pending=[[time.time(), 1, "t"]])
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import fcntl\n"
        "def _boom(*a, **k):\n"
        f"    raise OSError({_SECRET!r})\n"
        "fcntl.flock = _boom\n")
    p = _spawn(script, tmp_path, payload, LLM_ROUTER_BREAKER_LOCK_WAIT_S="0",
               PYTHONPATH=f"{shim}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    out, err = p.communicate(json.dumps(payload), timeout=60)
    log = (tmp_path / ".llm-router" / "hook_errors.log").read_text()
    assert "lock unavailable (OSError)" in log and "lock unavailable (OSError)" in err
    assert _SECRET not in log and _SECRET not in err and _SECRET not in out
    assert [json.loads(line)["hook"] for line in log.splitlines()] == [hook]
