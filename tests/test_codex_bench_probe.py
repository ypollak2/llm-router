"""R2 (routing plan 2026-11-04): a Codex bench ends when the evidence says so, and every fail-open is counted.

* A fresh Plus meter, newer than the bench and showing room in both windows (5 h under 90%, weekly under 95%), lifts the bench.
* While a bench has more than 24 h left, one real delegation per 3 h is let through as a canary; a success lifts the bench.
* The three silent ``except`` returns in agent-route (bench read, window reserve, window spend) now record a ``failopen`` code.

The hook runs in-process with a scripted stand-in for ``run_codex``: no real ``codex exec`` is ever started.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

from llm_router import codex_bench, codex_meter, codex_window, failopen, paths, provider_bench_log, provider_reset
from llm_router.codex_agent import CodexResult
from tests._bind_support import quota_text
from tests._codex_meter_support import rate_limits, write_rollout

REPO = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO / "src" / "llm_router" / "hooks" / "agent-route.py"
HOUR = 3600
PROMPT = "analyze the auth module for bugs"


def _load_hook():
    spec = importlib.util.spec_from_file_location("agent_route_bench_probe", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


class _Codex:
    """Scripted stand-in for run_codex: one result per call, every call's kwargs recorded."""

    def __init__(self, monkeypatch, results):
        self.results = list(results)
        self.calls: list[dict] = []
        monkeypatch.setattr("llm_router.codex_agent.run_codex", self)

    async def __call__(self, prompt, model="gpt-5.5", timeout=None, **kw):
        self.calls.append({"model": model, **kw})
        return self.results.pop(0)


def _ok(model="gpt-6-astra", content="answer"):
    return CodexResult(content=content, model=model, exit_code=0, duration_sec=0.1)


def _quota(model="gpt-6-astra"):
    return CodexResult(content=quota_text(), model=model, exit_code=1, duration_sec=0.1)


