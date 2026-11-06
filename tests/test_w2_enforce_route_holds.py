"""BIND W2 on the real hook: a trivial llm_* call no longer releases a hold, a real route does.

Every test runs ``enforce-route.py`` as a subprocess against a temporary
``LLM_ROUTER_HOME``, the way Claude Code runs it, and reads what a host reads: stdout (one
JSON object or nothing), the hold file, ``enforcement.log`` and the text-free hold ledger.
No model is called. Prompts are synthetic.

Evidence behind the change: BIND-R2 (the coordinator's read of the 158 routed_mcp units of
the audit window) found 76 of 141 unused MCP units to be "Reply OK" probes sent to clear
the lock, 0 of 133 router calls forwarding the owner's prompt, and 51 holds in 9 sessions.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENFORCE = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"

OWNER = (
    "Explain the difference between a mutex and a semaphore, with a short example in "
    "Python, and say when each one is the right tool please"
)
OPERATIONAL_OWNER = (
    "Run the database migration against the staging cluster, then verify the row counts "
    "match and report any table that differs"
)
PARAPHRASE = (
    "Explain mutex vs semaphore: the difference, when to use each one, with a short "
    "python example"
)
SESSION = "w2sess"
TUID = "toolu_01W2TESTTOOLUSEID0000"


def _env(home: Path, **extra) -> dict:
    e = {k: v for k, v in os.environ.items()
         if not k.startswith("LLM_ROUTER_") and k not in ("HOME",)}
    e.update(HOME=str(home), LLM_ROUTER_HOME=str(home / ".llm-router"),
             LLM_ROUTER_ENFORCE="smart", LLM_ROUTER_BASH_INTERCEPT="off")
    e.update(extra)
    return e


def _seed(home: Path, sid: str = SESSION, **over) -> Path:
    d = home / ".llm-router"
    d.mkdir(parents=True, exist_ok=True)
    data = {
        "expected_tool": "llm_query", "task_type": "query", "complexity": "simple",
        "method": "heuristic", "issued_at": time.time(), "turn_id": 1, "session_id": sid,
        "original_prompt": OWNER,
    }
    data.update(over)
    path = d / f"pending_route_{sid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _hook(home: Path, tool: str, tool_input=None, sid: str = SESSION, **env_extra):
    payload = {"session_id": sid, "tool_name": tool, "tool_input": tool_input or {},
               "tool_use_id": TUID}
    return subprocess.run(
        [sys.executable, str(ENFORCE)], input=json.dumps(payload),
        capture_output=True, text=True, env=_env(home, **env_extra), timeout=60,
    )


def _decision(proc) -> dict | None:
    """What the host would parse: exactly one JSON object, or nothing at all."""
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)  # raises on a stray line before or after the object


def _held(proc) -> bool:
    d = _decision(proc)
    return bool(d) and d.get("decision") == "block"


def _ledger(home: Path) -> list[dict]:
    path = home / ".llm-router" / "hold_events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _events(home: Path, event: str) -> list[dict]:
    return [r for r in _ledger(home) if r["event"] == event]


def _log(home: Path) -> str:
    p = home / ".llm-router" / "enforcement.log"
    return p.read_text() if p.exists() else ""


BASH = {"command": "git push origin main"}  # a write: held for a Q&A task in smart mode


def test_premise_the_hold_blocks_bash(tmp_path):
    """The control for everything below: with the hold in place Bash is refused."""
    _seed(tmp_path)
    assert _held(_hook(tmp_path, "Bash", BASH))
    (row,) = _events(tmp_path, "blocked")
    assert row["tool"] == "Bash" and row["task"] == "query" and row["violation"] == 1
    assert row["tool_use_id"] == TUID, "a ledger row must join to the transcript's tool_use block"


# ── a toll probe no longer releases ──────────────────────────────────────────

@pytest.mark.parametrize("probe", [
    {"prompt": "Reply OK", "task": "query", "tier": "fast"},
    {"prompt": "Reply with the single word: ready", "task": "auto"},
    {"prompt": "What is the capital of France?", "task": "query"},
    {"prompt": "", "task": "query"},
])
@pytest.mark.parametrize("tool", ["mcp__llm_router__llm", "mcp__llm_router__llm_route"])
def test_a_probe_through_a_routing_door_runs_but_does_not_release(tmp_path, tool, probe):
    pending = _seed(tmp_path)
    r = _hook(tmp_path, tool, probe)
    assert _decision(r) is None, "the probe call itself must run (it is not a work tool)"
    assert pending.exists(), "a probe must not release the hold"
    assert _held(_hook(tmp_path, "Bash", BASH)), "the hold must still refuse Bash after the probe"
    rows = _events(tmp_path, "probe")
    assert len(rows) == 1 and rows[0]["kind"] == "door_unrelated"
    assert rows[0]["call_words"] == len(probe["prompt"].split())
    assert rows[0]["tool_use_id"] == TUID
    assert _events(tmp_path, "released") == []
    assert "PROBE" in _log(tmp_path) and "HOLD_KEPT" in _log(tmp_path)


@pytest.mark.parametrize("tool", [
    "mcp__llm_router__llm_router_status", "mcp__llm_router__llm_router_admin",
    "mcp__llm_router__llm_router_session", "llm_savings",
])
def test_a_status_or_admin_call_does_not_release(tmp_path, tool):
    pending = _seed(tmp_path)
    assert _decision(_hook(tmp_path, tool, {"view": "summary"})) is None
    assert pending.exists()
    rows = _events(tmp_path, "probe")
    assert len(rows) == 1 and rows[0]["kind"] == "non_door"


def test_a_probe_is_recorded_in_hard_mode_too(tmp_path):
    pending = _seed(tmp_path)
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK"},
                           LLM_ROUTER_ENFORCE="hard")) is None
    assert pending.exists()
    assert len(_events(tmp_path, "probe")) == 1


def test_no_probe_is_recorded_where_nothing_is_held(tmp_path):
    """soft / suggest never hold a tool, so a call made under them is no toll."""
    _seed(tmp_path)
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK"},
                           LLM_ROUTER_ENFORCE="soft")) is None
    assert _events(tmp_path, "probe") == []
    assert "PROBE" not in _log(tmp_path)


def test_no_text_reaches_the_ledger_or_the_log(tmp_path):
    secret = "UNIQUECANARYPROMPTTEXT77"     # one word: it must not make the probe look like a quote of the owner
    _seed(tmp_path, original_prompt=OWNER + " " + secret)
    _hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK " + secret, "task": "query"})
    _hook(tmp_path, "Bash", {"command": "echo " + secret + " > out.txt"})  # generative: D-396 exempts read-only Bash
    blob = (tmp_path / ".llm-router" / "hold_events.jsonl").read_text() + _log(tmp_path)
    assert secret not in blob
    # the check had something to check: both the call and the refusal were recorded
    assert len(_events(tmp_path, "door_call")) == 1 and len(_events(tmp_path, "blocked")) == 1


# ── a short owner prompt is no easier to release than a long one ─────────────

TINY = "push to main"      # 3 words, 2 of them content words
THIN = "yes please do that again now and keep going with the same thing as before"  # 15 words, 1 content


@pytest.mark.parametrize("mode", ["hard", "strict"])
def test_a_reply_ok_probe_does_not_release_a_tiny_prompts_hold(tmp_path, mode):
    """docs/proxy.md: a probe never releases a hold. A prompt of fewer than three content
    words used to be 'unverifiable', so any door call released it and the toll was back for
    the shortest prompts, in exactly the modes that hold them."""
    pending = _seed(tmp_path, original_prompt=TINY)
    env = {"LLM_ROUTER_ENFORCE": mode}
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK"}, **env)) is None
    assert pending.exists(), f"a probe released the {mode} hold on a three-word prompt"
    assert _held(_hook(tmp_path, "Bash", BASH, **env))
    assert [r["kind"] for r in _events(tmp_path, "probe")] == ["door_unrelated"]
    assert _events(tmp_path, "released") == []


@pytest.mark.parametrize("mode", ["hard", "strict", "smart"])
@pytest.mark.parametrize("call", [
    {"prompt": "push to main"},
    {"prompt": "Push to main!", "task": "query"},
    {"prompt": "Please push the finished changes to main and report back", "task": "query"},
])
def test_restating_a_tiny_prompt_releases_its_hold(tmp_path, mode, call):
    pending = _seed(tmp_path, original_prompt=TINY)
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm", call, LLM_ROUTER_ENFORCE=mode)) is None
    assert not pending.exists()
    assert [r["how"] for r in _events(tmp_path, "released")] == ["route"]


def test_a_long_prompt_with_one_content_word_is_judged_the_same_way(tmp_path):
    """15 words, so smart holds it; one content word, so the overlap test cannot judge it. A
    probe still shares none of it, and the prompt's own words still release."""
    pending = _seed(tmp_path, original_prompt=THIN)
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK"})) is None
    assert pending.exists() and len(_events(tmp_path, "probe")) == 1
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm", {"prompt": THIN})) is None
    assert not pending.exists()


