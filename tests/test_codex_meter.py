"""D-373: the Plus meter is read from Codex's own rollouts and reserved for agent-route.

Every Codex caller except agent-route refuses once the weekly meter passes 60% (``LLM_ROUTER_CODEX_RESERVE_PCT``). A meter older
than 30 minutes is not trusted; without a fresh one the router's own counters decide, and if those cannot be read the call is
refused (never "unlimited"). Every refusal is logged. No real Codex is started: the rollouts are written here, in the shape of a copy
of a real one (``tests/_codex_meter_support.py``).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_router import codex_agent, codex_meter, codex_window, paths
from tests._codex_meter_support import iso, rate_limits, write_rollout

REPO = Path(__file__).resolve().parents[1]
HOUR = 3600


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("LLM_ROUTER_CODEX_RESERVE_PCT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_AGENT_ROUTE_CODEX_DAILY_BUDGET", raising=False)
    monkeypatch.delenv("LLM_ROUTER_CODEX_WINDOW_BUDGET", raising=False)
    codex_meter._noted.clear()  # the once-per-process failopen notes: a test must see its own
    return home


def _events() -> list[dict]:
    path = paths.state_path(codex_meter.EVENT_LOG)
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _meter_at(codex_home, weekly, five=10.0, age_s=60.0, now=None, **kw):
    now = time.time() if now is None else now
    write_rollout(codex_home, [(now - age_s, rate_limits(weekly=weekly, five=five, now=now, **kw))])
    return now


# ------------------------------------------------------------------------------------------------ reading


def test_the_newest_reading_is_chosen_by_its_event_time_not_by_file_name_or_order(codex_home):
    now = time.time()
    write_rollout(codex_home, [(now - 600, rate_limits(weekly=40.0, five=5.0, now=now))], file_clock=now + 5 * HOUR)
    write_rollout(codex_home, [(now - 120, rate_limits(weekly=44.0, five=9.0, now=now))], file_clock=now - 9 * HOUR)
    meter = codex_meter.read_meter(now)
    assert (meter.weekly_used_pct, meter.five_hour_used_pct) == (44.0, 9.0)
    assert meter.observed_at == pytest.approx(now - 120, abs=0.01)


def test_the_last_reading_inside_one_file_wins(codex_home):
    now = time.time()
    write_rollout(codex_home, [(now - 300, rate_limits(weekly=30.0, five=1.0, now=now)),
                               (now - 200, rate_limits(weekly=31.0, five=2.0, now=now)),
                               (now - 100, rate_limits(weekly=32.0, five=3.0, now=now))])
    assert codex_meter.read_meter(now).weekly_used_pct == 32.0


def test_a_premium_event_with_null_windows_is_skipped(codex_home):
    now = time.time()
    write_rollout(codex_home, [(now - 200, rate_limits(weekly=22.0, five=8.0, now=now)),
                               (now - 100, rate_limits(weekly=None, five=None, now=now, limit_id="premium"))])
    assert codex_meter.read_meter(now).weekly_used_pct == 22.0


def test_a_meter_older_than_thirty_minutes_is_not_trusted(codex_home):
    now = time.time()
    write_rollout(codex_home, [(now - 31 * 60, rate_limits(weekly=80.0, five=10.0, now=now))])
    assert codex_meter.read_meter(now) is None
    write_rollout(codex_home, [(now - 29 * 60, rate_limits(weekly=81.0, five=10.0, now=now))])
    assert codex_meter.read_meter(now).weekly_used_pct == 81.0


def test_a_reading_from_the_future_is_not_trusted(codex_home):
    now = time.time()
    write_rollout(codex_home, [(now + 3600, rate_limits(weekly=90.0, five=10.0, now=now))], mtime=now)
    assert codex_meter.read_meter(now) is None


def test_a_window_that_has_reset_since_the_reading_is_dropped(codex_home):
    now = time.time()
    write_rollout(codex_home, [(now - 120, rate_limits(weekly=95.0, five=99.0, now=now, weekly_resets=now - 10,
                                                       five_resets=now + 3600))])
    meter = codex_meter.read_meter(now)
    assert meter.weekly_used_pct is None and meter.five_hour_used_pct == 99.0
    write_rollout(codex_home, [(now - 60, rate_limits(weekly=95.0, five=99.0, now=now, weekly_resets=now - 10,
                                                      five_resets=now - 5))])
    assert codex_meter.read_meter(now) is None  # neither window is still the one that was read


def test_garbage_and_missing_state_read_as_no_meter(codex_home, tmp_path):
    assert codex_meter.read_meter() is None  # no sessions directory at all
    now = time.time()
    path = write_rollout(codex_home, [(now - 60, rate_limits(weekly=10.0, five=1.0, now=now))])
    path.write_text('{"rate_limits": \n not json \n{"payload": {"rate_limits": "str"}}\n', encoding="utf-8")
    assert codex_meter.read_meter(now) is None


def test_only_the_tail_of_a_huge_rollout_is_read(codex_home):
    now = time.time()
    path = write_rollout(codex_home, [(now - 60, rate_limits(weekly=55.0, five=1.0, now=now))], noise=0)
    filler = "x" * 4000
    with open(path, "r+", encoding="utf-8") as fh:
        body = fh.read()
        fh.seek(0)
        fh.write(("".join(json.dumps({"timestamp": iso(now - 3000), "type": "response_item", "payload": {"t": filler}}) + "\n"
                          for _ in range(600))) + body)  # about 2.4 MB in front of the reading
    assert codex_meter.read_meter(now).weekly_used_pct == 55.0


def test_the_reader_never_writes_into_codex_home(codex_home):
    now = _meter_at(codex_home, 20.0)
    before = sorted((p, p.stat().st_mtime_ns, p.stat().st_size) for p in codex_home.rglob("*"))
    codex_meter.read_meter(now)
    codex_meter.check("llm_codex", now)
    assert before == sorted((p, p.stat().st_mtime_ns, p.stat().st_size) for p in codex_home.rglob("*"))


# ------------------------------------------------------------------------------------------------ the reserve


def test_agent_route_is_exempt_and_every_other_caller_is_guarded(codex_home):
    now = _meter_at(codex_home, 99.0, five=99.0)
    assert codex_meter.check("agent_route", now).allowed
    for caller in ("llm_codex", "router_chain", "text_job", "evaluator", "calibrate", "session_broker", "", "anything-new"):
        assert not codex_meter.check(caller, now).allowed, caller


def test_the_line_is_at_sixty_percent_of_the_weekly_meter(codex_home):
    now = _meter_at(codex_home, 59.9)
    assert codex_meter.check("llm_codex", now).allowed
    now = _meter_at(codex_home, 60.0)
    verdict = codex_meter.check("llm_codex", now)
    assert not verdict.allowed and verdict.source == "meter" and verdict.weekly_used_pct == 60.0


def test_the_five_hour_window_refuses_at_ninety_percent_whatever_the_week_says(codex_home):
    """The counter branch refuses when the router's own 5 h window is spent; the meter branch draws the same line."""
    now = _meter_at(codex_home, 10.0, five=89.9)
    assert codex_meter.check("llm_codex", now).allowed
    now = _meter_at(codex_home, 10.0, five=90.0)
    verdict = codex_meter.check("llm_codex", now)
    assert not verdict.allowed and verdict.source == "meter" and "5 h meter at 90%" in verdict.reason
    assert codex_meter.check("agent_route", now).allowed  # the reserved caller is never held by it


