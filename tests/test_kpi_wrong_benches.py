"""KPI G4: was a provider bench wrong?

`kpi` could only count the providers benched right now. `provider_reset.json`
holds "blocked until T" and nothing else, so a bench that had lapsed or been
cleared left no trace. Every bench, owner unban and success-while-benched is now
logged, and a bench is WRONG when

  (a) the owner cleared it with `llm-router provider unban` before it lapsed, or
  (b) a call to that provider succeeded before its reset time.

These tests pin the judgement on explicit timestamps (fake clock: every event and
the report's `now` are numbers the test chose), the real code paths that write
the log (`record_provider_reset`, `note_provider_error`, `clear_provider_reset`,
`HealthTracker.record_success`, `llm-router provider unban`, the agent-route
Codex path), and the report -- 0 benches reads "not measurable", never 0%.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from llm_router import provider_bench_log as bl
from llm_router import provider_reset as pr
from llm_router.commands import kpi

NOW = 1_790_000_000.0
HOUR = 3600.0
DAY = 86400.0
MIN_N = kpi.MIN_N
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    # compute_scorecard reads transcripts for other KPIs: never the operator's.
    (tmp_path / "claude-projects").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude-projects"))
    bl._success_logged.clear()
    yield
    bl._success_logged.clear()


def _judge(days=7, now=NOW):
    return bl.judge(since=now - days * DAY, now=now)


def _rows():
    return [json.loads(x) for x in bl.store_path().read_text().splitlines()]


# ── the judgement ────────────────────────────────────────────────────────────


def test_no_events_is_an_empty_judgement_not_a_clean_one():
    j = _judge()
    assert (j.benches, j.wrong, j.active) == (0, 0, 0)


@pytest.mark.parametrize("until", ["soon", None, True, object(), float("nan"), float("inf"), float("-inf")])
def test_log_bench_never_raises_on_a_non_numeric_until_and_writes_no_row(until):
    from llm_router import failopen

    failopen.reset_unpersisted()
    bl.log_bench("codex", "cli", until, NOW)          # must not raise
    # Its own code, distinct from a disk-write failure (CHZ-FO-BENCH-LOG-WRITE).
    failopen.reset_cache()
    by_code = failopen.snapshot().by_code
    assert by_code.get("CHZ-FO-BENCH-LOG-ARG") == 1 and "CHZ-FO-BENCH-LOG-WRITE" not in by_code
    failopen.reset_unpersisted()
    assert not bl.store_path().exists() or _rows() == []
    assert _judge().benches == 0


def test_a_bench_nobody_contradicted_and_that_lapsed_is_not_wrong():
    bl.log_bench("codex", "cli", NOW - HOUR, NOW - 3 * HOUR)
    j = _judge()
    assert (j.benches, j.wrong, j.active) == (1, 0, 0)


def test_a_bench_still_in_force_is_active_not_right():
    bl.log_bench("codex", "cli", NOW + HOUR, NOW - HOUR)
    j = _judge()
    assert (j.benches, j.wrong, j.active) == (1, 0, 1)


def test_owner_unban_before_the_reset_time_makes_it_wrong():
    bl.log_bench("codex", "cli", NOW + HOUR, NOW - 3 * HOUR)
    bl.log_unban("codex", NOW + HOUR, NOW - HOUR)
    j = _judge()
    assert (j.benches, j.wrong, j.wrong_by_unban, j.wrong_by_success, j.active) == (1, 1, 1, 0, 0)


def test_owner_unban_after_the_bench_had_lapsed_is_not_wrong():
    bl.log_bench("codex", "cli", NOW - 2 * HOUR, NOW - 3 * HOUR)
    bl.log_unban("codex", NOW - 2 * HOUR, NOW - HOUR)
    assert _judge().wrong == 0


def test_a_success_before_the_reset_time_makes_it_wrong():
    bl.log_bench("codex", "cli", NOW + HOUR, NOW - 3 * HOUR)
    bl.log_success("codex", NOW + HOUR, NOW - HOUR)
    j = _judge()
    assert (j.wrong, j.wrong_by_success, j.wrong_by_unban) == (1, 1, 0)


def test_a_success_after_the_reset_time_is_the_provider_working_again_not_a_wrong_bench():
    bl.log_bench("codex", "cli", NOW - 2 * HOUR, NOW - 3 * HOUR)
    bl.log_success("codex", NOW - 2 * HOUR, NOW - HOUR)
    assert _judge().wrong == 0


def test_a_bench_that_is_both_unbanned_and_succeeded_counts_once():
    bl.log_bench("codex", "cli", NOW + HOUR, NOW - 3 * HOUR)
    bl.log_success("codex", NOW + HOUR, NOW - 2 * HOUR)
    bl.log_unban("codex", NOW + HOUR, NOW - HOUR)
    j = _judge()
    assert (j.benches, j.wrong, j.wrong_by_unban, j.wrong_by_success) == (1, 1, 1, 1)


def test_an_event_at_the_very_instant_of_the_bench_is_not_after_it():
    bl.log_bench("codex", "cli", NOW + HOUR, NOW - HOUR)
    bl.log_success("codex", NOW + HOUR, NOW - HOUR)
    bl.log_unban("codex", NOW + HOUR, NOW - HOUR)
    assert _judge().wrong == 0


def test_an_event_at_the_reset_instant_is_not_before_it():
    bl.log_bench("codex", "cli", NOW - HOUR, NOW - 3 * HOUR)
    bl.log_success("codex", NOW - HOUR, NOW - HOUR)
    assert _judge().wrong == 0


def test_a_later_bench_replaces_the_earlier_one_and_takes_the_blame_for_what_follows():
    """provider_reset.json holds one entry per provider, so B2 REPLACED B1. A
    success after B2 is B2's; one between B1 and B2 is B1's. Neither is counted
    against both."""
    bl.log_bench("codex", "text", NOW + 5 * HOUR, NOW - 5 * HOUR)       # B1
    bl.log_bench("codex", "cli", NOW + 5 * HOUR, NOW - 2 * HOUR)        # B2 replaces it
    bl.log_success("codex", NOW + 5 * HOUR, NOW - HOUR)                 # after B2
    j = _judge()
    assert (j.benches, j.wrong) == (2, 1)
    # ... and a success BETWEEN them is B1's alone.
    bl.log_success("codex", NOW + 4 * HOUR, NOW - 3 * HOUR)
    j = _judge()
    assert (j.benches, j.wrong) == (2, 2)


def test_providers_are_judged_independently():
    bl.log_bench("codex", "cli", NOW + HOUR, NOW - 3 * HOUR)
    bl.log_bench("gemini", "header", NOW + HOUR, NOW - 3 * HOUR)
    bl.log_success("gemini", NOW + HOUR, NOW - HOUR)
    j = _judge()
    assert (j.benches, j.wrong, j.active) == (2, 1, 1)


def test_only_benches_inside_the_window_are_counted_as_benches():
    bl.log_bench("old", "cli", NOW - 9 * DAY + HOUR, NOW - 9 * DAY)
    bl.log_bench("new", "cli", NOW + HOUR, NOW - HOUR)
    j = _judge(days=7)
    assert j.benches == 1
    assert _judge(days=10).benches == 2


def test_events_after_now_are_not_known_yet():
    bl.log_bench("codex", "cli", NOW + 5 * HOUR, NOW - HOUR)
    bl.log_unban("codex", NOW + 5 * HOUR, NOW + 1)
    assert _judge().wrong == 0


def test_benches_are_counted_by_trigger_and_an_unknown_trigger_is_not_dropped():
    bl.log_bench("a", "header", NOW + HOUR, NOW - HOUR)
    bl.log_bench("b", "cli", NOW + HOUR, NOW - HOUR)
    bl.log_bench("c", "cli", NOW + HOUR, NOW - HOUR)
    bl.log_bench("d", "weird", NOW + HOUR, NOW - HOUR)
    assert _judge().by_trigger == {"header": 1, "cli": 2, "unknown": 1}


def test_read_events_skips_junk_and_keeps_order():
    path = bl.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        json.dumps({"kind": "bench", "provider": "a", "trigger": "cli", "until": NOW + 1, "ts": NOW - 5}),
        "{torn",
        json.dumps({"kind": "bench", "provider": "a", "trigger": "cli", "ts": NOW - 4}),        # no until
        json.dumps({"kind": "nope", "provider": "a", "ts": NOW - 3}),
        json.dumps({"kind": "unban", "provider": 7, "ts": NOW - 2}),
        json.dumps({"kind": "unban", "provider": "a", "ts": True}),
        json.dumps({"kind": "unban", "provider": "a", "ts": NOW - 6}),
    ]) + "\n")
    assert [(e["kind"], e["ts"]) for e in bl.read_events()] == [("unban", NOW - 6), ("bench", NOW - 5)]


def test_a_write_failure_is_counted_not_raised(tmp_path, monkeypatch):
    from llm_router import failopen

    failopen.reset_unpersisted()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(blocker / "state"))
    bl.log_bench("codex", "cli", NOW, NOW)                    # must not raise
    assert failopen.snapshot().unpersisted_by_code == {"CHZ-FO-BENCH-LOG-WRITE": 1}
    failopen.reset_unpersisted()


# ── the code paths that write the log ───────────────────────────────────────


def test_a_persisted_bench_is_logged_with_provider_trigger_reset_and_ts():
    assert pr.record_provider_reset("codex", NOW + 2 * HOUR, "usage limit", now=NOW, source="cli")
    assert _rows() == [{"kind": "bench", "provider": "codex", "trigger": "cli",
                        "until": NOW + 2 * HOUR, "ts": NOW}]


def test_the_logged_reset_is_the_persisted_capped_one():
    pr.record_provider_reset("codex", NOW + 30 * DAY, "x", now=NOW, source="header")
    (row,) = _rows()
    assert row["until"] == NOW + pr.MAX_SKIP_SECONDS == pr.all_provider_resets(NOW)["codex"]


def test_a_blip_too_short_to_persist_is_not_a_bench():
    assert pr.record_provider_reset("codex", NOW + 10, "x", now=NOW, source="cli") is False
    assert not bl.store_path().exists()


def test_a_bench_whose_state_write_failed_is_not_logged(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(blocker / "provider_reset.json"))
    assert pr.record_provider_reset("codex", NOW + HOUR, "x", now=NOW, source="cli") is False
    assert not bl.store_path().exists()


def test_an_unlabelled_bench_is_logged_as_unknown_not_guessed():
    pr.record_provider_reset("codex", NOW + HOUR, "x", now=NOW)
    assert _rows()[0]["trigger"] == "unknown"


def test_trigger_is_header_when_the_reset_came_from_a_response_header():
    assert pr.note_provider_error("openai", RuntimeError("429"), {"Retry-After": "7200"}) is not None
    assert _rows()[-1]["trigger"] == "header"


def test_trigger_is_text_when_the_reset_came_from_an_api_error_message():
    err = RuntimeError("You have hit the usage limit. Please try again in 3 hours.")
    assert pr.note_provider_error("openai", err) is not None
    assert _rows()[-1]["trigger"] == "text"


def test_trigger_is_cli_when_the_caller_held_the_full_cli_output():
    out = "ERROR: usage limit reached. Please try again in 3 hours."
    assert pr.note_provider_error("codex", RuntimeError("short"), text=out) is not None
    assert _rows()[-1]["trigger"] == "cli"


def test_a_provider_that_is_never_benched_leaves_no_bench_row():
    """Anthropic is benched from headers only: prose must not bench it, and so
    must not appear in the log as a bench."""
    err = RuntimeError("You have hit the usage limit. Please try again in 3 hours.")
    assert pr.note_provider_error("anthropic", err) is None
    assert not bl.store_path().exists()


def test_unban_logs_what_each_cleared_entry_held():
    pr.record_provider_reset("codex", NOW + 2 * HOUR, "x", now=NOW - HOUR, source="cli")
    pr.record_provider_reset("gemini", NOW + 3 * HOUR, "x", now=NOW - HOUR, source="header")
    assert sorted(pr.clear_provider_reset(None, now=NOW)) == ["codex", "gemini"]
    unbans = [r for r in _rows() if r["kind"] == "unban"]
    assert sorted((r["provider"], r["until"], r["ts"]) for r in unbans) == [
        ("codex", NOW + 2 * HOUR, NOW), ("gemini", NOW + 3 * HOUR, NOW)]
    j = _judge()
    assert (j.benches, j.wrong, j.wrong_by_unban) == (2, 2, 2)


def test_unbanning_something_that_is_not_benched_logs_nothing():
    pr.record_provider_reset("codex", NOW + HOUR, "x", now=NOW - HOUR, source="cli")
    before = _rows()
    assert pr.clear_provider_reset("gemini", now=NOW) == []
    assert _rows() == before


def test_unbanning_an_entry_that_had_already_lapsed_is_logged_but_not_wrong():
    pr.record_provider_reset("codex", NOW - HOUR, "x", now=NOW - 3 * HOUR, source="cli")
    assert pr.clear_provider_reset("codex", now=NOW) == ["codex"]
    assert _rows()[-1] == {"kind": "unban", "provider": "codex", "until": NOW - HOUR, "ts": NOW}
    assert _judge().wrong == 0


def test_a_success_on_a_provider_that_is_not_benched_writes_nothing():
    pr.note_provider_success("codex", now=NOW)
    assert not bl.store_path().exists()


def test_a_success_while_benched_is_logged_once_per_bench():
    pr.record_provider_reset("codex", NOW + HOUR, "x", now=NOW - HOUR, source="cli")
    for _ in range(5):
        pr.note_provider_success("codex", now=NOW)
    assert [r["kind"] for r in _rows()] == ["bench", "success"]
    assert _rows()[-1] == {"kind": "success", "provider": "codex", "until": NOW + HOUR, "ts": NOW}


def test_a_success_after_the_bench_lapsed_writes_nothing():
    pr.record_provider_reset("codex", NOW - HOUR, "x", now=NOW - 3 * HOUR, source="cli")
    pr.note_provider_success("codex", now=NOW)
    assert [r["kind"] for r in _rows()] == ["bench"]


def test_the_router_success_path_reports_a_success_to_the_bench_log():
    """HealthTracker.record_success is what router.py calls after every good
    call; a benched provider that succeeds there is the (b) signal."""
    from llm_router.health import HealthTracker

    now = __import__("time").time()
    pr.record_provider_reset("codex", now + HOUR, "x", now=now - 10, source="cli")
    HealthTracker().record_success("codex")
    assert [r["kind"] for r in _rows()] == ["bench", "success"]


def test_a_broken_success_note_never_breaks_the_call(monkeypatch):
    from llm_router import failopen

    failopen.reset_unpersisted()
    monkeypatch.setattr(pr, "get_provider_reset_until", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    pr.note_provider_success("codex", now=NOW)                  # must not raise
    assert failopen.snapshot().unpersisted_by_code == {}        # the store was writable: it is on disk
    assert failopen.snapshot().by_code == {"CHZ-FO-PROVIDER-RESET-SUCCESS-NOTE": 1}
    failopen.reset_unpersisted()


def test_the_unban_command_logs_an_owner_override_and_kpi_reads_it(capsys):
    from llm_router.commands.provider import cmd_provider

    now = __import__("time").time()
    pr.record_provider_reset("codex", now + 2 * HOUR, "x", now=now - HOUR, source="cli")
    cmd_provider(["unban", "codex"])
    assert "Cleared: codex" in capsys.readouterr().out
    g4 = kpi._g4_wrongly_benched(7, now + 5)
    assert (g4["benches"], g4["wrong"], g4["wrong_by_unban"]) == (1, 1, 1)


def _load_agent_route():
    spec = importlib.util.spec_from_file_location("agent_route_hook_g4",
                                                  REPO / "src/llm_router/hooks/agent-route.py")
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def test_the_codex_agent_path_logs_its_fixed_duration_bench_as_cli():
    hook = _load_agent_route()
    # Quota wording with no parseable reset: the hook benches for a fixed hour.
    until = hook._bench_after_quota_failure("codex", "ERROR: You have exceeded your quota.")
    assert until is not None
    (row,) = _rows()
    assert (row["kind"], row["provider"], row["trigger"]) == ("bench", "codex", "cli")


def test_the_codex_agent_path_logs_a_parsed_bench_as_cli():
    hook = _load_agent_route()
    assert hook._bench_after_quota_failure("codex", "usage limit reached, try again in 2 hours") is not None
    assert _rows()[-1]["trigger"] == "cli"


def test_a_codex_success_while_the_account_is_benched_is_the_b_signal():
    hook = _load_agent_route()
    now = __import__("time").time()
    pr.record_provider_reset("codex", now + HOUR, "x", now=now - 10, source="cli")
    pr.record_provider_reset("codex:gpt-5.5", now + HOUR, "x", now=now - 10, source="cli")
    hook._note_codex_success("gpt-5.5")
    assert sorted(r["provider"] for r in _rows() if r["kind"] == "success") == ["codex", "codex:gpt-5.5"]


# ── the report ───────────────────────────────────────────────────────────────


def _g4(days=7, now=NOW):
    return kpi._g4_wrongly_benched(days, now)


def test_zero_benches_is_not_measurable_not_zero_percent():
    g4 = _g4()
    assert g4["value"] == ("not measurable: no provider bench recorded in window (0 benches: "
                           "'none were wrong' cannot be told from 'none happened')")
    assert g4["measurable"] is False and g4["n"] is None and g4["benches"] == 0
    assert "%" not in g4["value"] and "0.0" not in g4["value"]


def test_benches_only_outside_the_window_are_not_measurable():
    bl.log_bench("codex", "cli", NOW - 8 * DAY + HOUR, NOW - 8 * DAY)
    assert _g4(days=7)["value"].startswith("not measurable: ")
    assert _g4(days=9)["benches"] == 1


def test_the_point_in_time_reading_survives_as_a_detail_line():
    pr.record_provider_reset("codex", NOW + HOUR, "x", now=NOW - 10, source="cli")
    lines = _g4()["lines"]
    assert lines[0].startswith("benched right now: codex until ")
    assert lines[0].endswith("(point-in-time)")
    assert _g4(now=NOW + 2 * HOUR)["lines"][0] == "benched right now: none (point-in-time)"


def test_a_handful_of_benches_gives_counts_not_a_rate():
    for i in range(3):
        bl.log_bench(f"p{i}", "cli", NOW + HOUR, NOW - HOUR)
    bl.log_unban("p0", NOW + HOUR, NOW - 60)
    g4 = _g4()
    assert g4["value"] == "too few to tell (n=3 benches); 1 shown wrong so far (target 0)"
    assert g4["measurable"] is False and g4["n"] == 3
    assert (g4["benches"], g4["wrong"], g4["active"]) == (3, 1, 2)
    assert "2 bench(es) still in force and not shown wrong -- they can still turn out wrong, " \
           "so the wrong count is a floor" in g4["lines"]


def _fifty(wrong_ids=()):
    for i in range(MIN_N):
        bl.log_bench(f"p{i}", ["header", "cli", "text"][i % 3], NOW + HOUR, NOW - HOUR)
    for i in wrong_ids:
        bl.log_unban(f"p{i}", NOW + HOUR, NOW - 60)


def test_at_the_minimum_n_the_rate_per_100_benches_is_exact():
    _fifty(wrong_ids=(0,))
    g4 = _g4()
    assert g4["value"] == f"2.0 wrong benches per 100 (1 / {MIN_N} benches, 7d; OVER the =0 target)"
    assert g4["measurable"] is True and g4["n"] == MIN_N and g4["rate_per_100"] == 2.0
    assert g4["by_trigger"] == {"header": 17, "cli": 17, "text": 16}
    assert "benches by trigger: cli 17, header 17, text 16" in g4["lines"]
    assert "wrong: 1 cleared by the owner (provider unban), 0 succeeded before the reset time " \
           "(a bench can be both)" in g4["lines"]


def test_no_wrong_bench_among_many_is_a_measured_rate_that_does_not_claim_the_target_is_met():
    _fifty()
    g4 = _g4()
    assert g4["value"] == (f"0.0 wrong benches per 100 (0 / {MIN_N} benches, 7d; "
                           "none shown wrong yet (target 0))")
    assert g4["measurable"] is True and g4["active"] == MIN_N


def test_the_scorecard_g4_follows_the_fake_now():
    bl.log_bench("codex", "cli", NOW + HOUR, NOW - HOUR)
    card = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["G4"]
    assert card["benches"] == 1
    later = kpi.compute_scorecard(days=7, now=NOW + 8 * DAY)["kpis"]["G4"]
    assert later["value"].startswith("not measurable: ")


def test_health_judges_g4_by_its_newest_bench_and_says_benches_are_rare():
    _fifty()                                                  # every bench is 1h old at NOW
    h = kpi.compute_health(kpi.compute_scorecard(days=7, now=NOW), now=NOW)["kpis"]["G4"]
    assert (h["state"], h["n"]) == ("measured", MIN_N)
    assert h["reason"].startswith("newest data point 1.0h old (benches are rare events: ")
    later = NOW + 3 * DAY
    h = kpi.compute_health(kpi.compute_scorecard(days=7, now=later), now=later)["kpis"]["G4"]
    assert h["state"] == "stale"


def test_judge_drops_a_non_finite_until_row_read_from_disk():
    bl.store_path().parent.mkdir(parents=True, exist_ok=True)
    bl.store_path().write_text(
        '{"kind":"bench","provider":"codex","trigger":"cli","until":NaN,"ts":%s}\n' % (NOW - HOUR))
    assert _judge().benches == 0
