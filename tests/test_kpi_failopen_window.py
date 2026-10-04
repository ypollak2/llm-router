"""KPI G2: fail-open events per 100 calls, over a window.

`fail_open.jsonl` rows carried no time, so 1,925 all-time events could not become
a rate. Every row now carries `ts`; these tests pin that, the windowed reader,
and the report: events per 100 calls overall and per code, the top five codes,
a labelled all-time line for the old untimestamped rows, and "not measurable" /
"too few to tell" for every case the data cannot support.

Fake clock throughout: `failopen._now` stamps the rows, `now=` places the report.
"""

from __future__ import annotations

import json
import time

import pytest

from llm_router import failopen
from llm_router import hook_latency as hl
from llm_router.commands import kpi

NOW = 1_790_000_000.0
DAY = 86400.0
MIN_N = kpi.MIN_N


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    # compute_scorecard reads transcripts for other KPIs: never the operator's.
    (tmp_path / "claude-projects").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude-projects"))
    monkeypatch.setattr(failopen, "_now", lambda: NOW)
    failopen.reset_unpersisted()
    failopen.reset_cache()
    yield
    failopen.reset_unpersisted()
    failopen.reset_cache()


def _event(code, ts, **kw):
    """One fail-open event at fake time `ts`."""
    real = failopen._now
    failopen._now = lambda: ts
    try:
        failopen.record(code, **kw)
    finally:
        failopen._now = real


def _legacy(n, code="CHZ-FO-LEGACY"):
    path = failopen.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for _ in range(n):
            fh.write(json.dumps({"c": code, "e": "RuntimeError"}) + "\n")
    failopen.reset_cache()


def _hook_calls(n, first_ts):
    """n hook invocations, the oldest at `first_ts`, spread 1 s apart."""
    for i in range(n):
        assert hl.record("enforce-route", "PreToolUse", 10.0, now=first_ts + i)


def _proxy(n, ts):
    return [{"ts": ts + i, "session_kind": "organic"} for i in range(n)]


def _g2(days=7, proxy=()):
    return kpi._g2_silent_failures(days, NOW, list(proxy))


# ── the timestamp itself ─────────────────────────────────────────────────────


def test_every_new_row_carries_ts_and_keeps_its_old_fields(monkeypatch):
    monkeypatch.setattr(failopen, "_now", lambda: 1_790_000_000.12345)
    failopen.record("CHZ-FO-X", RuntimeError("boom"), detail="where")
    (row,) = [json.loads(x) for x in failopen.store_path().read_text().splitlines()]
    assert row == {"c": "CHZ-FO-X", "ts": 1_790_000_000.123, "e": "RuntimeError", "d": "where"}


def test_a_timestamped_row_is_still_one_all_time_event_to_snapshot():
    _legacy(2)
    _event("CHZ-FO-X", NOW - DAY)
    assert failopen.snapshot().by_code == {"CHZ-FO-LEGACY": 2, "CHZ-FO-X": 1}
    assert failopen.snapshot().total == 3


# ── the windowed reader ──────────────────────────────────────────────────────


def test_windowed_separates_old_untimestamped_rows_from_timestamped_ones():
    _legacy(3)
    _event("A", NOW - 10)
    _event("A", NOW - 20)
    _event("B", NOW - 30)
    w = failopen.windowed(since=NOW - 25, until=NOW)
    assert w.by_code == {"A": 2}                  # B is before the window
    assert w.untimestamped == 3                   # real events, time unknown: not in any window


def test_windowed_bounds_are_inclusive_at_both_ends():
    _event("A", NOW - 100)
    _event("A", NOW - 50)
    _event("A", NOW)
    _event("A", NOW + 1)
    assert failopen.windowed(since=NOW - 100, until=NOW).by_code == {"A": 3}
    assert failopen.windowed(since=NOW - 99, until=NOW - 1).by_code == {"A": 1}


def test_windowed_counts_untimestamped_rows_as_unplaceable_not_as_in_window():
    _legacy(5)
    _event("A", NOW - 10)
    w = failopen.windowed(since=NOW - 1000, until=NOW)
    assert (w.in_window, w.untimestamped, w.first_ts, w.readable) == (1, 5, NOW - 10, True)


def test_first_ts_is_the_earliest_timestamp_even_outside_the_window():
    _event("A", NOW - 500)
    _event("A", NOW - 10)
    assert failopen.windowed(since=NOW - 100, until=NOW).first_ts == NOW - 500


def test_a_boolean_or_string_ts_is_not_a_timestamp():
    path = failopen.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"c":"A","ts":true}\n{"c":"A","ts":"1790000000"}\n{"c":"A","ts":null}\n')
    w = failopen.windowed()
    assert (w.in_window, w.untimestamped, w.first_ts) == (0, 3, None)


def test_windowed_with_no_store_is_empty_and_readable():
    w = failopen.windowed(since=0, until=NOW)
    assert (w.by_code, w.untimestamped, w.first_ts, w.readable) == ({}, 0, None, True)