def test_the_line_is_tunable_and_can_be_switched_off(codex_home, monkeypatch):
    now = _meter_at(codex_home, 70.0)
    monkeypatch.setenv("LLM_ROUTER_CODEX_RESERVE_PCT", "80")
    assert codex_meter.check("llm_codex", now).allowed
    monkeypatch.setenv("LLM_ROUTER_CODEX_RESERVE_PCT", "65")
    assert not codex_meter.check("llm_codex", now).allowed
    monkeypatch.setenv("LLM_ROUTER_CODEX_RESERVE_PCT", "off")
    assert codex_meter.check("llm_codex", now).source == "off" and codex_meter.check("llm_codex", now).allowed
    monkeypatch.setenv("LLM_ROUTER_CODEX_RESERVE_PCT", "0")
    assert not codex_meter.check("llm_codex", _meter_at(codex_home, 1.0)).allowed
    monkeypatch.setenv("LLM_ROUTER_CODEX_RESERVE_PCT", "lots")
    assert codex_meter.reserve_pct() == codex_meter.DEFAULT_RESERVE_PCT


def _no_process(monkeypatch) -> None:
    """The CLI must not be started. Patch the spawn itself: ``find_codex_binary`` is also asked, on a fresh worker, by the
    availability cache that route_and_call builds its chain from, which is not a start."""
    async def boom(*_a, **_k):
        raise AssertionError("a Codex process was started")

    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", boom)


