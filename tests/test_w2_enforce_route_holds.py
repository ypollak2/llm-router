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
    secret = "UNIQUE-CANARY-PROMPT-TEXT-77"
    _seed(tmp_path, original_prompt=OWNER + " " + secret)
    _hook(tmp_path, "mcp__llm_router__llm", {"prompt": "Reply OK " + secret, "task": "query"})
    _hook(tmp_path, "Bash", {"command": "echo " + secret})
    blob = (tmp_path / ".llm-router" / "hold_events.jsonl").read_text() + _log(tmp_path)
    assert secret not in blob


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