# ── strict has no free valve ─────────────────────────────────────────────────

STRICT = {"LLM_ROUTER_ENFORCE": "strict"}


def test_strict_has_no_free_valve_and_a_probe_leaves_the_hold(tmp_path):
    """SECURITY.md, enforcement modes: strict turns the trap, loop and count releases off.
    Retrying does not release, a probe runs and leaves the hold, and the exits are routing the
    request (the user's `claude:` prefix and the next prompt are in the prompt-hook tests)."""
    pending = _seed(tmp_path)
    for n in range(1, 7):          # the 2nd (trap), 3rd (loop) and 4th (count) block would each have released
        assert _held(_hook(tmp_path, "Bash", BASH, **STRICT)), f"strict released at block {n}"
    assert pending.exists() and _events(tmp_path, "released") == []
    assert "AUTO-PIVOT" not in _log(tmp_path) and "BLOCKED(strict)" in _log(tmp_path)
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK"}, **STRICT)) is None
    assert pending.exists(), "a probe must not release a strict hold"
    assert _held(_hook(tmp_path, "Bash", BASH, **STRICT))
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm", {"prompt": OWNER}, **STRICT)) is None
    assert not pending.exists()
    assert not _held(_hook(tmp_path, "Bash", BASH, **STRICT))
    assert [r["how"] for r in _events(tmp_path, "released")] == ["route"]