def _spend_daily(count: int, now: float | None = None) -> None:
    today = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).date().isoformat()
    path = paths.state_path(codex_meter.DAILY_COUNTER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": today, "count": count}))


def test_without_a_fresh_meter_the_counters_decide_and_never_open_the_meter(codex_home):
    now = time.time()
    # a stale reading is no reading
    write_rollout(codex_home, [(now - 2 * HOUR, rate_limits(weekly=5.0, five=1.0, now=now))])
    quiet = codex_meter.check("llm_codex", now)
    assert quiet.allowed and quiet.source == "counters"

    _spend_daily(59)
    assert codex_meter.check("llm_codex", now).allowed
    _spend_daily(60)
    busy = codex_meter.check("llm_codex", now)
    assert not busy.allowed and busy.source == "counters" and "60 of its 100" in busy.reason

    _spend_daily(0)
    for _ in range(codex_window.budget()):
        codex_window.record_delegation(now - 60)
    spent = codex_meter.check("llm_codex", now)
    assert not spent.allowed and "5 h window is spent" in spent.reason


def test_yesterdays_daily_count_does_not_count(codex_home):
    now = time.time()
    path = paths.state_path(codex_meter.DAILY_COUNTER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": "2020-01-01", "count": 99}))
    assert codex_meter.check("llm_codex", now).allowed


def test_when_the_counters_cannot_be_read_either_the_call_is_refused(codex_home):
    path = paths.state_path(codex_meter.DAILY_COUNTER_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    verdict = codex_meter.check("llm_codex")
    assert not verdict.allowed and verdict.source == "unknown"


def test_a_refusal_is_logged_every_time_and_an_allowed_call_is_not(codex_home):
    now = _meter_at(codex_home, 75.0)
    assert codex_meter.refusal("llm_codex", now) is not None
    assert codex_meter.refusal("router_chain", now) is not None
    assert codex_meter.refusal("agent_route", now) is None
    rows = _events()
    assert [(r["kind"], r["caller"]) for r in rows] == [("refused", "llm_codex"), ("refused", "router_chain")]
    assert rows[0]["weekly_used_pct"] == 75.0 and rows[0]["source"] == "meter" and rows[0]["reserve_pct"] == 60.0
    assert all(set(r) <= {"ts", "kind", "caller", "source", "reason", "reserve_pct", "weekly_used_pct", "meter_age_s"} for r in rows)


def test_the_refusal_text_cannot_be_read_as_a_usage_limit_report(codex_home):
    """The chain parses a failed Codex call's text for a reset time and benches the provider; a refusal must not."""
    from llm_router import provider_reset

    for now in (_meter_at(codex_home, 90.0), ):
        text = codex_meter.refusal("router_chain", now)
        assert text and provider_reset.parse_reset_epoch(text) is None
        assert not provider_reset._LIMIT_SIGNAL_RE.search(text)
    _spend_daily(80)
    text = codex_meter.refusal("router_chain", time.time() + 4 * HOUR)  # meter now stale: counters speak
    assert text and not provider_reset._LIMIT_SIGNAL_RE.search(text)


def test_the_daily_budget_default_matches_the_hook():
    import importlib.util

    spec = importlib.util.spec_from_file_location("agent_route_for_budget", REPO / "src/llm_router/hooks/agent-route.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    assert codex_meter.DEFAULT_DAILY_BUDGET == hook._CODEX_SUBAGENT_DEFAULT_DAILY_BUDGET
    assert codex_meter.DAILY_COUNTER_FILE == hook._codex_subagent_budget_file().name


# ------------------------------------------------------------------------------------------------ run_codex and its callers


def _run(**kw):
    return asyncio.run(codex_agent.run_codex("hello", **kw))


def test_run_codex_refuses_a_guarded_caller_before_anything_starts(codex_home, monkeypatch):
    _meter_at(codex_home, 88.0)

    def boom(*_a, **_k):
        raise AssertionError("a refused call must not look for the CLI")

    monkeypatch.setattr(codex_agent, "find_codex_binary", boom)
    _no_process(monkeypatch)
    res = _run(model="gpt-6-astra", caller="llm_codex")
    assert not res.success and res.reason_code == "meter_reserved" and res.duration_sec == 0.0
    assert "reserved for sub-agent delegation" in res.content
    res = _run(model="gpt-6-astra")  # a caller that names nothing is guarded too
    assert res.reason_code == "meter_reserved"
    assert [r["caller"] for r in _events()] == ["llm_codex", "unspecified"]


def test_run_codex_lets_agent_route_through_the_same_meter(codex_home, monkeypatch):
    _meter_at(codex_home, 99.0, five=99.0)
    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: None)
    res = _run(model="gpt-6-astra", caller="agent_route")
    assert res.reason_code == "binary_missing"  # it got as far as looking for the binary
    assert _events() == []


def test_a_broken_guard_refuses_rather_than_opens(codex_home, monkeypatch):
    monkeypatch.setattr(codex_meter, "refusal", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("bug")))
    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: None)
    assert _run(model="gpt-6-astra", caller="llm_codex").reason_code == "meter_reserved"


def test_the_llm_codex_tool_returns_the_refusal_and_logs_it(codex_home, monkeypatch):
    from llm_router.tools import codex as codex_tool

    _meter_at(codex_home, 72.0)
    monkeypatch.setattr(codex_tool, "is_codex_available", lambda: True)
    _no_process(monkeypatch)
    out = asyncio.run(codex_tool.llm_codex("what is 2+2"))
    assert "reserved for sub-agent delegation" in out
    assert [(r["kind"], r["caller"]) for r in _events()] == [("refused", "llm_codex")]


def test_the_chain_tells_the_guard_who_it_is(codex_home):
    from llm_router import eligibility_routing, router

    assert router._codex_caller() == "router_chain"
    token = eligibility_routing._text_job_ctx.set(("commit_message", 100))
    try:
        assert router._codex_caller() == "text_job"
    finally:
        eligibility_routing._text_job_ctx.reset(token)


@pytest.mark.asyncio
async def test_route_and_call_passes_the_chain_caller_to_run_codex(temp_db, mock_env, monkeypatch, tmp_path):
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile, TaskType

    monkeypatch.delenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_BLOCK_PROVIDERS", raising=False)
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.97)
    captured: dict = {}

    async def _fake(prompt, **kwargs):
        captured.update(kwargs)
        return codex_agent.CodexResult(content="ok", model="gpt-5.4", exit_code=0, duration_sec=0.1)

    monkeypatch.setattr("llm_router.router.run_codex", _fake)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    resp = await route_and_call(TaskType.CODE, "refactor this function", profile=RoutingProfile.BALANCED,
                                project_root=str(repo))
    assert resp.provider == "codex" and captured["caller"] == "router_chain"