@pytest.fixture
def env(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
    monkeypatch.setenv("LLM_ROUTER_ROUTE_BANNER", "off")
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(tmp_path / "reset.json"))
    for name in ("LLM_ROUTER_CODEX_AGENT_MODEL", "LLM_ROUTER_CODEX_BENCH_PROBE", "LLM_ROUTER_CODEX_WINDOW_BUDGET",
                 "LLM_ROUTER_CODEX_RESERVE_PCT", "LLM_ROUTER_SUBAGENT_CLI_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
    failopen.reset_cache()
    failopen.clear()
    return codex_home


@pytest.fixture
def hook(env):
    return _load_hook()


def _bench(until_in_s: float, *, set_ago_s: float = 0.0, key: str = "codex") -> float:
    now = time.time()
    assert provider_reset.record_provider_reset(key, now + until_in_s, reason="usage limit", now=now - set_ago_s, source="cli")
    return now


def _ns3(hook, prompt=PROMPT):
    return hook._try_codex_subagent_delegation(prompt, "analyze", "moderate", "general-purpose", "sess-probe")


def _ledger(lever: str | None = None) -> list[dict]:
    f = paths.state_path("north_star_units.jsonl")
    rows = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
    return [r for r in rows if lever is None or r.get("lever") == lever]


def _meter_events() -> list[dict]:
    f = paths.state_path(codex_meter.EVENT_LOG)
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def _unbans() -> list[dict]:
    return [e for e in provider_bench_log.read_events() if e["kind"] == "unban"]


def _fresh_meter(env, *, weekly, five, age_s=60.0, **kw):
    now = time.time()
    write_rollout(env, [(now - age_s, rate_limits(weekly=weekly, five=five, now=now, **kw))])


# --------------------------------------------------------------------------------------------- the canary


def test_a_long_bench_lets_one_run_through_and_a_success_lifts_the_bench(hook, monkeypatch):
    _bench(48 * HOUR)
    codex = _Codex(monkeypatch, [_ok()])
    assert _ns3(hook) == "answer"
    assert len(codex.calls) == 1 and codex.calls[0]["caller"] == "agent_route"
    assert provider_reset.get_provider_reset_until("codex") is None  # the probe worked: the bench was wrong
    delegated = [r for r in _ledger("agent_route_codex") if r["outcome"] == "delegated"]
    assert len(delegated) == 1 and delegated[0]["canary"] is True
    assert [e["kind"] for e in _meter_events()] == ["canary_claimed", "bench_cleared"]
    assert _meter_events()[1]["source"] == "canary"
    assert [(u["provider"], u.get("source")) for u in _unbans()] == [("codex", "canary")]


def test_only_one_canary_per_three_hours_across_hook_runs(env, monkeypatch):
    _bench(48 * HOUR)
    first = _load_hook()
    codex = _Codex(monkeypatch, [_quota()])
    assert _ns3(first) is None  # the probe hit the wall again: benched as before
    assert len(codex.calls) == 1
    rows = _ledger("agent_route_codex")
    assert rows[-1]["outcome"] == "codex_quota" and rows[-1]["canary"] is True
    assert provider_reset.get_provider_reset_until("codex") is not None

    second = _load_hook()  # the next hook run, an hour later in effect: no second canary
    assert _ns3(second) is None
    assert len(codex.calls) == 1
    assert _ledger("agent_route_codex")[-1]["outcome"] == "benched"
    assert "canary" not in _ledger("agent_route_codex")[-1]

    stamp = paths.state_path(codex_bench.CANARY_FILE)
    stamp.write_text(json.dumps({"last": time.time() - 3 * HOUR - 5}))  # three hours on
    third = _load_hook()
    codex.results = [_ok()]
    assert _ns3(third) == "answer"
    assert len(codex.calls) == 2
    assert provider_reset.get_provider_reset_until("codex") is None


def test_a_failed_canary_does_not_lift_the_bench(hook, monkeypatch):
    _bench(48 * HOUR)
    _Codex(monkeypatch, [_quota()])
    assert _ns3(hook) is None
    assert provider_reset.get_provider_reset_until("codex") is not None
    assert [u for u in _unbans()] == []


def test_a_short_bench_gets_no_canary(hook, monkeypatch):
    _bench(12 * HOUR)
    codex = _Codex(monkeypatch, [_ok()])
    assert _ns3(hook) is None
    assert codex.calls == [] and not paths.state_path(codex_bench.CANARY_FILE).exists()


def test_the_canary_slot_is_not_used_up_by_a_task_declined_for_another_reason(hook, monkeypatch):
    """The slot is claimed before a window slot or a budget unit is spent, and handed back when the task is then declined."""
    until = time.time() + 48 * HOUR
    _bench(48 * HOUR)
    for _ in range(codex_window.budget()):
        codex_window.record_delegation(time.time() - 60)  # the 5 h window is spent
    codex = _Codex(monkeypatch, [_ok()])
    assert _ns3(hook) is None
    assert codex.calls == []
    assert [r["outcome"] for r in _ledger("agent_route_codex")] == ["window_exhausted"]
    assert hook._canary_claimed is False and hook._canary_claim is None
    assert codex_bench.canary_due(until)  # the next task can be the probe
    assert [e["kind"] for e in _meter_events()] == ["canary_claimed", "canary_released"]


def test_the_probe_can_be_switched_off(env, hook, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CODEX_BENCH_PROBE", "off")
    _bench(48 * HOUR, set_ago_s=600)
    _fresh_meter(env, weekly=10.0, five=10.0, age_s=5.0)  # room in both windows, and newer than the bench
    codex = _Codex(monkeypatch, [_ok()])
    assert _ns3(hook) is None and codex.calls == []
    assert provider_reset.get_provider_reset_until("codex") is not None


def test_two_processes_cannot_both_claim_the_slot(env):
    until = time.time() + 48 * HOUR
    assert codex_bench.canary_due(until)
    claim = codex_bench.claim_canary(until)
    assert claim is not None and claim.previous == 0.0
    assert codex_bench.claim_canary(until) is None
    assert codex_bench.canary_due(until) is False
    assert codex_bench.canary_due(until, time.time() + 3 * HOUR + 1) is True


def test_a_canary_that_cannot_be_decided_is_not_taken(env, monkeypatch):
    monkeypatch.setattr(codex_bench, "exclusive_lock", lambda *a, **k: (_ for _ in ()).throw(OSError("no locks")))
    assert codex_bench.claim_canary(time.time() + 48 * HOUR) is None
    assert failopen.snapshot().by_code.get("CHZ-FO-CODEX-CANARY-CLAIM") == 1


# ---------------------------------------------------------------------------------- a fresh meter lifts a bench


def test_a_fresh_meter_with_room_lifts_a_bench_it_postdates(env, hook, monkeypatch):
    _bench(5 * HOUR, set_ago_s=600)  # recorded ten minutes ago, 5 h to run
    _fresh_meter(env, weekly=40.0, five=30.0, age_s=60.0)
    codex = _Codex(monkeypatch, [_ok()])
    assert _ns3(hook) == "answer"
    assert len(codex.calls) == 1
    assert [(u["provider"], u.get("source")) for u in _unbans()] == [("codex", "meter")]
    event = [e for e in _meter_events() if e["kind"] == "bench_cleared"][0]
    assert event["source"] == "meter" and event["weekly_used_pct"] == 40.0 and event["five_hour_used_pct"] == 30.0


@pytest.mark.parametrize("name,weekly,five,age_s,set_ago_s", [
    ("reading predates the bench", 40.0, 30.0, 900.0, 600.0),
    ("5 h window at 95%", 40.0, 95.0, 60.0, 600.0),
    ("5 h window at exactly 90%", 40.0, 90.0, 60.0, 600.0),
    ("weekly window at 96%", 96.0, 30.0, 60.0, 600.0),
    ("weekly window at exactly 95%", 95.0, 30.0, 60.0, 600.0),
    ("reading older than 30 minutes", 40.0, 30.0, 31 * 60.0, 3 * HOUR),
])
def test_a_meter_that_does_not_contradict_the_bench_leaves_it(env, hook, monkeypatch, name, weekly, five, age_s, set_ago_s):
    """The 14 fresh-meter refusals of 2026-10-04..11-03 were 5 h at 0% with the weekly meter at 100%: right. A reading counts only if
    it is newer than the bench, fresh, and shows room in BOTH windows."""
    _bench(5 * HOUR, set_ago_s=set_ago_s)
    _fresh_meter(env, weekly=weekly, five=five, age_s=age_s)
    codex = _Codex(monkeypatch, [_ok()])
    assert _ns3(hook) is None, name
    assert codex.calls == [] and provider_reset.get_provider_reset_until("codex") is not None
    assert _unbans() == []


def test_a_reading_with_only_one_window_is_not_room(env, hook, monkeypatch):
    _bench(5 * HOUR, set_ago_s=600)
    _fresh_meter(env, weekly=None, five=10.0)
    codex = _Codex(monkeypatch, [_ok()])
    assert _ns3(hook) is None and codex.calls == []


def test_no_rollouts_are_read_when_nothing_is_benched(env, monkeypatch):
    monkeypatch.setattr(codex_meter, "read_meter", lambda *a, **k: pytest.fail("read the rollouts with no bench"))
    assert codex_bench.clear_if_meter_shows_headroom(("codex", "codex:gpt-6-astra")) == []


def test_the_model_bench_is_lifted_with_the_account_bench(env, hook, monkeypatch):
    _bench(5 * HOUR, set_ago_s=600, key="codex:gpt-6-astra")
    _fresh_meter(env, weekly=20.0, five=20.0)
    assert hook._codex_bench_until() is None
    assert [u["provider"] for u in _unbans()] == ["codex:gpt-6-astra"]


# ------------------------------------------------------------------------- the three silent excepts are counted


def _codes() -> dict[str, int]:
    failopen.reset_cache()
    return dict(failopen.snapshot().by_code)


def test_a_bench_that_cannot_be_read_is_counted_and_still_fails_open(hook, monkeypatch):
    monkeypatch.setattr(provider_reset, "get_provider_reset_until", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert hook._codex_bench_until() is None
    assert _codes() == {"CHZ-FO-AGENT-ROUTE-CODEX-BENCH-READ": 1}


def test_a_window_reserve_that_fails_is_counted_and_still_admits(hook, monkeypatch):
    monkeypatch.setattr(codex_window, "reserve", lambda **k: (_ for _ in ()).throw(OSError("lock")))
    assert hook._codex_window_declines("ns3", PROMPT, "general-purpose", "analyze", "moderate", "s") is False
    assert _codes() == {"CHZ-FO-AGENT-ROUTE-CODEX-WINDOW-RESERVE": 1}


def test_a_window_spend_that_fails_is_counted_and_still_dispatches(hook, monkeypatch):
    monkeypatch.setattr(codex_window, "reserve", lambda **k: (_ for _ in ()).throw(OSError("lock")))
    assert hook._spend_window_slot() is True
    assert _codes() == {"CHZ-FO-AGENT-ROUTE-CODEX-WINDOW-SPEND": 1}


def test_a_window_release_that_fails_is_counted(hook, monkeypatch):
    monkeypatch.setattr(codex_window, "release", lambda *a, **k: (_ for _ in ()).throw(OSError("lock")))
    hook._held_reservation, hook._held_token = True, 123.0
    hook._release_reservation()
    assert _codes() == {"CHZ-FO-AGENT-ROUTE-CODEX-WINDOW-RELEASE": 1}
    assert hook._held_reservation is False


def test_the_counter_helper_itself_never_raises(hook, monkeypatch):
    monkeypatch.setattr(failopen, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store gone")))
    hook._failopen("CHZ-FO-X", OSError("x"))  # must return


def test_a_broken_probe_does_not_hide_the_bench(hook, monkeypatch):
    """The bench is read even when the probe module is missing or raises: a hook newer than the package must not open Codex."""
    _bench(5 * HOUR)
    monkeypatch.setattr(codex_bench, "clear_if_meter_shows_headroom", lambda *a, **k: (_ for _ in ()).throw(ImportError("old package")))
    assert hook._codex_bench_until() is not None
    assert _codes() == {"CHZ-FO-AGENT-ROUTE-CODEX-BENCH-CLEAR": 1}


# ------------------------------------------------------------------- canary concurrency (review item 5)


def _daily_count() -> int:
    f = paths.state_path(codex_meter.DAILY_COUNTER_FILE)
    return json.loads(f.read_text())["count"] if f.exists() else 0


def test_a_run_that_loses_the_canary_claim_spends_no_budget_and_no_window_slot(hook, monkeypatch):
    """N sub-agents spawned together all pass the 'is a canary due' question; only one wins the claim, and the N-1 others must
    not have spent a daily-budget unit or held a window slot before they lose."""
    _bench(48 * HOUR)
    monkeypatch.setattr(codex_bench, "claim_canary", lambda *a, **k: None)  # another run took it between the question and the claim
    codex = _Codex(monkeypatch, [_ok()])
    assert _ns3(hook) is None
    assert codex.calls == []
    assert _daily_count() == 0
    assert codex_window.snapshot().used == 0
    row = _ledger("agent_route_codex")[-1]
    assert row["outcome"] == "benched" and "canary slot" in row["reason"]
    assert provider_reset.get_provider_reset_until("codex") is not None


def test_n_concurrent_runs_one_probe_n_minus_one_untouched(env, monkeypatch):
    """Four hook runs on four threads against one long bench, held at a barrier so that ALL of them have passed 'is a canary
    due' before ANY has claimed it: one takes the slot and makes the only Codex call; the budget moves by one; the three
    losers spend no daily-budget unit and hold no window slot."""
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _bench(48 * HOUR)
    runs = [_load_hook() for _ in range(4)]
    barrier, local, calls = threading.Barrier(4, timeout=20), threading.local(), []
    real_due = codex_bench.canary_due

    def due(until, now=None):
        if not getattr(local, "waited", False):  # each thread's first question is the gate's; the claim's own re-check comes later
            local.waited = True
            barrier.wait()
        return real_due(until, now)

    async def slow_codex(prompt, model="gpt-5.5", timeout=None, **kw):
        calls.append(kw)
        await asyncio.sleep(0.6)  # the probe is still running while the losers settle
        return _ok()

    monkeypatch.setattr(codex_bench, "canary_due", due)
    monkeypatch.setattr("llm_router.codex_agent.run_codex", slow_codex)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_ns3, runs))
    assert results.count("answer") == 1 and results.count(None) == 3
    assert len(calls) == 1 and calls[0]["caller"] == "agent_route"
    assert _daily_count() == 1
    assert codex_window.snapshot().used == 1
    outcomes = sorted(r["outcome"] for r in _ledger("agent_route_codex"))
    assert outcomes == ["benched", "benched", "benched", "delegated"]
    assert provider_reset.get_provider_reset_until("codex") is None  # and the winner's success lifted the bench


def test_a_canary_slot_is_handed_back_when_no_codex_call_is_made(hook, monkeypatch):
    """No time left for a run: the probe never ran, so the 3 h wait must not be spent on it."""
    until = time.time() + 48 * HOUR
    _bench(48 * HOUR)
    monkeypatch.setattr(hook, "_delegation_time_left", lambda configured: 0)
    codex = _Codex(monkeypatch, [_ok()])
    assert _ns3(hook) is None
    assert codex.calls == []
    assert [r["reason_code"] for r in _ledger("agent_route_codex") if r.get("reason_code")] == ["no_time"]
    assert hook._canary_claimed is False
    assert codex_bench.canary_due(until)
    assert provider_reset.get_provider_reset_until("codex") is not None


def test_a_canary_slot_that_made_a_call_stays_taken_even_when_the_call_failed(hook, monkeypatch):
    until = time.time() + 48 * HOUR
    _bench(48 * HOUR)
    _Codex(monkeypatch, [CodexResult(content="codex: empty completion (no output)", model="gpt-6-astra", exit_code=1,
                                     duration_sec=0.1, reason_code="empty_completion")])
    assert _ns3(hook) is None
    assert hook._canary_claimed is True
    assert codex_bench.canary_due(until) is False  # the probe ran (and failed): the next one is 3 h away


def test_releasing_a_slot_another_run_has_since_taken_changes_nothing(env):
    until = time.time() + 48 * HOUR
    claim = codex_bench.claim_canary(until)
    later = time.time() + 5
    paths.state_path(codex_bench.CANARY_FILE).write_text(json.dumps({"last": later}))  # a later claim, by someone else
    codex_bench.release_canary(claim)
    assert json.loads(paths.state_path(codex_bench.CANARY_FILE).read_text())["last"] == later
    codex_bench.release_canary(None)  # nothing held: nothing to do


# ------------------------------------------------- the bench comparison is made under the lock (review item 5)


def test_a_bench_recorded_after_the_entries_were_read_is_not_lifted_by_an_older_reading(env, monkeypatch):
    """The check 'the reading is newer than the bench' was made on entries read BEFORE the meter was read; another process can
    re-bench in between. The same comparison is now made against the bench file under its lock."""
    now = time.time()
    _bench(5 * HOUR, set_ago_s=600)
    stale_entries = provider_reset.provider_reset_entries()  # what the caller read first
    _fresh_meter(env, weekly=20.0, five=20.0, age_s=300.0)  # reading from 5 minutes ago
    _bench(5 * HOUR, set_ago_s=60)  # ... and a new bench, recorded one minute ago, after that reading
    monkeypatch.setattr(provider_reset, "provider_reset_entries", lambda *a, **k: stale_entries)
    assert codex_bench.clear_if_meter_shows_headroom(("codex",), now) == []
    assert provider_reset.get_provider_reset_until("codex") is not None
    assert _unbans() == []


def test_a_canary_success_does_not_lift_a_bench_recorded_after_the_probe_began(env):
    until = time.time() + 48 * HOUR
    claim = codex_bench.claim_canary(until)
    _bench(5 * HOUR, set_ago_s=-5)  # recorded 5 s after the claim, by a run that hit the wall
    assert codex_bench.clear_after_canary(("codex",), claim) == []
    assert provider_reset.get_provider_reset_until("codex") is not None


def test_clear_provider_reset_if_set_before_is_decided_under_the_lock(env):
    now = time.time()
    _bench(5 * HOUR, set_ago_s=100)
    assert provider_reset.clear_provider_reset("codex", now, source="meter", if_set_before=now - 200) == []
    assert provider_reset.clear_provider_reset("codex", now, source="meter", if_set_before=now - 50) == ["codex"]
    assert provider_reset.clear_provider_reset("codex") == []  # nothing left; the owner's form is unchanged


# ------------------------------------------------------------------- deploy skew between the hook and the package (item 4)


def test_the_hook_passes_caller_only_to_a_run_codex_that_takes_it(hook):
    from llm_router import codex_agent

    async def package_before_d373(prompt, model="gpt-5.5", timeout=None, sandbox=None):
        return None

    async def takes_caller(prompt, *, caller=""):
        return None

    async def takes_anything(prompt, **kw):
        return None

    assert hook._caller_kwargs(package_before_d373) == {}
    assert hook._caller_kwargs(takes_caller) == {"caller": "agent_route"}
    assert hook._caller_kwargs(takes_anything) == {"caller": "agent_route"}
    assert hook._caller_kwargs(codex_agent.run_codex) == {"caller": "agent_route"}  # this package's own
    assert hook._caller_kwargs(object()) == {}  # not introspectable: leave it out rather than guess


def test_a_hook_newer_than_the_package_still_delegates(hook, monkeypatch):
    """Hook 28 with a package whose run_codex has no ``caller``: the keyword used to raise TypeError and fail every delegation."""
    seen = []

    async def old_run_codex(prompt, model="gpt-5.5", working_dir=None, timeout=None, on_event=None, context_root=None,
                            messages=None, transcript_path=None, target_window=0, sandbox=None):
        seen.append(sandbox)
        return _ok()

    monkeypatch.setattr("llm_router.codex_agent.run_codex", old_run_codex)
    assert _ns3(hook) == "answer"
    assert seen == ["workspace-write"] or seen == ["read-only"]
