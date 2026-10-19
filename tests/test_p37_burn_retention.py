"""P3.7: S3 quota burn vs the GE6 baseline week, and S7 retention per D-26 option A.

Pre-registered behaviour:
  * ``active_days.jsonl`` holds one ``{day, install_id}`` row per local day, no other field;
    one append per day, bounded at ``MAX_ROWS`` with the first row kept; status says
    yes / no / not yet / n/a for "active on day 14 after install";
  * the marker is written from the existing session start / Stop sample path;
  * ``kpi --quota-burn`` compares the window with the baseline week (7 days from the first
    GE6 row), reports per-session S3 coverage with n, unknown as n/a (never 0), and never
    claims a reduction.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from llm_router import active_days as ad
from llm_router import quota_samples as qs

DAY = 86400.0
T0 = 1_791_000_000.0  # baseline start


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "rh"
    h.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h))
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield h
    monkeypatch.undo()
    time.tzset()


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _rows(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# ── S7 marker ───────────────────────────────────────────────────────────────

def test_marker_one_row_per_day_with_only_day_and_install_id(home):
    assert ad.mark_active(T0) is True
    assert ad.mark_active(T0 + 60) is False  # same day: no second row
    assert ad.mark_active(T0 + DAY) is True
    rows = _rows(home / ad.NAME)
    assert [set(r) for r in rows] == [{"day", "install_id"}] * 2
    assert rows[0]["install_id"] == rows[1]["install_id"] and len(rows[0]["install_id"]) == 16
    assert (home / ad.NAME).stat().st_mode & 0o777 == 0o600


def test_status_na_without_marker_never_zero(home):
    st = ad.status(T0)
    assert st["instrumented"] is False and st["active_on_day14"] == "n/a"
    assert st["active_days"] is None
    assert "n/a" in ad.render_status(st)


@pytest.mark.parametrize("extra_days,expect", [
    ([], "not yet"),            # day 14 not reached
    ([14], "yes"),              # active on day 14, today is day 14
])
def test_status_not_yet_and_yes(home, extra_days, expect):
    ad.mark_active(T0)
    for d in extra_days:
        ad.mark_active(T0 + d * DAY)
    now = T0 + (14 * DAY if extra_days else 13 * DAY)
    st = ad.status(now)
    assert st["active_on_day14"] == expect
    assert st["install_day"] == _day(T0) and st["day14"] == _day(T0 + 14 * DAY)


def test_status_no_when_day14_passed_without_activity(home):
    for d in (0, 1, 2, 15, 16):
        ad.mark_active(T0 + d * DAY)
    st = ad.status(T0 + 16 * DAY)
    assert st["active_on_day14"] == "no" and st["active_days"] == 5
    assert st["days_since_install"] == 16


def test_marker_is_bounded_and_keeps_install_row(home):
    path = home / ad.NAME
    first = {"day": "2020-01-01", "install_id": "abcd" * 4}
    lines = [first] + [{"day": (date(2020, 1, 2) + timedelta(days=i)).isoformat(),
                        "install_id": first["install_id"]} for i in range(ad.MAX_ROWS + 20)]
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in lines))
    assert ad.mark_active(T0) is True
    rows = _rows(path)
    assert len(rows) <= ad.MAX_ROWS
    assert rows[0] == first and rows[-1]["day"] == _day(T0)


def test_corrupt_lines_are_skipped(home):
    (home / ad.NAME).write_text('not json\n{"day":"nope"}\n')
    assert ad.status(T0)["instrumented"] is False
    assert ad.mark_active(T0) is True
    assert ad.status(T0)["active_days"] == 1


def test_marker_written_from_the_session_sample_path(home):
    (home / "usage.json").write_text(json.dumps(
        {"session_pct": 5.0, "weekly_pct": 10.0, "updated_at": T0 - 5}))
    assert qs.append_session_sample("s1", "start", now=T0) is True
    assert _rows(home / ad.NAME)[0]["day"] == _day(T0)
    # no session id: no quota row, but the day is still marked
    (home / ad.NAME).unlink()
    assert qs.append_session_sample(None, "start", now=T0) is False
    assert _rows(home / ad.NAME)[0]["day"] == _day(T0)


def test_marker_failure_never_raises(home, monkeypatch):
    monkeypatch.setattr(ad, "_append", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    assert ad.mark_active(T0) is False


# ── S3 vs baseline ──────────────────────────────────────────────────────────

def _session(home: Path, sid: str, t: float, h5: list[float], wk: list[float], *, source="measured"):
    """A tagged organic session: start sample, then one Stop (turn) per later reading."""
    (home / f"session_kind_{sid}.json").write_text(json.dumps(
        {"ts": t, "session_id": sid, "kind": "organic"}))
    with open(home / qs.SAMPLES_NAME, "a") as fh:
        for i, (a, b) in enumerate(zip(h5, wk)):
            fh.write(json.dumps({"session_id": sid, "kind": "start" if i == 0 else "stop",
                                 "ts": t + 60 * i, "five_hour_pct": a, "weekly_pct": b,
                                 "updated_at": t + 60 * i, "five_hour_resets_at": None,
                                 "source": source}) + "\n")


def test_report_compares_with_baseline_and_counts_coverage(home):
    # baseline week: 2 sessions, 10 five-hour pts over 4 turns -> 2.5 per turn
    _session(home, "b1", T0, [0, 2, 4], [0, 1, 2])
    _session(home, "b2", T0 + DAY, [10, 13, 17], [5, 6, 8])
    # current window: 4 sessions, 3 with burn, 1 start-only (not reported)
    c0 = T0 + 10 * DAY
    _session(home, "c1", c0, [0, 1, 2], [0, 1, 1])
    _session(home, "c2", c0 + 3600, [5, 6, 7], [0, 0, 1])
    _session(home, "c3", c0 + 7200, [9, 11, 13], [2, 3, 4])
    _session(home, "c4", c0 + 9000, [1], [1])
    r = qs.quota_burn_report(c0 - 1, c0 + DAY)
    v = r["s3_vs_baseline"]
    assert v["baseline_available"] and not v["overlaps_baseline"]
    assert (v["reported"], v["sessions"]) == (3, 4) and v["rate"] == 0.75
    assert v["claims_reduction"] is False
    bm = r["baseline"]["measured"]
    assert bm["n_sessions"] == 2 and bm["n_turns"] == 4
    assert bm["five_hour_per_turn"] == pytest.approx(11 / 4)  # (4 + 7) / 4
    cur = r["measured"]["five_hour_per_turn"]  # (2 + 2 + 4) / 6
    assert v["five_hour_per_turn_ratio"] == pytest.approx(cur / bm["five_hour_per_turn"])
    text = qs.render_quota_burn_report(r)
    assert "3/4 sessions" in text and "no reduction claimed" in text and "baseline week" in text
    assert r["baseline"]["window"]["since"] == T0  # first GE6 row anchors the baseline


def test_no_baseline_is_na_not_zero(home):
    r = qs.quota_burn_report(T0, T0 + DAY)
    v = r["s3_vs_baseline"]
    assert r["baseline"] is None and v["rate"] is None and v["reported"] is None
    assert v["five_hour_per_turn_ratio"] is None
    text = qs.render_quota_burn_report(r)
    assert "baseline week: n/a" in text and "S3 reported vs baseline: n/a" in text
    assert "0.00x" not in text and "0.0%" not in text


def test_zero_baseline_burn_gives_no_ratio(home):
    _session(home, "b1", T0, [3, 3, 3], [4, 4, 4])
    c0 = T0 + 9 * DAY
    _session(home, "c1", c0, [0, 2, 4], [0, 1, 2])
    v = qs.quota_burn_report(c0 - 1, c0 + DAY)["s3_vs_baseline"]
    assert v["five_hour_per_turn_ratio"] is None  # divide by a zero baseline is unknown


def test_overlap_with_baseline_is_flagged(home):
    _session(home, "b1", T0, [0, 2, 4], [0, 1, 2])
    r = qs.quota_burn_report(T0 - 1, T0 + 2 * DAY)
    assert r["s3_vs_baseline"]["overlaps_baseline"] is True
    assert "not independent" in qs.render_quota_burn_report(r)


def test_explicit_baseline_start_overrides_first_row(home):
    _session(home, "b1", T0, [0, 2, 4], [0, 1, 2])
    r = qs.quota_burn_report(T0 + 20 * DAY, T0 + 21 * DAY, baseline_start=T0 + 30 * DAY)
    assert r["baseline"]["window"]["since"] == T0 + 30 * DAY
    assert r["s3_vs_baseline"]["baseline_available"] is False


def test_history_burn_second_source(home):
    rows = []
    for i in range(6):  # 5-minute series, weekly 10 -> 15, one reading stale
        rows.append({"ts": T0 + 300 * i, "five_hour_pct": float(i), "weekly_pct": 10.0 + i,
                     "updated_at": T0 + 300 * i, "five_hour_resets_at": None,
                     "source": "stale" if i == 3 else "measured"})
    (home / qs.HISTORY_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows))
    h = qs.history_burn(T0 - 1, T0 + DAY)
    assert h["measured_rows"] == 5
    # the stale row is skipped, the 10-minute neighbours still join: +1 +1 +2 +1
    assert h["weekly_delta_sum"] == pytest.approx(5.0) and h["active_hours"] == 1
    assert h["weekly_per_active_hour"] == pytest.approx(5.0)
    empty = qs.history_burn(T0 + 5 * DAY, T0 + 6 * DAY)
    assert empty["weekly_delta_sum"] is None and empty["weekly_per_active_hour"] is None


def test_history_burn_splits_at_gaps(home):
    rows = [{"ts": T0, "five_hour_pct": 1.0, "weekly_pct": 10.0, "source": "measured"},
            {"ts": T0 + 7200, "five_hour_pct": 9.0, "weekly_pct": 50.0, "source": "measured"}]
    (home / qs.HISTORY_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows))
    h = qs.history_burn(T0 - 1, T0 + DAY)
    assert h["pairs"] == 0 and h["weekly_delta_sum"] is None  # 2 h apart: not one reading


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_quota_burn_prints_report_and_retention(home, capsys):
    from llm_router.commands import kpi

    _session(home, "b1", T0, [0, 2, 4], [0, 1, 2])
    ad.mark_active(T0)
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))  # noqa: E731
    assert kpi.cmd_kpi(["--quota-burn", "--since", iso(T0 + 8 * DAY), "--until", iso(T0 + 9 * DAY)]) == 0
    out = capsys.readouterr().out
    assert "baseline week" in out and "S3 reported vs baseline" in out and "Retention (S7" in out
    assert kpi.cmd_kpi(["--quota-burn", "--json", "--since", iso(T0), "--until", iso(T0 + DAY),
                        "--baseline-start", iso(T0)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["s3_vs_baseline"]["claims_reduction"] is False and data["retention"]["instrumented"]


def test_cli_retention_flag(home, capsys):
    from llm_router.commands import kpi

    assert kpi.cmd_kpi(["--retention"]) == 0
    assert "n/a" in capsys.readouterr().out
    ad.mark_active()
    assert kpi.cmd_kpi(["--retention", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["active_on_day14"] == "not yet"