def test_the_strict_block_text_does_not_promise_a_release_that_never_comes(tmp_path):
    _seed(tmp_path)
    reason = _decision(_hook(tmp_path, "Bash", BASH, **STRICT))["reason"]
    assert "Strict mode has no free valve" in reason and "60 minutes" in reason
    for promised in ("Trap detection", "Loop detection", "hit violation 4", "Auto-pivot at violation 4",
                     "retry the same tool"):
        assert promised not in reason, f"strict text promises: {promised}"
    assert "`claude:`" in reason and "does NOT release" in reason


def test_strict_still_lets_the_routers_own_cli_run(tmp_path):
    """The one exit that needs no routing (F-1): SECURITY.md and docs/proxy.md say so."""
    _seed(tmp_path)
    assert _decision(_hook(tmp_path, "Bash", {"command": "llm-router set-enforce off"}, **STRICT)) is None
    assert "ROUTER_CLI_EXEMPT" in _log(tmp_path)


def test_control_the_other_modes_still_list_their_free_valves(tmp_path):
    _seed(tmp_path)
    reason = _decision(_hook(tmp_path, "Bash", BASH))["reason"]
    assert "Trap detection" in reason and "Loop detection" in reason and "Strict mode" not in reason


# ── soft and suggest keep the release they always had ────────────────────────