def test_windowed_with_only_garbage_is_unreadable_not_empty():
    path = failopen.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json\nnor this\n")
    assert failopen.windowed().readable is False


# ── the report: empty and thin data ──────────────────────────────────────────


def test_nothing_recorded_at_all_is_not_measurable():
    g2 = _g2()
    assert g2["value"] == ("not measurable: no timestamped fail-open event and no recorded hook "
                           "call exist yet, so there is no window to take a rate over")
    assert g2["measurable"] is False and g2["n"] is None


def test_only_old_untimestamped_events_stay_in_the_all_time_line():
    _legacy(1925)
    g2 = _g2()
    assert g2["value"].startswith("not measurable: ")
    assert g2["lines"] == ["all-time: 1925 fail-open event(s) recorded, 1925 of them from before "
                           "per-event timestamps (cannot be placed in any window)"]


def test_events_but_no_calls_in_the_window_is_not_measurable():
    _event("A", NOW - 100)
    g2 = _g2()
    assert g2["value"].startswith("not measurable: no hook invocation or proxy call recorded since ")
    assert g2["events"] == 1 and g2["calls"] == 0


def test_under_the_minimum_calls_says_too_few_and_still_shows_the_raw_count():
    _event("A", NOW - 100)
    _event("A", NOW - 90)
    _hook_calls(MIN_N - 1, NOW - 3600)
    g2 = _g2()
    assert g2["value"] == f"too few to tell (n={MIN_N - 1} calls; 2 fail-open event(s) so far)"
    assert g2["measurable"] is False and g2["n"] == MIN_N - 1


def test_a_denominator_but_no_timestamped_event_ever_is_not_a_zero():
    """Calls exist, no event has a ts: 0 cannot be told from 'timestamps are not
    being written', and 0.00 would be the favourable reading of an unknown."""
    _legacy(40)
    _hook_calls(MIN_N + 10, NOW - 3600)
    g2 = _g2()
    assert g2["value"] == ("not measurable: no timestamped fail-open event has ever been recorded "
                           "here, so 0 cannot be told from 'timestamps are not being written'")


def test_zero_in_window_is_a_real_zero_once_timestamps_are_known_to_work():
    _event("A", NOW - 30 * DAY)                   # proves timestamps are written; outside the window
    _hook_calls(100, NOW - 3600)
    g2 = _g2(days=7)
    assert g2["value"] == "0.00 per 100 calls (0 events / 100 calls, 7.0d window)"
    assert g2["measurable"] is True and g2["n"] == 100 and g2["rate_per_100"] == 0.0


def test_an_unreadable_store_is_not_measurable():
    path = failopen.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json\n")
    failopen.reset_cache()
    assert _g2()["value"] == "not measurable: fail-open store is present but unreadable"


# ── the report: exact rates ──────────────────────────────────────────────────


def test_events_per_100_calls_exact_with_hook_and_proxy_calls_pooled():
    _hook_calls(60, NOW - 2 * DAY)                  # first evidence: exactly 2 days back
    proxy = _proxy(40, NOW - DAY)
    for ts in (NOW - DAY, NOW - 3600, NOW - 60, NOW - 5):
        _event("CHZ-FO-A", ts)
    g2 = _g2(days=7, proxy=proxy)
    assert g2["value"] == "4.00 per 100 calls (4 events / 100 calls, 2.0d window)"
    assert (g2["events"], g2["calls"], g2["hook_calls"], g2["proxy_calls"]) == (4, 100, 60, 40)
    assert g2["measurable"] is True and g2["n"] == 100 and g2["rate_per_100"] == 4.0


def test_per_code_rates_and_only_the_top_five_are_listed():
    _hook_calls(100, NOW - 3600)
    counts = {"CHZ-FO-G": 1, "CHZ-FO-F": 2, "CHZ-FO-E": 3, "CHZ-FO-D": 4,
              "CHZ-FO-C": 6, "CHZ-FO-B": 8, "CHZ-FO-A": 10}
    for code, n in counts.items():
        for i in range(n):
            _event(code, NOW - 100 - i)
    g2 = _g2()
    assert g2["rate_per_100"] == 34.0 and g2["events"] == 34
    assert [t["code"] for t in g2["top_codes"]] == ["CHZ-FO-A", "CHZ-FO-B", "CHZ-FO-C", "CHZ-FO-D", "CHZ-FO-E"]
    assert g2["top_codes"][0] == {"code": "CHZ-FO-A", "events": 10, "per_100_calls": 10.0}
    assert len(g2["by_code"]) == 7                              # overall by-code keeps every code
    assert g2["by_code_per_100"]["CHZ-FO-G"] == 1.0
    listed = [ln for ln in g2["lines"] if ln.startswith("  CHZ-FO-")]
    assert listed == ["  CHZ-FO-A: 10.00 per 100 calls (10)", "  CHZ-FO-B: 8.00 per 100 calls (8)",
                      "  CHZ-FO-C: 6.00 per 100 calls (6)", "  CHZ-FO-D: 4.00 per 100 calls (4)",
                      "  CHZ-FO-E: 3.00 per 100 calls (3)"]


