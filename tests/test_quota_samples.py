"""GE6 quota-burn baseline (PLAN v16 GE6, S3).

Pre-registered behaviour:
  * SessionStart and Stop append one ``{session_id, kind, ts, five_hour_pct,
    weekly_pct, source}`` row to ``quota_samples.jsonl`` from ``usage.json``;
  * the status-line tick appends one ``{ts, five_hour_pct, weekly_pct, ...}`` row to
    ``quota_history.jsonl`` at most every 300 s (the 5-minute series);
  * a snapshot whose ``updated_at`` is more than 30 min old, a fallback / pending
    snapshot, or a missing one is ``source: stale`` (never ``measured``), and an
    invented or missing value is null, never a number;
  * both files are append-only, 0600, and carry no text besides these fields;
  * ``kpi --quota-burn --since --until``: burn per session and per human turn from
    measured samples only; stale samples are labelled estimated and kept out of
    the measured line; coverage counts every tagged session in the window.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from llm_router import quota_samples as qs
from llm_router import statusline_tick as tick

NOW = 1_791_234_000.0
FRESH = {"session_pct": 12.0, "weekly_pct": 41.0, "sonnet_pct": 3.0,
         "highest_pressure": 0.41, "updated_at": NOW - 60}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "rh"
    h.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h))
    return h


def _usage(home: Path, data: dict) -> None:
    (home / "usage.json").write_text(json.dumps(data))


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── one sample from usage.json ───────────────────────────────────────────────

def test_fresh_snapshot_is_measured():
    s = qs.sample_from_usage(FRESH, now=NOW)
    assert s == {"five_hour_pct": 12.0, "weekly_pct": 41.0,
                 "updated_at": NOW - 60, "five_hour_resets_at": None, "source": "measured"}


def test_snapshot_older_than_30_min_is_stale_but_keeps_its_values():
    old = dict(FRESH, updated_at=NOW - 1801)
    s = qs.sample_from_usage(old, now=NOW)
    assert s["source"] == "stale"
    assert s["five_hour_pct"] == 12.0 and s["weekly_pct"] == 41.0


def test_exactly_30_min_is_still_measured():
    assert qs.sample_from_usage(dict(FRESH, updated_at=NOW - 1800), now=NOW)["source"] == "measured"


@pytest.mark.parametrize("usage", [
    None,
    {"pending": True},
    dict(FRESH, is_fallback=True),          # invented 50s: not a reading
    {"updated_at": NOW},                      # no pct fields at all
    dict(FRESH, session_pct=True, weekly_pct="41"),  # not numbers
])
def test_unknown_snapshot_is_stale_with_null_values(usage):
    s = qs.sample_from_usage(usage, now=NOW)
    assert s["source"] == "stale"
    assert s["five_hour_pct"] is None and s["weekly_pct"] is None


def test_snapshot_without_updated_at_is_stale():
    s = qs.sample_from_usage({"session_pct": 5.0, "weekly_pct": 6.0}, now=NOW)
    assert s["source"] == "stale" and s["updated_at"] is None


# ── session samples (SessionStart / Stop) ────────────────────────────────────

def test_append_session_sample_writes_one_row_0600(home):
    _usage(home, FRESH)
    assert qs.append_session_sample("sess-1", "start", now=NOW) is True
    path = home / qs.SAMPLES_NAME
    rows = _rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-1" and row["kind"] == "start"
    assert row["ts"] == NOW and row["source"] == "measured"
    assert row["five_hour_pct"] == 12.0 and row["weekly_pct"] == 41.0
    assert set(row) <= {"session_id", "kind", "ts", "five_hour_pct", "weekly_pct",
                        "updated_at", "five_hour_resets_at", "source", "session_kind"}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_session_samples_are_append_only(home):
    _usage(home, FRESH)
    path = home / qs.SAMPLES_NAME
    path.write_text('{"keep": "me"}\n')
    os.chmod(path, 0o600)
    qs.append_session_sample("sess-1", "start", now=NOW)
    qs.append_session_sample("sess-1", "stop", now=NOW + 10)
    lines = path.read_text().splitlines()
    assert lines[0] == '{"keep": "me"}'
    assert [json.loads(x)["kind"] for x in lines[1:]] == ["start", "stop"]


def test_session_sample_needs_a_session_id_and_a_known_kind(home):
    _usage(home, FRESH)
    assert qs.append_session_sample(None, "start", now=NOW) is False
    assert qs.append_session_sample("s", "bogus", now=NOW) is False
    assert not (home / qs.SAMPLES_NAME).exists()


def test_session_sample_never_raises(home, monkeypatch):
    _usage(home, FRESH)
    monkeypatch.setattr(qs.os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert qs.append_session_sample("s", "start", now=NOW) is False


def test_session_sample_carries_the_session_kind_tag(home):
    _usage(home, FRESH)
    (home / "session_kind_sess-9.json").write_text(json.dumps({"session_id": "sess-9", "kind": "research",
                                                               "ts": NOW}))
    qs.append_session_sample("sess-9", "start", now=NOW)
    assert _rows(home / qs.SAMPLES_NAME)[0]["session_kind"] == "research"


# ── the 5-minute history series (status-line tick) ───────────────────────────

def test_tick_appends_history_at_most_every_300_s(home):
    usage = dict(FRESH)
    assert tick.maybe_append_history(str(home), usage, NOW) is True
    assert tick.maybe_append_history(str(home), usage, NOW + 1) is False
    assert tick.maybe_append_history(str(home), usage, NOW + 299) is False
    assert tick.maybe_append_history(str(home), usage, NOW + 300) is True
    rows = _rows(home / tick.HISTORY_NAME)
    assert [r["ts"] for r in rows] == [NOW, NOW + 300]
    assert rows[0] == {"ts": NOW, "five_hour_pct": 12.0, "weekly_pct": 41.0,
                       "updated_at": NOW - 60, "five_hour_resets_at": None, "source": "measured"}
    assert stat.S_IMODE(os.stat(home / tick.HISTORY_NAME).st_mode) == 0o600


def test_tick_history_labels_stale_and_unknown(home):
    tick.maybe_append_history(str(home), dict(FRESH, updated_at=NOW - 3600), NOW)
    tick.maybe_append_history(str(home), {"pending": True}, NOW + 300)
    tick.maybe_append_history(str(home), None, NOW + 600)
    rows = _rows(home / tick.HISTORY_NAME)
    assert [r["source"] for r in rows] == ["stale", "stale", "stale"]
    assert rows[0]["five_hour_pct"] == 12.0
    assert rows[1]["five_hour_pct"] is None and rows[2]["weekly_pct"] is None


def test_tick_history_is_append_only(home):
    path = home / tick.HISTORY_NAME
    path.write_text('{"old": 1}\n')
    tick.maybe_append_history(str(home), FRESH, NOW)
    assert path.read_text().splitlines()[0] == '{"old": 1}'
    assert len(path.read_text().splitlines()) == 2


def test_tick_history_and_session_sample_agree_on_the_rules():
    """The tick cannot import llm_router; its copy of the rules must not drift."""
    cases = [FRESH, dict(FRESH, updated_at=NOW - 1801), dict(FRESH, updated_at=NOW - 1800),
             {"pending": True}, dict(FRESH, is_fallback=True), None, {"session_pct": 1, "weekly_pct": 2},
             dict(FRESH, session_pct=True)]
    for usage in cases:
        assert tick.quota_sample(usage, NOW) == qs.sample_from_usage(usage, now=NOW), usage
    assert tick.HISTORY_INTERVAL_S == qs.HISTORY_INTERVAL_S
    assert tick.HISTORY_NAME == qs.HISTORY_NAME
    assert tick.QUOTA_STALE_AFTER_S == qs.STALE_AFTER_S


def test_tick_main_appends_history(home, monkeypatch):
    _usage(home, dict(FRESH, updated_at=__import__("time").time()))
    monkeypatch.setenv("LLM_ROUTER_STATUSLINE_REFRESH_CMD", "")
    monkeypatch.setattr(tick, "_refresh_argv", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    with redirect_stdout(io.StringIO()):
        tick.main()
    rows = _rows(home / tick.HISTORY_NAME)
    assert len(rows) == 1 and rows[0]["source"] == "measured"


# ── quota burn: Δ arithmetic, stale labelling, coverage ──────────────────────

def _write_samples(home: Path, rows: list[dict]) -> None:
    with open(home / qs.SAMPLES_NAME, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _s(sid, kind, ts, h5, wk, source="measured", sk="organic"):
    return {"session_id": sid, "kind": kind, "ts": ts, "five_hour_pct": h5, "weekly_pct": wk,
            "updated_at": ts, "source": source, "session_kind": sk}


def _tag(home: Path, sid: str, ts: float, kind: str = "organic") -> None:
    (home / f"session_kind_{sid}.json").write_text(json.dumps({"session_id": sid, "kind": kind, "ts": ts}))


def test_burn_delta_arithmetic(home):
    _write_samples(home, [
        _s("A", "start", NOW, 10, 40),
        _s("A", "stop", NOW + 60, 12, 41),
        _s("A", "stop", NOW + 120, 15, 42),
        _s("B", "start", NOW, 20, 50),
        _s("B", "stop", NOW + 60, 21, 50),
    ])
    for sid in ("A", "B"):
        _tag(home, sid, NOW)
    r = qs.quota_burn(NOW - 1, NOW + 1000)
    m = r["measured"]
    assert m["n_sessions"] == 2 and m["n_turns"] == 3
    assert m["five_hour_delta_sum"] == pytest.approx(6.0)
    assert m["weekly_delta_sum"] == pytest.approx(2.0)
    assert m["five_hour_per_session"] == pytest.approx(3.0)
    assert m["five_hour_per_turn"] == pytest.approx(2.0)
    assert m["weekly_per_turn"] == pytest.approx(2.0 / 3)
    assert r["coverage"]["covered"] == 2 and r["coverage"]["sessions"] == 2


def test_burn_counts_a_window_reset_and_keeps_the_burn_after_it(home):
    # 5h window resets mid-session: 80 -> 90, reset (window restarts at 0) to 2, then 2 -> 5.
    # Burn = 10 + 2 + 3: the 2 points after the reset were burned in this session too.
    _write_samples(home, [
        _s("A", "start", NOW, 80, 40),
        _s("A", "stop", NOW + 60, 90, 41),
        _s("A", "stop", NOW + 120, 2, 41),
        _s("A", "stop", NOW + 180, 5, 42),
    ])
    _tag(home, "A", NOW)
    m = qs.quota_burn(NOW - 1, NOW + 1000)["measured"]
    assert m["five_hour_delta_sum"] == pytest.approx(15.0)
    assert m["five_hour_resets"] == 1
    assert m["weekly_delta_sum"] == pytest.approx(2.0)


R1 = "2026-10-08T02:29:59.848187+00:00"   # usage.json session_resets_at (ISO string)
R2 = "2026-10-08T07:29:59.848187+00:00"   # the next 5h window


def _sr(sid, kind, ts, h5, wk, resets_at):
    row = _s(sid, kind, ts, h5, wk)
    row["five_hour_resets_at"] = qs._epoch(resets_at)
    return row


def test_sample_carries_the_five_hour_reset_time():
    s = qs.sample_from_usage(dict(FRESH, session_resets_at=R1), now=NOW)
    assert s["five_hour_resets_at"] == pytest.approx(1791427799.848187)
    assert qs.sample_from_usage(FRESH, now=NOW)["five_hour_resets_at"] is None
    assert qs.sample_from_usage(dict(FRESH, session_resets_at="garbage"), now=NOW)["five_hour_resets_at"] is None
    for usage in (dict(FRESH, session_resets_at=R1), dict(FRESH, session_resets_at="x"),
                  dict(FRESH, session_resets_at=R1, is_fallback=True)):
        assert tick.quota_sample(usage, NOW) == qs.sample_from_usage(usage, now=NOW), usage


def test_reset_seen_by_reset_time_counts_the_new_reading_even_when_it_is_higher(home):
    # 40 in window R1; the window rolls over to R2 and the session burns 50 there.
    # The 50 points are this session's burn, not 50 - 40 = 10.
    _write_samples(home, [_sr("A", "start", NOW, 40, 40, R1), _sr("A", "stop", NOW + 60, 50, 41, R2)])
    _tag(home, "A", NOW)
    m = qs.quota_burn(NOW - 1, NOW + 1000)["measured"]
    assert m["five_hour_delta_sum"] == pytest.approx(50.0)
    assert m["five_hour_resets"] == 1


def test_small_drop_inside_one_window_is_not_a_reset(home):
    # Same reset time, 45 -> 44.9 -> 46: jitter, burn 1.1, no reset (adding 44.9 would be a 40x error).
    _write_samples(home, [_sr("A", "start", NOW, 45, 40, R1), _sr("A", "stop", NOW + 60, 44.9, 40, R1),
                          _sr("A", "stop", NOW + 120, 46, 40, R1)])
    _tag(home, "A", NOW)
    m = qs.quota_burn(NOW - 1, NOW + 1000)["measured"]
    assert m["five_hour_delta_sum"] == pytest.approx(1.1)
    assert m["five_hour_resets"] == 0


def test_reset_time_wins_over_the_drop_size(home):
    # Same reset time on both readings: even a 10-pt drop is not a reset (burn 5, not 50 + 5).
    _write_samples(home, [_sr("A", "start", NOW, 60, 40, R1), _sr("A", "stop", NOW + 60, 50, 40, R1),
                          _sr("A", "stop", NOW + 120, 55, 40, R1)])
    _tag(home, "A", NOW)
    m = qs.quota_burn(NOW - 1, NOW + 1000)["measured"]
    assert m["five_hour_delta_sum"] == pytest.approx(5.0)
    assert m["five_hour_resets"] == 0


def test_small_drop_without_a_reset_time_is_not_a_reset(home):
    # weekly has no reset time in usage.json: 41 -> 40.8 -> 41.5 is jitter (burn 0.7), not a reset.
    _write_samples(home, [_s("A", "start", NOW, 10, 41), _s("A", "stop", NOW + 60, 11, 40.8),
                          _s("A", "stop", NOW + 120, 12, 41.5)])
    _tag(home, "A", NOW)
    m = qs.quota_burn(NOW - 1, NOW + 1000)["measured"]
    assert m["weekly_delta_sum"] == pytest.approx(0.7)
    assert m["five_hour_delta_sum"] == pytest.approx(2.0)


def test_stale_samples_are_estimated_and_kept_out_of_the_measured_line(home):
    _write_samples(home, [
        _s("A", "start", NOW, 10, 40),
        _s("A", "stop", NOW + 60, 99, 99, source="stale"),  # ignored by the measured line
        _s("A", "stop", NOW + 120, 13, 41),
        _s("C", "start", NOW, 10, 40, source="stale"),
        _s("C", "stop", NOW + 60, 30, 45, source="stale"),
    ])
    _tag(home, "A", NOW)
    _tag(home, "C", NOW)
    r = qs.quota_burn(NOW - 1, NOW + 1000)
    m, e = r["measured"], r["estimated"]
    assert m["n_sessions"] == 1
    assert m["five_hour_delta_sum"] == pytest.approx(3.0)
    assert e["n_sessions"] == 1 and e["label"] == "estimated"
    assert e["five_hour_delta_sum"] == pytest.approx(20.0)
    assert r["samples"]["stale"] == 3 and r["samples"]["measured"] == 2


def test_coverage_counts_tagged_sessions_with_no_samples(home):
    _write_samples(home, [_s("A", "start", NOW, 10, 40), _s("A", "stop", NOW + 60, 11, 40),
                          _s("B", "start", NOW, 10, 40)])  # B: no stop sample
    _tag(home, "A", NOW)
    _tag(home, "B", NOW)
    _tag(home, "D", NOW)                     # tagged, never sampled
    _tag(home, "R", NOW, kind="research")   # research: out by default
    _tag(home, "OLD", NOW - 10 * 86400)      # outside the window
    cov = qs.quota_burn(NOW - 1, NOW + 1000)["coverage"]
    assert cov == {**cov, "sessions": 3, "covered": 1, "with_start": 2, "with_stop": 1}
    assert cov["rate"] == pytest.approx(1 / 3)
    assert cov["wilson_lo"] < 1 / 3 < cov["wilson_hi"]


def test_research_sessions_only_with_include_research(home):
    _write_samples(home, [_s("R", "start", NOW, 10, 40, sk="research"),
                          _s("R", "stop", NOW + 60, 14, 41, sk="research")])
    _tag(home, "R", NOW, kind="research")
    assert qs.quota_burn(NOW - 1, NOW + 1000)["measured"]["n_sessions"] == 0
    r = qs.quota_burn(NOW - 1, NOW + 1000, include_research=True)
    assert r["measured"]["n_sessions"] == 1


def test_empty_window_is_not_informative(home):
    r = qs.quota_burn(NOW - 1, NOW + 1000)
    assert r["measured"]["n_sessions"] == 0
    assert r["measured"]["five_hour_per_turn"] is None
    assert r["informative"] is False
    text = qs.render_quota_burn(r)
    assert "not informative" in text and "n=0" in text


def test_history_series_summary(home):
    with open(home / qs.HISTORY_NAME, "w") as fh:
        for i in range(10):
            src = "stale" if i == 3 else "measured"
            fh.write(json.dumps({"ts": NOW + i * 300, "five_hour_pct": 1, "weekly_pct": 2,
                                 "updated_at": NOW, "source": src}) + "\n")
        fh.write(json.dumps({"ts": NOW + 9 * 300 + 3000, "five_hour_pct": 1, "weekly_pct": 2,
                             "updated_at": NOW, "source": "measured"}) + "\n")
    h = qs.quota_burn(NOW, NOW + 3600 * 2)["history"]
    assert h["rows"] == 11 and h["stale"] == 1 and h["measured"] == 10
    assert h["slots"] == 24 and h["slots_filled"] == 11
    assert h["largest_gap_s"] == pytest.approx(3000)


def test_malformed_lines_are_skipped_and_counted(home):
    (home / qs.SAMPLES_NAME).write_text('not json\n{"session_id": "A"}\n' +
                                       json.dumps(_s("A", "start", NOW, 1, 2)) + "\n")
    _tag(home, "A", NOW)
    r = qs.quota_burn(NOW - 1, NOW + 1000)
    assert r["samples"]["skipped"] == 2


def test_per_turn_bootstrap_ci_brackets_the_point(home):
    rows = []
    for i in range(12):
        sid = f"S{i}"
        rows += [_s(sid, "start", NOW, 10, 40), _s(sid, "stop", NOW + 60, 10 + i % 4, 40),
                 _s(sid, "stop", NOW + 120, 12 + i % 4, 41)]
        _tag(home, sid, NOW)
    _write_samples(home, rows)
    m = qs.quota_burn(NOW - 1, NOW + 1000)["measured"]
    lo, hi = m["five_hour_per_turn_ci"]
    assert lo <= m["five_hour_per_turn"] <= hi
    assert m["largest_session_share"] <= 1.0 and len(m["top3_sessions"]) == 3


# ── the kpi command ──────────────────────────────────────────────────────────

def test_kpi_quota_burn_needs_an_absolute_window(home, capsys):
    from llm_router.commands.kpi import cmd_kpi

    with pytest.raises(SystemExit):
        cmd_kpi(["--quota-burn"])
    assert "--quota-burn needs --since and --until" in capsys.readouterr().err


def test_kpi_quota_burn_prints_measured_and_estimated(home, capsys):
    from llm_router.commands.kpi import cmd_kpi

    _write_samples(home, [_s("A", "start", NOW, 10, 40), _s("A", "stop", NOW + 60, 12, 41),
                          _s("C", "start", NOW, 1, 1, source="stale"),
                          _s("C", "stop", NOW + 60, 2, 2, source="stale")])
    _tag(home, "A", NOW)
    _tag(home, "C", NOW)
    since = "2026-10-05T00:00:00Z"
    until = "2026-10-12T00:00:00Z"
    assert cmd_kpi(["--quota-burn", "--since", since, "--until", until]) == 0
    out = capsys.readouterr().out
    assert "measured" in out and "estimated" in out and "n_sessions=1" in out
    assert cmd_kpi(["--quota-burn", "--since", since, "--until", until, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["measured"]["n_sessions"] == 1 and data["estimated"]["n_sessions"] == 1


# ── hook wiring ──────────────────────────────────────────────────────────────

HOOKS = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks"


def _load(name: str, mod_name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(mod_name, HOOKS / name)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def test_session_start_hook_appends_a_start_sample(home, monkeypatch):
    mod = _load("session-start.py", "ss_quota_samples")
    for fn in ("_ensure_ollama_running", "_ensure_pxpipe_running", "_sync_pxpipe_anthropic_base_url",
               "_refresh_claude_usage", "_format_learned_memory", "_weekly_digest", "_latency_hint",
               "_preflight_check"):
        if hasattr(mod, fn):
            monkeypatch.setattr(mod, fn, lambda: "")
    for fn in ("_maybe_refresh_benchmarks_bg", "_warm_ollama_bg", "_maybe_update_pull_routing_rules",
               "_spawn_background_usage_refresh"):
        if hasattr(mod, fn):
            monkeypatch.setattr(mod, fn, lambda: None)
    calls = []
    monkeypatch.setattr(qs, "append_session_sample", lambda sid, kind, **k: calls.append((sid, kind)))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "cc-sess-1"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    mod.main()
    assert calls == [("cc-sess-1", "start")]


def test_session_end_hook_appends_a_stop_sample(home, monkeypatch):
    mod = _load("session-end.py", "se_quota_samples")
    calls = []
    monkeypatch.setattr(qs, "append_session_sample", lambda sid, kind, **k: calls.append((sid, kind)))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "cc-sess-1"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    mod.main()
    assert calls == [("cc-sess-1", "stop")]