def test_control_a_pending_file_under_soft_logs_a_violation_for_a_work_tool(tmp_path):
    _seed(tmp_path)
    assert _decision(_hook(tmp_path, "Bash", BASH, LLM_ROUTER_ENFORCE="soft")) is None
    assert "VIOLATION" in _log(tmp_path) and "ALLOWED(soft)" in _log(tmp_path)


@pytest.mark.parametrize("mode", ["soft", "suggest"])
@pytest.mark.parametrize("tool", ["mcp__llm_router__llm_route", "mcp__llm_router__llm_router_status"])
def test_soft_and_suggest_still_clear_the_pending_file_on_any_llm_tool(tmp_path, mode, tool):
    """Nothing is held under soft or suggest, so there is no toll to prevent. A probe that
    left the pending file behind would log one more VIOLATION on the next tool call; the
    release those modes always had keeps their telemetry what it was."""
    pending = _seed(tmp_path)
    env = {"LLM_ROUTER_ENFORCE": mode}
    assert _decision(_hook(tmp_path, tool, {"prompt": "Reply OK"}, **env)) is None
    assert not pending.exists()
    assert _events(tmp_path, "probe") == [] and "PROBE" not in _log(tmp_path)
    assert _decision(_hook(tmp_path, "Bash", BASH, **env)) is None
    assert "VIOLATION" not in _log(tmp_path)


# ── every routing-door call is counted, held or not ──────────────────────────

def test_a_door_call_outside_any_hold_is_counted_so_the_probe_bar_is_measurable(tmp_path):
    """A ROUTE hint can provoke a "Reply OK" with no hold in force. probe rows exist only
    under a hold, so the door_call row is where 'no Reply OK probes' is measured."""
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK", "task": "query"})) is None
    (row,) = _events(tmp_path, "door_call")
    assert row["tool"] == "llm" and row["held"] is False and row["forwarded"] == "unknown"
    assert row["ack"] is True and row["call_words"] == 2
    assert row["session_id"] == SESSION and row["tool_use_id"] == TUID
    assert _events(tmp_path, "probe") == [] and _events(tmp_path, "released") == []


def test_a_door_call_under_a_hold_records_whether_it_carried_the_request(tmp_path):
    _seed(tmp_path)
    _hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK"})
    (first,) = _events(tmp_path, "door_call")
    assert first["held"] is True and first["forwarded"] == "unrelated" and first["ack"] is True
    assert len(_events(tmp_path, "probe")) == 1            # the same call, seen as a probe
    _hook(tmp_path, "mcp__llm_router__llm", {"prompt": OWNER})
    rows = _events(tmp_path, "door_call")
    assert [r["forwarded"] for r in rows] == ["unrelated", "route"]
    assert rows[1]["ack"] is False and rows[1]["call_words"] == len(OWNER.split())
    assert [r["how"] for r in _events(tmp_path, "released")] == ["route"]


@pytest.mark.parametrize("text,ack", [
    ("Reply OK", True),
    ("Reply with the single word: ready", True),
    ("Respond with exactly: pong", True),
    ("Reply to the customer about the invoice", False),
    ("What is the capital of France?", False),
])
def test_the_ack_verdict_is_on_the_row_and_the_text_is_not(tmp_path, text, ack):
    _hook(tmp_path, "mcp__llm_router__llm", {"prompt": text})
    (row,) = _events(tmp_path, "door_call")
    assert row["ack"] is ack and text not in json.dumps(row)


@pytest.mark.parametrize("tool", ["Bash", "Read", "mcp__llm_router__llm_router_status", "mcp__other__thing"])
def test_only_routing_doors_get_a_door_call_row(tmp_path, tool):
    _seed(tmp_path)
    _hook(tmp_path, tool, {"command": "ls"} if tool == "Bash" else {"view": "x"})
    assert _events(tmp_path, "door_call") == []


