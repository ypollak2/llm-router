"""BIND W2 on the real prompt hook: a prompt that is not a request writes no hold.

``auto-route.py`` runs as a subprocess against a temporary ``LLM_ROUTER_HOME``, the way
Claude Code runs it. Prompts are synthetic and have the shape of the ones the audit found
holding tools: short continuations and instructions, and the compaction summary Claude
Code injects. Every run's stdout is parsed as the one JSON object the host expects.
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
AUTO = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"

LONG_CODE = (
    "Refactor the authentication module to use dependency injection and add unit tests "
    "for every public function in the module"
)
LONG_GENERATE = (
    "Please write a function that parses ISO 8601 dates, handles time zones correctly, "
    "and include thorough unit tests for leap years"
)
SUMMARY_BODY = (
    "Earlier work implemented the parser module, fixed the failing tests in the cache layer "
    "and refactored the release checklist script. " * 30
)
SUMMARY = (
    "This session is being continued from a previous conversation that ran out of context. "
    + SUMMARY_BODY
)


def _env(home: Path, enforce: str = "smart") -> dict:
    e = {k: v for k, v in os.environ.items()
         if not k.startswith("LLM_ROUTER_") and k != "HOME"}
    e.update(HOME=str(home), LLM_ROUTER_HOME=str(home / ".llm-router"), LLM_ROUTER_ENFORCE=enforce,
             LLM_ROUTER_DISABLE_LLM_CLASSIFIERS="1", LLM_ROUTER_DIRECT_EXECUTION="0",
             OPENAI_API_KEY="", GEMINI_API_KEY="", GOOGLE_API_KEY="")
    return e


def _turn(home: Path, prompt: str, sid: str, enforce: str = "smart"):
    (home / ".llm-router").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(AUTO)], input=json.dumps({"prompt": prompt, "session_id": sid}),
        capture_output=True, text=True, env=_env(home, enforce), timeout=120,
    )


def _context(proc) -> str:
    """The directive the model reads; parsing stdout whole proves it is one JSON object."""
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def _pending(home: Path, sid: str) -> Path:
    return home / ".llm-router" / f"pending_route_{sid}.json"


def _rows(home: Path, event: str | None = None) -> list[dict]:
    p = home / ".llm-router" / "hold_events.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return [r for r in rows if event is None or r["event"] == event]


def test_control_a_real_request_still_writes_a_hold(tmp_path):
    proc = _turn(tmp_path, LONG_GENERATE, "ctl")
    ctx = _context(proc)
    assert _pending(tmp_path, "ctl").exists()
    assert [r["event"] for r in _rows(tmp_path)] == ["prompt", "hold"]
    assert "nothing held" not in ctx


@pytest.mark.parametrize("prompt", [
    "write a function that parses ISO dates",       # a code request, 7 words
    "refactor the auth module",                      # 4 words
    "fix the flaky test in the parser module now",  # 9 words
])
def test_a_short_prompt_of_a_held_class_writes_no_hold(tmp_path, prompt):
    sid = "short"
    ctx = _context(_turn(tmp_path, prompt, sid))
    assert not _pending(tmp_path, sid).exists()
    assert "nothing held" in ctx and "short prompt" in ctx
    assert "ENFORCEMENT ACTIVE" not in ctx and "releases the hold" not in ctx
    assert [r["event"] for r in _rows(tmp_path)] == ["prompt", "exempt"]
    ex = _rows(tmp_path, "exempt")[0]
    assert ex["reason"] == "short_prompt" and ex["words"] == len(prompt.split())


@pytest.mark.parametrize("mode", ["hard", "strict", "suggest"])
def test_hard_and_the_modes_that_never_block_are_unchanged_for_a_short_prompt(tmp_path, mode):
    """The rule is for `smart`: `hard` and `strict` are the explicit opt-in to hold everything,
    and `suggest` has no hold to take away, so each keeps the pending state and the text it had."""
    ctx = _context(_turn(tmp_path, "refactor the auth module", "m", enforce=mode))
    assert _pending(tmp_path, "m").exists()
    assert "nothing held" not in ctx
    assert [r["event"] for r in _rows(tmp_path)] == ["prompt", "hold"]


def test_the_ten_word_boundary_on_the_real_hook(tmp_path):
    ten = "write a function that parses ISO dates and handles zones"
    eleven = ten + " too"
    assert len(ten.split()) == 10 and len(eleven.split()) == 11
    _context(_turn(tmp_path, ten, "b10"))
    _context(_turn(tmp_path, eleven, "b11"))
    assert not _pending(tmp_path, "b10").exists()
    assert _pending(tmp_path, "b11").exists()


def test_a_compaction_summary_writes_no_hold(tmp_path):
    # control: the same text without the marker is a long code-shaped prompt and holds
    _context(_turn(tmp_path, SUMMARY_BODY, "sumctl"))
    assert _pending(tmp_path, "sumctl").exists()
    ctx = _context(_turn(tmp_path, SUMMARY, "sum"))
    assert not _pending(tmp_path, "sum").exists()
    assert "nothing held" in ctx
    assert _rows(tmp_path, "exempt")[0]["reason"] == "session_continuation"


def test_the_hold_text_no_longer_offers_a_trivial_call(tmp_path):
    ctx = _context(_turn(tmp_path, LONG_GENERATE, "txt", enforce="hard"))
    assert _pending(tmp_path, "txt").exists()
    assert "this releases the hold for this turn" not in ctx
    assert "does NOT release" in ctx and "user's request as the prompt" in ctx


def test_a_hook_newer_than_the_package_says_only_what_is_true(tmp_path):
    """Without llm_router.hold_policy every routing door still releases a hold, so the
    directive must not claim that a trivial call does not (and no prompt is exempted)."""
    wrapper = (
        "import runpy, sys\n"
        "sys.modules['llm_router.hold_policy'] = None\n"
        "runpy.run_path(sys.argv[1], run_name='__main__')\n"
    )
    (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-c", wrapper, str(AUTO)],
        input=json.dumps({"prompt": LONG_GENERATE, "session_id": "old"}),
        capture_output=True, text=True, env=_env(tmp_path, "hard"), timeout=120,
    )
    ctx = _context(proc)
    assert _pending(tmp_path, "old").exists()
    assert "releases" in ctx and "does NOT release" not in ctx


@pytest.mark.parametrize("mode", ["smart", "hard", "strict"])
def test_the_claude_prefix_is_the_explicit_user_bypass(tmp_path, mode):
    """docs/proxy.md, Routing prefixes: `claude:` answers natively and holds nothing, and it
    drops a hold left by the previous turn, so the user can always say 'no hold'. In strict,
    which turns every automatic release off, it is the only free exit."""
    sid = "pfx"
    first = _context(_turn(tmp_path, LONG_GENERATE, sid, enforce=mode))
    assert _pending(tmp_path, sid).exists() and first
    proc = _turn(tmp_path, "claude: " + LONG_GENERATE, sid, enforce=mode)
    assert proc.returncode == 0 and proc.stdout.strip() == ""   # answered natively: no directive
    assert not _pending(tmp_path, sid).exists(), "the bypass must drop the previous hold"
    assert [r["event"] for r in _rows(tmp_path)].count("prompt") == 2   # and it still counts as a prompt


@pytest.mark.parametrize("prompt", [LONG_GENERATE, "refactor the auth module", "claude: " + LONG_GENERATE],
                         ids=["hold", "exempt", "claude-prefix"])
def test_a_failing_exit_time_write_puts_no_line_behind_the_prompt_hooks_json(tmp_path, prompt):
    """The hook_latency row is written by an atexit handler. This hook pins its logging to
    stderr, so a failed write there cannot reach stdout; the test keeps it that way on the
    paths the hold change touched (the enforce-route twin is in test_w2_enforce_route_holds)."""
    root = tmp_path / ".llm-router"
    root.mkdir(parents=True)
    (root / "hook_latency.jsonl").mkdir()           # a directory where the file goes
    proc = _turn(tmp_path, prompt, "lat")
    assert proc.returncode == 0, proc.stderr
    if proc.stdout.strip():
        json.loads(proc.stdout)                     # one object, nothing after it
    assert "CHZ-FO-HOOK-LATENCY-WRITE" in proc.stderr, "the write did not fail: nothing was checked"


def test_every_prompt_is_counted_even_when_nothing_else_is_written(tmp_path):
    for i, prompt in enumerate(["ok", "keep going", LONG_GENERATE]):
        _turn(tmp_path, prompt, f"cnt{i}")
    assert len(_rows(tmp_path, "prompt")) == 3
    assert [r["words"] for r in _rows(tmp_path, "prompt")] == [1, 2, len(LONG_GENERATE.split())]


def test_the_ledger_carries_no_prompt_text(tmp_path):
    canary = "CANARY-PROMPT-TEXT-91"
    _turn(tmp_path, f"write a function that parses ISO dates {canary}", "canary")
    _turn(tmp_path, LONG_GENERATE + " " + canary, "canary2")
    assert canary not in (tmp_path / ".llm-router" / "hold_events.jsonl").read_text()


def test_a_forced_local_prefix_keeps_its_hold_even_when_short(tmp_path):
    """`local:` is an explicit request to route; the short-prompt rule must not take it away."""
    wrapper = (
        "import runpy, sys\n"
        "import llm_router.model_discovery as md\n"
        "md.first_installed = lambda *a, **k: 'fake-local-model'\n"
        "runpy.run_path(sys.argv[1], run_name='__main__')\n"
    )
    (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-c", wrapper, str(AUTO)],
        input=json.dumps({"prompt": "local: what is two plus two", "session_id": "fl"}),
        capture_output=True, text=True, env=_env(tmp_path), timeout=120,
    )
    ctx = _context(proc)
    assert "USER PREFIX `local:`" in ctx
    assert _pending(tmp_path, "fl").exists()
    assert _rows(tmp_path, "exempt") == []


def test_stale_holds_do_not_survive_into_a_short_prompt(tmp_path):
    """A hold from a long request must not outlive the next prompt, short or not."""
    sid = "stale"
    _context(_turn(tmp_path, LONG_GENERATE, sid))
    assert _pending(tmp_path, sid).exists()
    _context(_turn(tmp_path, "refactor the auth module", sid))
    assert not _pending(tmp_path, sid).exists()
    assert time.time() - os.path.getmtime(tmp_path / ".llm-router" / "hold_events.jsonl") < 60