@pytest.mark.asyncio
async def test_a_refused_chain_entry_fails_over_to_the_next_provider_and_is_not_benched(codex_home, temp_db, mock_env,
                                                                                         monkeypatch, tmp_path):
    """The reserve is a failed chain entry, not an outage: Codex is refused, the chain moves on, and Codex is NOT benched."""
    from llm_router import provider_reset
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile, TaskType

    codex_agent._reset_codex_cache_for_tests()  # a fresh worker: nothing cached about the CLI yet
    _meter_at(codex_home, 91.0)
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(tmp_path / "reset.json"))
    monkeypatch.delenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_BLOCK_PROVIDERS", raising=False)
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.97)
    _no_process(monkeypatch)
    try:
        await route_and_call(TaskType.CODE, "refactor this function", profile=RoutingProfile.BALANCED)
    except Exception:  # noqa: BLE001 -- every other provider is absent in the test environment
        pass
    assert provider_reset.get_provider_reset_until("codex") is None
    assert any(r["caller"] == "router_chain" for r in _events())


# ------------------------------------------------------------------------------------------------ a stale reading is a floor


def test_a_stale_reading_at_or_above_the_line_still_refuses_until_its_window_resets(codex_home):
    """Weekly 80% read at 10:00 must not admit llm_codex at 11:00: the weekly percentage only rises until ``resets_at``."""
    read_at = time.time()
    resets = read_at + 3 * 24 * HOUR
    write_rollout(codex_home, [(read_at, rate_limits(weekly=80.0, five=10.0, now=read_at, weekly_resets=resets))])
    one_hour_on = read_at + HOUR
    assert codex_meter.read_meter(one_hour_on) is None  # not trusted ...
    verdict = codex_meter.check("llm_codex", one_hour_on)  # ... but not ignored
    assert not verdict.allowed and verdict.source == "meter_stale" and verdict.weekly_used_pct == 80.0
    assert "holds until" in verdict.reason and verdict.meter_age_s == pytest.approx(HOUR, abs=1)
    assert codex_meter.check("llm_codex", read_at + 3 * HOUR).source == "meter_stale"
    assert codex_meter.check("agent_route", one_hour_on).allowed
    after_reset = codex_meter.check("llm_codex", resets + 1)  # a new window: the old reading says nothing
    assert after_reset.allowed and after_reset.source == "counters"