def test_a_crash_while_writing_the_door_call_row_never_changes_the_decision(tmp_path, monkeypatch):
    """The row is a metric. If building it raises, the hook still decides exactly as before
    (here: the owner's request releases the hold, nothing on stdout) and the miss is counted."""
    wrapper = (
        "import runpy, sys\n"
        "import llm_router.hold_policy as hp\n"
        "def boom(*a, **k):\n"
        "    raise RuntimeError('boom')\n"
        "hp.is_ack_probe = boom\n"                      # only the door_call row uses it
        "runpy.run_path(sys.argv[1], run_name='__main__')\n"
    )
    pending = _seed(tmp_path)
    r = subprocess.run(
        [sys.executable, "-c", wrapper, str(ENFORCE)],
        input=json.dumps({"session_id": SESSION, "tool_name": "mcp__llm_router__llm",
                          "tool_input": {"prompt": OWNER}, "tool_use_id": TUID}),
        capture_output=True, text=True, env=_env(tmp_path), timeout=60,
    )
    assert r.returncode == 0 and r.stdout == "", r.stderr[-400:]
    assert not pending.exists() and [x["how"] for x in _events(tmp_path, "released")] == ["route"]
    assert _events(tmp_path, "door_call") == []
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    from llm_router import failopen

    failopen.reset_cache()           # snapshot() caches per process, not per store path
    assert failopen.snapshot().by_code.get("CHZ-FO-HOOK-DOOR-CALL", 0) >= 1


def test_door_calls_are_counted_in_soft_mode_too(tmp_path):
    _seed(tmp_path)
    _hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK"}, LLM_ROUTER_ENFORCE="soft")
    (row,) = _events(tmp_path, "door_call")
    assert row["ack"] is True and row["held"] is True


# ── a real route releases ────────────────────────────────────────────────────

@pytest.mark.parametrize("call", [
    {"prompt": OWNER, "task": "query"},
    {"prompt": PARAPHRASE, "task": "query", "tier": "fast"},
    {"prompt": PARAPHRASE, "task": "query", "context": "unrelated file text " * 20},
])
def test_forwarding_the_owners_request_releases_the_hold(tmp_path, call):
    pending = _seed(tmp_path)
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm", call)) is None
    assert not pending.exists()
    assert not _held(_hook(tmp_path, "Bash", BASH)), "after a real route Bash runs"
    rows = _events(tmp_path, "released")
    assert [r["how"] for r in rows] == ["route"]
    assert rows[0]["tool_use_id"] == TUID and rows[0]["tool"] == "llm"
    assert _events(tmp_path, "probe") == [] and _events(tmp_path, "blocked") == []


def test_an_operational_hold_is_released_by_llm_act_carrying_the_task(tmp_path):
    pending = _seed(tmp_path, expected_tool="llm_delegate", task_type="delegate",
                    complexity="moderate", original_prompt=OPERATIONAL_OWNER)
    env = {"LLM_ROUTER_DELEGATE": "on"}
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm_router_status", {}, **env)) is None
    assert pending.exists()
    task = "Run the database migration on staging, verify row counts match, report differing tables"
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm_act", {"task": task}, **env)) is None
    assert not pending.exists()


