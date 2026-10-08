"""PLAN v16 P0.9 task 7: the Stop hook (session-end.py) returns without waiting on
work the per-turn line does not need, and names its phases; agent-route names its
phases too.

Live session-end p95 was 3,353 ms (n = 252, [HL7]) against the PRD's +300 ms sync
bar (P2-G-3). Every Stop ran, inline: a keychain read plus an HTTPS call to the
Anthropic usage endpoint (8 s timeout), the learned-profile rebuild, the auto-profile
rescan and the model-evaluator check. None of them feeds the per-turn line: the line
reads quota from usage.json, which that HTTPS call only refreshes for next time.
main() now reads the cached usage and spawns ONE detached child for the rest.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).parent.parent / "src" / "llm_router" / "hooks"
HOOK_PATH = HOOKS / "session-end.py"


def _load(path: Path = HOOK_PATH, name: str = "session_end_p09"):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


@pytest.fixture()
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    st = tmp_path / "state"
    st.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(st))
    monkeypatch.setenv("LLM_ROUTER_STOP_HOOK", "condensed")
    return st


@pytest.fixture()
def hook(state):
    return _load()


def _write_usage(state: Path, updated_at: float, **extra) -> dict:
    data = {"session_pct": 16.0, "weekly_pct": 40.0, "sonnet_pct": 5.0,
            "session_resets_at": "", "updated_at": updated_at, **extra}
    (state / "usage.json").write_text(json.dumps(data))
    return data


def _run_main(mod, monkeypatch, payload=None):
    payload = payload if payload is not None else {"session_id": "sess-p09", "hook_event_name": "Stop"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    t = time.monotonic()
    mod.main()
    return time.monotonic() - t, out.getvalue()


def _trap_slow_steps(hook, monkeypatch, tmp_path):
    """Replace every step that belongs in the child with a recorder; the live-usage
    fetch also sleeps as long as a slow keychain + HTTPS call does."""
    called: list[str] = []

    def fetch():
        called.append("_fetch_live_usage")
        time.sleep(3)
        return None

    monkeypatch.setattr(hook, "_fetch_live_usage", fetch)
    monkeypatch.setattr(hook, "_build_and_save_learned_profile",
                        lambda: called.append("_build_and_save_learned_profile"))
    import llm_router.auto_profile as ap

    monkeypatch.setattr(ap, "should_rescan", lambda: called.append("should_rescan") or False)
    import llm_router.model_evaluator as me

    async def _eval(*a, **k):
        called.append("evaluate_available_models")

    monkeypatch.setattr(me, "evaluate_available_models", _eval)
    # The Stop hook imports EVAL_CACHE_PATH; point it at a missing file so the
    # evaluator would be due if the hook ran it inline.
    monkeypatch.setattr(me, "EVAL_CACHE_PATH", tmp_path / "no-evals.json", raising=False)
    monkeypatch.setattr(me, "EVAL_TTL_SECONDS", 1, raising=False)
    return called


def test_stop_runs_none_of_the_moved_steps_inline(hook, state, monkeypatch, tmp_path):
    _write_usage(state, time.time())
    called = _trap_slow_steps(hook, monkeypatch, tmp_path)
    spawned = []
    monkeypatch.setattr(hook, "_spawn_background_stop_work", lambda: spawned.append(1),
                        raising=False)  # absent before P0.9: the test then fails on `called`
    elapsed, _out = _run_main(hook, monkeypatch)
    assert called == [], f"Stop ran {called} inline"
    assert spawned == [1], "exactly one background child per Stop"
    assert elapsed < 2.0, f"main() took {elapsed:.1f} s"


def test_stop_returns_while_a_5s_child_still_runs(hook, state, monkeypatch, tmp_path):
    """The real spawn path: the child is detached, so main() returns first."""
    _write_usage(state, time.time())
    _trap_slow_steps(hook, monkeypatch, tmp_path)
    marker = tmp_path / "child_done"
    monkeypatch.setattr(hook, "_background_stop_work_argv", lambda: [
        sys.executable, "-c",
        f"import time, pathlib; time.sleep(5); pathlib.Path({str(marker)!r}).write_text('done')"],
        raising=False)
    elapsed, _out = _run_main(hook, monkeypatch)
    assert elapsed < 2.0, f"main() took {elapsed:.1f} s"
    assert not marker.exists(), "main() waited for the child"
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.exists() and marker.read_text() == "done", "the detached child never ran"


def test_the_per_turn_line_reads_quota_from_the_cached_usage(hook, state, monkeypatch, tmp_path):
    _write_usage(state, time.time())
    _trap_slow_steps(hook, monkeypatch, tmp_path)
    monkeypatch.setattr(hook, "_spawn_background_stop_work", lambda: None, raising=False)
    _elapsed, out = _run_main(hook, monkeypatch)
    line = json.loads(out)["systemMessage"]
    assert "5h 16%" in line and "wk 40%" in line, line


def test_cached_usage_is_live_only_while_fresh(hook, state, monkeypatch):
    monkeypatch.setattr(hook, "_fetch_live_usage",
                        lambda: pytest.fail("the sync path must not fetch"))
    now = time.time()
    _write_usage(state, now - 5)
    _start, cur, live = hook._get_cc_usage()
    assert cur["weekly_pct"] == 40.0 and live is True
    _write_usage(state, now - hook._LIVE_USAGE_MAX_AGE_S - 30)
    _start, cur, live = hook._get_cc_usage()
    assert cur["weekly_pct"] == 40.0 and live is False
    _write_usage(state, now, is_fallback=True)
    assert hook._get_cc_usage()[1] is None, "the 50/50/50 fallback placeholder is not a reading"


def test_the_child_runs_every_moved_step_in_order(hook, state, monkeypatch):
    ran = []
    for name in hook._STOP_BACKGROUND_STEPS:
        monkeypatch.setattr(hook, name, (lambda n: lambda *a, **k: ran.append(n))(name))
    hook._run_background_stop_work()
    assert ran == list(hook._STOP_BACKGROUND_STEPS)
    assert ran[0] == "_fetch_live_usage"


def test_one_failing_child_step_does_not_skip_the_rest(hook, state, monkeypatch):
    ran = []
    for name in hook._STOP_BACKGROUND_STEPS:
        monkeypatch.setattr(hook, name, (lambda n: lambda *a, **k: ran.append(n))(name))

    def boom():
        raise RuntimeError("keychain exploded")

    monkeypatch.setattr(hook, "_fetch_live_usage", boom)
    hook._run_background_stop_work()
    assert ran == [n for n in hook._STOP_BACKGROUND_STEPS if n != "_fetch_live_usage"]


def test_a_child_note_reaches_the_next_full_summary_once(hook, state, monkeypatch, tmp_path):
    """The rescan used to append "Profile updated" to the full box inline; the child
    leaves it for the next Stop, which shows it once."""
    hook._append_stop_note("🔄 Profile updated: ollama")
    _write_usage(state, time.time())
    _trap_slow_steps(hook, monkeypatch, tmp_path)
    monkeypatch.setattr(hook, "_spawn_background_stop_work", lambda: None)
    monkeypatch.setenv("LLM_ROUTER_STOP_HOOK", "full")
    _e, out = _run_main(hook, monkeypatch)
    assert "Profile updated: ollama" in json.loads(out)["systemMessage"]
    _e, out = _run_main(hook, monkeypatch)
    assert "Profile updated" not in json.loads(out)["systemMessage"]


def test_the_child_writes_no_session_end_latency_row(hook, state, monkeypatch):
    """Same trap as BUGS P09-3: the child re-runs this file, so the latency stanza
    armed a session-end row for it; the entry point turns the recorder off first."""
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)
    monkeypatch.setattr(hook, "_run_background_stop_work", lambda: None)
    hook._entry(["--background-stop-work"])
    assert os.environ.get("LLM_ROUTER_HOOK_LATENCY") == "off"
    from llm_router import hook_latency

    assert hook_latency.record("session-end", "Stop", 9000.0) is False


def test_the_hook_itself_keeps_the_recorder_on(hook, state, monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)
    _trap_slow_steps(hook, monkeypatch, tmp_path)
    monkeypatch.setattr(hook, "_spawn_background_stop_work", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    hook._entry([])
    assert os.environ.get("LLM_ROUTER_HOOK_LATENCY") is None


def test_session_end_archives_without_spawning(hook, state, monkeypatch):
    """SessionEnd only archives: no child, no summary."""
    monkeypatch.setattr(hook, "_spawn_background_stop_work",
                        lambda: pytest.fail("SessionEnd must not spawn"))
    _e, out = _run_main(hook, monkeypatch, {"session_id": "s", "hook_event_name": "SessionEnd"})
    assert out == ""


@pytest.fixture()
def armed(monkeypatch):
    """Arm the recorder as a hook process does, without an exit-time write."""
    from llm_router import hook_latency as hl

    monkeypatch.setattr(hl, "_pending", None)
    monkeypatch.setattr(hl, "_phases", {})
    monkeypatch.setattr(hl, "_registered", True)  # no atexit registration from a test
    hl.begin("session-end", "Stop")
    yield hl
    monkeypatch.setattr(hl, "_pending", None)


def test_a_stop_run_names_its_phases(hook, state, monkeypatch, tmp_path, armed):
    _write_usage(state, time.time())
    _trap_slow_steps(hook, monkeypatch, tmp_path)
    monkeypatch.setattr(hook, "_spawn_background_stop_work", lambda: None)
    _run_main(hook, monkeypatch)
    ph = set(armed._phases)
    assert {"import", "session_io", "session_data", "cc_usage", "savings_sync", "cumulative",
            "render", "unverified_note", "spend", "trends", "notes", "quota_timeline",
            "routing_section", "northstar", "quality_breaker", "quota_sample", "bg_spawn",
            "emit"} <= ph, ph


#: Every phase name each hook may write (pinned by reading the source, as
#: test_kpi_hook_latency does for auto-route and session-start).
_PHASE_NAMES = {
    "session-end": {"session_io", "session_data", "cc_usage", "savings_sync", "cumulative",
                    "render", "unverified_note", "spend", "trends", "notes", "quota_timeline",
                    "routing_section", "northstar", "quality_breaker", "quota_sample", "bg_spawn",
                    "emit"},
    "agent-route": {"session_io", "budget_init", "depth", "classify", "codex_delegation",
                    "direct_subagent", "cli_delegation", "limits", "emit"},
}


@pytest.mark.parametrize("name", sorted(_PHASE_NAMES))
def test_the_phase_names_a_hook_source_uses_are_exactly_these(name):
    tree = ast.parse((HOOKS / f"{name}.py").read_text())
    # `_hl_phase("x")`, and `_laps.next("x")` for session-end's consecutive phases.
    used = {n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant)
            and ((isinstance(n.func, ast.Name) and n.func.id == "_hl_phase")
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == "next"
                     and ast.unparse(n.func.value) == "_laps"))}
    assert used == _PHASE_NAMES[name], (used ^ _PHASE_NAMES[name])


@pytest.mark.parametrize("name", ["session-end", "agent-route"])
def test_main_marks_the_import_phase_first(name):
    tree = ast.parse((HOOKS / f"{name}.py").read_text())
    (main,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    first_calls = [n.value.func.id for n in main.body
                   if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                   and isinstance(n.value.func, ast.Name)]
    assert "_hl_mark_main" in first_calls[:3], first_calls


def test_an_agent_route_run_names_its_phases(state, monkeypatch, armed):
    """A reasoning Agent call that no delegation takes reaches the block line:
    every phase up to `emit` ran and was named."""
    armed.set_event("PreToolUse")
    mod = _load(HOOKS / "agent-route.py", "agent_route_p09")
    monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_ALLOW_SPAWN", "0")
    monkeypatch.setattr(mod, "_headless_entrypoint", lambda: "cli")
    monkeypatch.setattr(mod, "_allow_routed_spawn", lambda: False)
    monkeypatch.setattr(mod, "_try_codex_subagent_delegation", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_try_direct_subagent", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_try_cli_delegation", lambda *a, **k: None)
    payload = {"tool_name": "Agent", "session_id": "sess-p09",
               "tool_input": {"prompt": "analyse the trade-offs of two cache designs",
                              "subagent_type": "general-purpose"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        mod.main()
    except SystemExit:
        pass
    ph = set(armed._phases)
    assert {"import", "session_io", "budget_init", "depth", "classify", "codex_delegation",
            "direct_subagent", "cli_delegation", "limits", "emit"} <= ph, ph



# ── P0.9 repair 1: one child per 15 s, not one per Stop ─────────────────────
# A burst of N Stops used to start N concurrent keychain + HTTPS children.


def test_a_burst_of_stops_starts_one_child(hook, state, monkeypatch):
    import llm_router.statusline_tick as tick

    started: list = []
    monkeypatch.setattr(tick, "_spawn_detached", started.append)
    for _ in range(5):
        hook._spawn_background_stop_work()
    assert len(started) == 1, started


def test_the_claim_expires_after_its_window(hook, state):
    t0 = 1_790_000_000.0
    assert hook._claim_background_stop_work(now=t0) is True
    assert hook._claim_background_stop_work(now=t0 + hook._STOP_BG_CLAIM_S - 1) is False
    assert hook._claim_background_stop_work(now=t0 + hook._STOP_BG_CLAIM_S + 1) is True


def test_an_unwritable_claim_starts_no_child(hook, state, monkeypatch):
    import llm_router.statusline_tick as tick

    started: list = []
    monkeypatch.setattr(tick, "_spawn_detached", started.append)
    monkeypatch.setattr(hook, "_state_dir", lambda: "/dev/null/not-a-dir")
    hook._spawn_background_stop_work()
    assert started == []