def test_a_stale_reading_under_the_lines_decides_nothing(codex_home):
    read_at = time.time()
    write_rollout(codex_home, [(read_at, rate_limits(weekly=59.0, five=89.0, now=read_at))])
    verdict = codex_meter.check("llm_codex", read_at + HOUR)
    assert verdict.allowed and verdict.source == "counters"  # the counters, not the old reading, admit it
    _spend_daily(60, read_at + HOUR)
    assert not codex_meter.check("llm_codex", read_at + HOUR).allowed  # ... and they can still refuse


def test_a_stale_five_hour_reading_refuses_until_that_window_resets(codex_home):
    read_at = time.time()
    write_rollout(codex_home, [(read_at, rate_limits(weekly=5.0, five=97.0, now=read_at, five_resets=read_at + 2 * HOUR))])
    assert codex_meter.check("llm_codex", read_at + HOUR).source == "meter_stale"
    assert codex_meter.check("llm_codex", read_at + 2 * HOUR + 1).source == "counters"


def test_the_stale_search_finds_a_reading_in_an_older_day_directory(codex_home):
    read_at = time.time() - 4 * 24 * HOUR
    write_rollout(codex_home, [(read_at, rate_limits(weekly=70.0, five=1.0, now=read_at,
                                                     weekly_resets=read_at + 7 * 24 * HOUR))])
    verdict = codex_meter.check("llm_codex")
    assert not verdict.allowed and verdict.source == "meter_stale"


def test_the_newest_stale_reading_wins_over_an_older_higher_one_of_the_same_window(codex_home):
    """Within a window the newest reading is the largest; a file order that disagrees must not matter."""
    t = time.time() - 2 * HOUR
    write_rollout(codex_home, [(t, rate_limits(weekly=80.0, five=1.0, now=t))], mtime=t + 5)
    write_rollout(codex_home, [(t + 600, rate_limits(weekly=81.0, five=2.0, now=t))], mtime=t + 700)
    assert codex_meter.check("llm_codex").weekly_used_pct == 81.0


# ------------------------------------------------------------------------------------------------ what a reading may be


@pytest.mark.parametrize("bad", [101.0, -1.0, 1e9, "60", None, True])
def test_a_percentage_outside_zero_to_one_hundred_is_not_a_reading(codex_home, bad):
    now = time.time()
    limits = rate_limits(weekly=10.0, five=10.0, now=now)
    limits["secondary"]["used_percent"] = bad
    limits["primary"]["used_percent"] = bad
    write_rollout(codex_home, [(now - 60, limits)])
    assert codex_meter.read_meter(now) is None
    assert codex_meter.check("llm_codex", now).source == "counters"


def test_only_the_codex_bucket_is_the_plus_meter(codex_home):
    now = time.time()
    other = rate_limits(weekly=95.0, five=95.0, now=now)
    other["limit_id"] = "codex_other"
    write_rollout(codex_home, [(now - 60, other)])
    assert codex_meter.read_meter(now) is None
    assert codex_meter.check("llm_codex", now).allowed  # a different bucket's 95% is not the Plus meter


# ------------------------------------------------------------------------------------------------ problems are counted