def test_a_hold_with_nothing_to_compare_keeps_the_old_release(tmp_path):
    """Pending files with no original_prompt (older hooks, other writers) release on any
    routing door, as before: nothing can be judged, so nothing is held forever."""
    pending = _seed(tmp_path, original_prompt="")
    assert _decision(_hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK"})) is None
    assert not pending.exists()
    assert [r["how"] for r in _events(tmp_path, "released")] == ["unverifiable"]


# ── the free valves and the user's bypass still work, without a model call ───

def test_the_second_block_of_one_tool_in_one_turn_pivots_for_free(tmp_path):
    pending = _seed(tmp_path)
    assert _held(_hook(tmp_path, "Bash", BASH))
    assert pending.exists()
    assert _decision(_hook(tmp_path, "Bash", BASH)) is None  # trap detection: released
    assert not pending.exists()
    assert [r["how"] for r in _events(tmp_path, "released")] == ["trap"]
    assert "AUTO-PIVOT (trap)" in _log(tmp_path)


def test_a_probe_then_a_retry_is_two_calls_not_a_release_by_the_probe(tmp_path):
    """The toll: before this change the probe released the hold and the retry ran free."""
    _seed(tmp_path)
    assert _held(_hook(tmp_path, "Bash", BASH))                                  # block 1
    _hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK", "task": "query"})
    assert _decision(_hook(tmp_path, "Bash", BASH)) is None                      # trap: block 2 pivots
    assert [r["how"] for r in _events(tmp_path, "released")] == ["trap"]
    assert len(_events(tmp_path, "probe")) == 1


def test_the_block_text_names_the_user_bypass_and_not_the_toll(tmp_path):
    _seed(tmp_path)
    reason = _decision(_hook(tmp_path, "Bash", BASH))["reason"]
    for gone in ("Call ANY", "even a trivial", "Escape valves"):
        assert gone not in reason
    assert "Routing directive BLOCKED" in reason
    assert "`claude:`" in reason and "does NOT release" in reason


# ── stdout is exactly one JSON object on every emit path ─────────────────────

STRAY_WRAPPER = r"""
import runpy, sys
import llm_router.hold_ledger as hold_ledger
import llm_router.hooks.tool_intercept as tool_intercept

_orig = hold_ledger.record
def noisy(*a, **k):
    print("STRAY LOG LINE FROM A LIBRARY")          # to sys.stdout, as a careless logger would
    return _orig(*a, **k)
hold_ledger.record = noisy

_orig_log = tool_intercept._log_intercept
def noisy_log(*a, **k):
    print("STRAY LOG LINE FROM THE INTERCEPT")
    return _orig_log(*a, **k)
tool_intercept._log_intercept = noisy_log
runpy.run_path(sys.argv[1], run_name="__main__")
"""


def _noisy_run(home: Path, tool: str, tool_input: dict, **env_extra):
    payload = {"session_id": SESSION, "tool_name": tool, "tool_input": tool_input, "cwd": str(home)}
    return subprocess.run(
        [sys.executable, "-c", STRAY_WRAPPER, str(ENFORCE)], input=json.dumps(payload),
        capture_output=True, text=True, env=_env(home, **env_extra), timeout=60,
    )


def test_the_block_decision_is_exactly_one_json_object_even_when_a_library_prints(tmp_path):
    _seed(tmp_path)
    r = _noisy_run(tmp_path, "Bash", BASH)
    decision = json.loads(r.stdout)   # one object: a stray line anywhere in stdout raises here
    assert decision["decision"] == "block"
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "STRAY LOG LINE" in r.stderr and "STRAY LOG LINE" not in r.stdout


def test_the_local_interception_deny_is_exactly_one_json_object(tmp_path):
    """The other emit path of this hook: a read-only command run locally, result denied-with-text."""
    big = tmp_path / "big.txt"
    big.write_text("".join(f"line {i % 3} of the repeated log output with padding text\n" for i in range(400)))
    r = _noisy_run(tmp_path, "Bash", {"command": f"cat {big}"}, LLM_ROUTER_BASH_INTERCEPT="1")
    decision = json.loads(r.stdout)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "run locally by the router" in decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert "STRAY LOG LINE FROM THE INTERCEPT" in r.stderr and "STRAY" not in r.stdout


def test_a_probe_and_a_release_emit_nothing_on_stdout(tmp_path):
    """No decision is the host's 'proceed'; a stray line would be parsed as a broken one."""
    _seed(tmp_path)
    probe = _noisy_run(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK"})
    assert probe.returncode == 0 and probe.stdout == ""
    release = _noisy_run(tmp_path, "mcp__llm_router__llm", {"prompt": OWNER})
    assert release.returncode == 0 and release.stdout == ""


@pytest.mark.parametrize("both", [False, True], ids=["latency-store", "latency-and-failopen-store"])
@pytest.mark.parametrize("case", ["block", "probe", "release", "interception"])
def test_a_failing_exit_time_write_puts_no_line_behind_the_decision(tmp_path, case, both):
    """The hook_latency row (KPI G1) is written by an atexit handler, after main() has returned.
    When that write fails the library logs through structlog to sys.stdout, and a line behind
    (or instead of) the JSON voids the decision. The script entry leaves sys.stdout on stderr
    until the process is gone. Run as the host runs it: the real script, no wrapper."""
    _seed(tmp_path)
    root = tmp_path / ".llm-router"
    (root / "hook_latency.jsonl").mkdir()            # a directory where the file goes: the write cannot open it
    if both:
        (root / "fail_open.jsonl").mkdir()           # nor can the fail-open store, so the library warns instead
    big = tmp_path / "big.txt"
    big.write_text("".join(f"line {i % 3} of the repeated log output with padding text\n" for i in range(400)))
    tool, tool_input, extra = {
        "block": ("Bash", BASH, {}),
        "probe": ("mcp__llm_router__llm", {"prompt": "Reply OK"}, {}),
        "release": ("mcp__llm_router__llm", {"prompt": OWNER}, {}),
        "interception": ("Bash", {"command": f"cat {big}"}, {"LLM_ROUTER_BASH_INTERCEPT": "1"}),
    }[case]
    r = _hook(tmp_path, tool, tool_input, **extra)
    decision = _decision(r)          # json.loads(stdout): a stray line before or after the object raises
    if case in ("block", "interception"):
        assert decision is not None and decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert decision is None
    # the check had something to check: the exit-time write failed and the library did log, on stderr
    assert "CHZ-FO-HOOK-LATENCY-WRITE" in r.stderr


def test_hooks_newer_than_the_package_still_decide_and_the_miss_is_counted(tmp_path, monkeypatch):
    """Hook files can be newer than the installed llm_router (hold_policy / hold_ledger
    absent). The hold still blocks, a door call still releases (nothing to judge it with),
    and the miss is a counted fail-open, not a silent one."""
    wrapper = (
        "import runpy, sys\n"
        "sys.modules['llm_router.hold_policy'] = None\n"      # `from llm_router import x` -> ImportError
        "sys.modules['llm_router.hold_ledger'] = None\n"
        "runpy.run_path(sys.argv[1], run_name='__main__')\n"
    )

    def run(tool, tool_input):
        return subprocess.run(
            [sys.executable, "-c", wrapper, str(ENFORCE)],
            input=json.dumps({"session_id": SESSION, "tool_name": tool, "tool_input": tool_input}),
            capture_output=True, text=True, env=_env(tmp_path), timeout=60,
        )

    pending = _seed(tmp_path)
    decision = json.loads(run("Bash", BASH).stdout)
    assert decision["decision"] == "block"
    # the block text claims only what is true: with no policy module every door still releases
    assert "How this hold is released:" in decision["reason"]
    assert "does NOT release" not in decision["reason"]
    assert run("mcp__llm_router__llm", {"prompt": "Reply OK"}).stdout == ""
    assert not pending.exists(), "without the policy module the old release applies"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    from llm_router import failopen

    failopen.reset_cache()           # snapshot() caches per process, not per store path
    by_code = failopen.snapshot().by_code
    assert by_code.get("CHZ-FO-HOOK-HOLD-LEDGER", 0) >= 1
    assert by_code.get("CHZ-FO-HOOK-HOLD-POLICY", 0) >= 1


def test_the_decision_goes_to_the_stdout_the_caller_holds_when_run_in_process(tmp_path, monkeypatch, capsys):
    """main() is also called in-process by tests: the guard must hand stdout back."""
    import importlib.util
    import io

    _seed(tmp_path)
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("enforce_route_w2_inproc", ENFORCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": SESSION, "tool_name": "Bash", "tool_input": BASH})))
    mod.main()
    out = capsys.readouterr().out
    assert json.loads(out)["decision"] == "block"
    assert sys.stdout is not sys.stderr