def test_equal_counts_are_ordered_by_code_so_the_report_is_stable():
    _hook_calls(100, NOW - 3600)
    for code in ("CHZ-FO-B", "CHZ-FO-A", "CHZ-FO-C"):
        _event(code, NOW - 100)
    assert [t["code"] for t in _g2()["top_codes"]] == ["CHZ-FO-A", "CHZ-FO-B", "CHZ-FO-C"]


def test_old_untimestamped_events_never_enter_the_rate():
    _legacy(300)
    _hook_calls(100, NOW - 3600)
    _event("CHZ-FO-A", NOW - 100)
    _event("CHZ-FO-A", NOW - 99)
    g2 = _g2()
    assert g2["events"] == 2 and g2["rate_per_100"] == 2.0
    assert g2["all_time_total"] == 302 and g2["untimestamped"] == 300
    assert ("all-time: 302 fail-open event(s) recorded, 300 of them from before per-event "
            "timestamps (cannot be placed in any window)") in g2["lines"]


# ── the window ───────────────────────────────────────────────────────────────


def test_the_window_starts_at_the_first_timestamped_evidence_not_before():
    """Proxy calls from before timestamps existed are in the ledger but their
    failures are not: counting them would dilute the rate."""
    _hook_calls(60, NOW - 2 * DAY)
    stale = _proxy(500, NOW - 5 * DAY)             # older than any timestamped evidence
    recent = _proxy(40, NOW - DAY)
    _event("CHZ-FO-A", NOW - 3600)
    g2 = _g2(days=7, proxy=stale + recent)
    assert g2["window_start"] == NOW - 2 * DAY
    assert g2["proxy_calls"] == 40 and g2["calls"] == 100
    assert any(ln.startswith("window starts at the first timestamped evidence, ") and
               ln.endswith("(requested 7d back)") for ln in g2["lines"])


def test_a_short_requested_window_is_not_stretched_back_to_the_evidence():
    _hook_calls(100, NOW - 5 * DAY)
    _event("CHZ-FO-A", NOW - 3 * DAY)              # outside a 1-day window
    _event("CHZ-FO-A", NOW - 3600)
    g2 = _g2(days=1)
    assert g2["window_start"] == NOW - DAY
    assert g2["events"] == 1
    assert not any("window starts at the first timestamped evidence" in ln for ln in g2["lines"])


def test_events_and_calls_after_now_are_not_counted():
    _hook_calls(100, NOW - 3600)
    _event("CHZ-FO-A", NOW + 10)
    g2 = _g2()
    assert g2["events"] == 0
    assert g2["hook_calls"] == 100                 # the future event proved timestamps work; zero is real


def test_fail_open_rows_name_no_session_so_every_session_kind_counts_as_a_call():
    _hook_calls(10, NOW - 3600)
    mixed = [{"ts": NOW - 100 - i, "session_kind": k}
             for i, k in enumerate(["organic", "research", None, "harness", "headless"] * 8)]
    _event("CHZ-FO-A", NOW - 50)
    g2 = _g2(proxy=mixed)
    assert g2["proxy_calls"] == 40 and g2["calls"] == 50


def test_proxy_rows_without_a_usable_timestamp_are_not_calls():
    _hook_calls(MIN_N, NOW - 3600)
    _event("CHZ-FO-A", NOW - 50)
    junk = [{"session_kind": "organic"}, {"ts": None}, {"ts": True}, {"ts": "x"}]
    assert _g2(proxy=junk)["proxy_calls"] == 0


# ── end to end through the scorecard ─────────────────────────────────────────


def test_scorecard_g2_with_a_fake_now(tmp_path, monkeypatch):
    from llm_router.proxy import ledger as pl

    rows = _proxy(40, NOW - DAY)
    pl.ledger_path().parent.mkdir(parents=True, exist_ok=True)
    pl.ledger_path().write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    _hook_calls(60, NOW - DAY)
    _event("CHZ-FO-A", NOW - 3600)
    g2 = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["G2"]
    assert g2["value"] == "1.00 per 100 calls (1 events / 100 calls, 1.0d window)"


def test_rendering_and_the_weekly_file_carry_the_detail_lines(tmp_path, monkeypatch):
    real_now = time.time()
    monkeypatch.setattr(failopen, "_now", lambda: real_now - 100)
    _hook_calls(100, real_now - 3600)
    failopen.record("CHZ-FO-A")
    data = kpi.compute_scorecard(days=7)
    text = kpi.render_scorecard(data)
    assert "1.00 per 100 calls (1 events / 100 calls" in text
    assert "        CHZ-FO-A: 1.00 per 100 calls (1)" in text
    path = kpi.write_weekly(data, tmp_path / "weekly")
    body = path.read_text()
    assert "## Details" in body and "- CHZ-FO-A: 1.00 per 100 calls (1)" in body