def _codes() -> dict[str, int]:
    from llm_router import failopen

    failopen.reset_cache()
    return dict(failopen.snapshot().by_code)


@pytest.fixture
def clean_failopen():
    from llm_router import failopen

    failopen.reset_cache()
    failopen.clear()
    yield
    failopen.reset_cache()


def test_a_sessions_directory_that_cannot_be_listed_is_counted_once(codex_home, clean_failopen):
    import os

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can list a mode-000 directory")
    now = _meter_at(codex_home, 10.0)
    sessions = codex_home / "sessions"
    sessions.chmod(0)
    try:
        assert codex_meter.read_meter(now) is None
        codex_meter.read_meter(now)
        codex_meter.check("llm_codex", now)
    finally:
        sessions.chmod(0o755)
    assert _codes() == {"CHZ-FO-CODEX-METER-READ": 1}  # three guarded calls, one row


def test_a_directory_that_does_not_exist_is_normal(codex_home, clean_failopen):
    assert codex_meter.read_meter() is None and codex_meter.read_last_meter() is None
    assert _codes() == {}


def test_a_codex_reading_in_a_format_the_reader_does_not_know_is_counted(codex_home, clean_failopen):
    now = time.time()
    limits = rate_limits(weekly=10.0, five=10.0, now=now)
    del limits["primary"], limits["secondary"]
    limits["windows"] = [{"used": 12}]  # a future shape
    write_rollout(codex_home, [(now - 60, limits)])
    assert codex_meter.read_meter(now) is None
    assert _codes() == {"CHZ-FO-CODEX-METER-FORMAT": 1}
    codex_meter.read_meter(now)
    assert _codes() == {"CHZ-FO-CODEX-METER-FORMAT": 1}  # once per process


def test_a_premium_event_is_not_a_format_problem(codex_home, clean_failopen):
    now = time.time()
    write_rollout(codex_home, [(now - 60, rate_limits(weekly=None, five=None, now=now, limit_id="premium"))])
    assert codex_meter.read_meter(now) is None
    assert _codes() == {}


def test_a_broken_line_is_counted_unless_it_is_the_last_one_codex_may_still_be_writing(codex_home, clean_failopen):
    now = time.time()
    path = write_rollout(codex_home, [(now - 120, rate_limits(weekly=30.0, five=1.0, now=now))])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"timestamp": "' + "x" * 20 + '", "payload": {"rate_limits": {"limit_id": "cod\n')  # cut off mid-write
    assert codex_meter.read_meter(now).weekly_used_pct == 30.0
    assert _codes() == {}  # the last line may be half written
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"type": "event_msg"}\n')  # now the broken line is in the middle of the file
    assert codex_meter.read_meter(now).weekly_used_pct == 30.0  # still read: the reader walks back past it
    assert _codes() == {"CHZ-FO-CODEX-METER-FORMAT": 1}  # but a broken line in the middle of a file is counted


# ------------------------------------------------------------------------------------------------ review items 1, 3, 4


@pytest.mark.asyncio
async def test_a_reserved_codex_entry_is_a_policy_skip_not_a_provider_fault(codex_home, temp_db, mock_env, monkeypatch, tmp_path):
    """Two refusals used to open Codex's breaker for 30 s (tracker.record_failure), write attempt_failed rows, and build the
    context pack first. The chain now asks BEFORE anything is built and goes on to the next entry."""
    from llm_router import router
    from llm_router.health import HealthTracker, get_tracker
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile, TaskType

    codex_agent._reset_codex_cache_for_tests()
    _meter_at(codex_home, 91.0)
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(tmp_path / "reset.json"))
    monkeypatch.delenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_BLOCK_PROVIDERS", raising=False)
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.97)
    _no_process(monkeypatch)
    seen = {"run_codex": 0, "context_pack": 0, "failures": [], "attempt_failed": []}

    async def run_codex_spy(*_a, **_k):
        seen["run_codex"] += 1
        raise AssertionError("run_codex was reached")

    async def context_spy(*_a, **_k):
        seen["context_pack"] += 1
        raise AssertionError("the context pack was built for a refused entry")

    real_emit = router._emit_ledger_attempt

    def emit_spy(response, model, *a, **k):
        if k.get("event_type") == "attempt_failed":
            seen["attempt_failed"].append(model)
        return real_emit(response, model, *a, **k)

    monkeypatch.setattr("llm_router.router.run_codex", run_codex_spy)
    monkeypatch.setattr("llm_router.router._cli_prompt_with_context", context_spy)
    monkeypatch.setattr("llm_router.router._emit_ledger_attempt", emit_spy)
    monkeypatch.setattr(HealthTracker, "record_failure", lambda self, provider: seen["failures"].append(provider))
    try:
        await route_and_call(TaskType.CODE, "refactor this function", profile=RoutingProfile.BALANCED)
    except Exception:  # noqa: BLE001 -- every other provider is absent in the test environment
        pass
    assert seen["run_codex"] == 0 and seen["context_pack"] == 0
    assert "codex" not in seen["failures"]
    # the first failed attempt on the ledger is the next provider's: the two refused Codex entries wrote none (the emergency
    # chain's own attempts at codex/* models, which litellm cannot run, come after the primary chain is exhausted)
    assert seen["attempt_failed"] and not seen["attempt_failed"][0].startswith("codex/")
    assert get_tracker().is_healthy("codex")
    assert {r["caller"] for r in _events()} == {"router_chain"} and len(_events()) >= 1


def test_a_chain_guard_that_fails_refuses_rather_than_opens(monkeypatch):
    from llm_router import router

    monkeypatch.setattr(codex_meter, "refusal", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("bug")))
    assert "reserve guard failed" in router._codex_reserve_refusal()


def test_a_call_that_passes_a_sandbox_and_no_caller_is_agent_routes(codex_home, monkeypatch):
    """Hook 27 (before D-373) passes the D-357 sandbox and no caller. A package from this change under that hook must not
    refuse every delegation as 'unspecified' once the weekly meter is at the line."""
    _meter_at(codex_home, 99.0, five=99.0)
    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: None)
    for sandbox in ("read-only", "workspace-write"):
        assert _run(model="gpt-6-astra", sandbox=sandbox).reason_code == "binary_missing"
    assert _events() == []
    assert _run(model="gpt-6-astra").reason_code == "meter_reserved"  # no sandbox, no caller: a pure-LLM caller
    assert _run(model="gpt-6-astra", sandbox="read-only", caller="evaluator").reason_code == "meter_reserved"  # a name wins


def _milestone():
    from llm_router.agentic.ledger import AcceptanceResult, Milestone

    return Milestone("M1", "do it", lambda _a: AcceptanceResult(True))


def test_the_agentic_codex_tier_is_guarded_like_every_other_caller(codex_home):
    """tools/agentic.py tier 1 (llm_delegate, llm_act) starts ``codex exec`` itself, not through run_codex."""
    from llm_router.agentic.adapters import CodexAdapter, ProcResult

    calls = []

    def fake_runner(argv, input_text):
        calls.append(argv)
        return ProcResult(0, "ran\n", "")

    _meter_at(codex_home, 81.0)
    res = CodexAdapter(tier=1, runner=fake_runner, binary="codex-x").run(_milestone(), [], 5.0)
    assert calls == []  # nothing was started
    assert res.artifacts["returncode"] == 127 and "reserved for sub-agent delegation" in res.artifacts["stderr"]
    assert res.confidence == 0.3  # a failed tier: the escalation moves on
    assert [(r["kind"], r["caller"]) for r in _events()] == [("refused", "agentic_codex")]


def test_the_agentic_codex_tier_runs_when_the_meter_has_room(codex_home):
    from llm_router.agentic.adapters import CodexAdapter, ProcResult

    calls = []
    _meter_at(codex_home, 12.0)
    res = CodexAdapter(tier=1, runner=lambda argv, i: calls.append(argv) or ProcResult(0, "ran\n", ""),
                       binary="codex-x").run(_milestone(), [], 5.0)
    assert calls and calls[0][:2] == ["codex-x", "exec"] and res.confidence == 1.0
    assert _events() == []


def test_an_agentic_guard_that_fails_refuses_rather_than_opens(monkeypatch):
    from llm_router.agentic.adapters import CodexAdapter

    monkeypatch.setattr(codex_meter, "refusal", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("bug")))
    assert "reserve guard failed" in CodexAdapter._reserve_refusal()
