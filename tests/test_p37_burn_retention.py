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


# ── F118: P3.7 follow-ups ───────────────────────────────────────────────────

def _row(home, sid, ts, kind="start", sk=None, h5=1.0, wk=1.0):
    row = {"session_id": sid, "kind": kind, "ts": ts, "five_hour_pct": h5, "weekly_pct": wk,
           "updated_at": ts, "five_hour_resets_at": None, "source": "measured"}
    if sk:
        row["session_kind"] = sk
    with open(home / qs.SAMPLES_NAME, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def test_baseline_anchors_on_first_organic_row_not_synthetic(home):
    _row(home, "syn1", T0 - 5 * DAY, sk="synthetic")
    _row(home, "bench", T0 - 3 * DAY, sk="benchmark")
    _row(home, "org1", T0, sk="organic")
    _row(home, "org2", T0 + DAY, sk="organic")
    assert qs.baseline_start_ts() == T0


def test_baseline_ignores_history_pruning(home):
    _row(home, "org1", T0, sk="organic")
    with open(home / qs.HISTORY_NAME, "w") as fh:  # history holds an earlier row ...
        fh.write(json.dumps({"ts": T0 - 9 * DAY, "source": "measured"}) + "\n")
    assert qs.baseline_start_ts() == T0
    (home / qs.HISTORY_NAME).write_text("")  # ... pruned away: same answer
    assert qs.baseline_start_ts() == T0


def test_baseline_none_without_organic_row(home):
    _row(home, "syn1", T0, sk="synthetic")
    _row(home, "untagged", T0 + 1)
    assert qs.baseline_start_ts() is None


def test_report_labels_assumed_baseline_and_flags_partial(home):
    _session(home, "b1", T0, [0, 2, 4], [0, 1, 2])
    with open(home / qs.SAMPLES_NAME) as fh:
        assert fh.read()
    # tag the baseline session organic via the row field
    lines = [json.loads(x) for x in (home / qs.SAMPLES_NAME).read_text().splitlines()]
    (home / qs.SAMPLES_NAME).write_text("".join(json.dumps({**r, "session_kind": "organic"}) + "\n" for r in lines))
    now = T0 + 3 * DAY  # week not complete
    r = qs.quota_burn_report(T0 + 2 * DAY, T0 + 3 * DAY, now=now)
    m = r["baseline_meta"]
    assert m["assumed"] is True and m["partial"] is True and m["week_complete"] is False
    text = qs.render_quota_burn_report(r)
    assert "(assumed: first organic row)" in text and "partial baseline" in text
    # complete week but only 1 session < minimum: still partial, with n shown
    r2 = qs.quota_burn_report(T0 + 8 * DAY, T0 + 9 * DAY, now=T0 + 10 * DAY)
    assert r2["baseline_meta"]["partial"] is True and r2["baseline_meta"]["sessions"] == 1
    assert "[partial baseline, n=1]" in qs.render_quota_burn_report(r2)


def test_explicit_baseline_not_labelled_assumed(home):
    _session(home, "b1", T0, [0, 2, 4], [0, 1, 2])
    r = qs.quota_burn_report(T0 + 20 * DAY, T0 + 21 * DAY, baseline_start=T0, now=T0 + 30 * DAY)
    assert r["baseline_meta"]["assumed"] is False
    assert "(assumed" not in qs.render_quota_burn_report(r)


def test_sufficient_baseline_not_partial(home, monkeypatch):
    monkeypatch.setattr(qs, "MIN_BASELINE_SESSIONS", 2)
    for i in range(2):
        _session(home, f"b{i}", T0 + i * 3600, [0, 2, 4], [0, 1, 2])
    lines = [json.loads(x) for x in (home / qs.SAMPLES_NAME).read_text().splitlines()]
    (home / qs.SAMPLES_NAME).write_text("".join(json.dumps({**r, "session_kind": "organic"}) + "\n" for r in lines))
    r = qs.quota_burn_report(T0 + 8 * DAY, T0 + 9 * DAY, now=T0 + 10 * DAY)
    assert r["baseline_meta"]["partial"] is False
    assert "partial baseline" not in qs.render_quota_burn_report(r)


def test_day14_with_no_session_yet_is_not_yet(home):
    ad.mark_active(T0)
    assert ad.status(T0 + 14 * DAY)["active_on_day14"] == "not yet"
    assert ad.status(T0 + 15 * DAY)["active_on_day14"] == "no"


def test_strict_reading_active_day15_not_day14_is_no(home):
    ad.mark_active(T0)
    ad.mark_active(T0 + 15 * DAY)
    assert ad.status(T0 + 15 * DAY)["active_on_day14"] == "no"


@pytest.mark.timing
def test_mark_active_skips_without_reading_when_file_is_from_today(home, monkeypatch):
    ad.mark_active()  # real now: file mtime is today
    def boom(_p):
        raise AssertionError("tail read on a same-day call")
    monkeypatch.setattr(ad, "_last_row", boom)
    assert ad.mark_active() is False


def test_second_sample_same_day_does_not_append(home):
    assert ad.mark_active(T0) is True
    assert ad.mark_active(T0 + 60) is False
    assert len(_rows(home / ad.NAME)) == 1


def test_trim_threshold_matches_real_full_file_size(home):
    path = home / ad.NAME
    iid = "a" * 16
    base = date(2020, 1, 1)
    rows = [{"day": (base + timedelta(days=i)).isoformat(), "install_id": iid} for i in range(ad.MAX_ROWS)]
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    assert path.stat().st_size == ad.MAX_ROWS * ad._ROW_BYTES
    assert ad.mark_active(T0) is True  # 401 rows -> trimmed back to the bound
    out = _rows(path)
    assert len(out) == ad.MAX_ROWS and out[0] == rows[0] and out[-1]["day"] == _day(T0)


def test_existing_files_are_chmodded_0600(home):
    path = home / ad.NAME
    path.write_text("")
    path.chmod(0o644)
    ad.mark_active(T0)
    assert (path.stat().st_mode & 0o777) == 0o600
    sp = home / qs.SAMPLES_NAME
    sp.write_text("")
    sp.chmod(0o644)
    qs._append(sp, {"ts": 1})
    assert (sp.stat().st_mode & 0o777) == 0o600


def test_concurrent_fresh_install_one_id_one_row_per_day(home):
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    with ctx.Pool(6) as pool:
        pool.map(_mark_same_day, [T0] * 12)
    rows = _rows(home / ad.NAME)
    assert len(rows) >= 1
    assert len({r["day"] for r in rows}) == len(rows) == 1
    assert len({r["install_id"] for r in rows}) == 1


def _mark_same_day(now):
    ad.mark_active(now)


def test_dedupe_on_trim_collapses_same_day_rows(home):
    path = home / ad.NAME
    iid = "b" * 16
    d = _day(T0)
    path.write_text("".join(json.dumps({"day": x, "install_id": iid}, separators=(",", ":")) + "\n"
                            for x in ("2020-01-01", d, d)))
    ad._trim(path)
    assert [r["day"] for r in _rows(path)] == ["2020-01-01", d]


def test_appends_survive_platform_without_fchmod(home, monkeypatch):
    """Windows has no os.fchmod: both appends must still write their row."""
    import os

    monkeypatch.delattr(os, "fchmod", raising=False)
    assert not hasattr(os, "fchmod")
    assert ad.mark_active(T0) is True
    assert _rows(home / ad.NAME)[0]["day"] == _day(T0)
    sp = home / qs.SAMPLES_NAME
    qs._append(sp, {"ts": 1})
    assert _rows(sp) == [{"ts": 1}]


@pytest.fixture()
def fo(monkeypatch):
    """Capture failopen.record calls."""
    from llm_router import failopen

    calls = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: calls.append(code))
    return calls


def test_install_id_loser_reads_winner_and_leaves_no_tmp(home):
    path = home / ad.NAME
    first = ad._new_install_id(path)
    assert (home / "active_days.id").read_text() == first
    assert ad._new_install_id(path) == first
    assert [p.name for p in home.iterdir() if p.name.endswith(".tmp")] == []


def test_link_failing_falls_back_and_row_is_written(home, monkeypatch, fo):
    import os

    def boom(*a, **k):
        raise PermissionError(1, "no hard links here")

    monkeypatch.setattr(os, "link", boom)
    assert ad.mark_active(T0) is True
    rows = _rows(home / ad.NAME)
    assert rows[0]["day"] == _day(T0)
    assert (home / "active_days.id").read_text() == rows[0]["install_id"]
    assert "P37-ACTIVE-DAY-LINK" in fo
    assert [p.name for p in home.iterdir() if p.name.endswith(".tmp")] == []


def test_link_absent_falls_back_and_row_is_written(home, monkeypatch, fo):
    import os

    monkeypatch.delattr(os, "link")
    assert ad.mark_active(T0) is True
    rows = _rows(home / ad.NAME)
    assert (home / "active_days.id").read_text() == rows[0]["install_id"]
    assert "P37-ACTIVE-DAY-LINK" in fo


def test_fallback_loser_reads_winner_id(home, monkeypatch):
    import os

    monkeypatch.delattr(os, "link")
    (home / "active_days.id").write_text("w" * 16)
    assert ad._new_install_id(home / ad.NAME) == "w" * 16


def test_tmp_unlink_failure_is_reported_not_raised(home, monkeypatch, fo):
    import os

    real = os.unlink

    def bad(p, *a, **k):
        if str(p).endswith(".tmp"):
            raise PermissionError(13, "locked")
        return real(p, *a, **k)

    monkeypatch.setattr(os, "unlink", bad)
    assert ad.mark_active(T0) is True
    assert "P37-ACTIVE-DAY-TMP" in fo
    assert _rows(home / ad.NAME)[0]["day"] == _day(T0)


def test_fchmod_oserror_still_appends_and_is_reported(home, monkeypatch, fo):
    import os

    def boom(*a, **k):
        raise OSError(1, "not permitted")

    monkeypatch.setattr(os, "fchmod", boom)
    assert ad.mark_active(T0) is True
    assert _rows(home / ad.NAME)[0]["day"] == _day(T0)
    assert "P37-ACTIVE-DAY-CHMOD" in fo


def _slow_reader(args):
    """Poll for the id file; return every distinct content seen (including "")."""
    home, stop_at = args
    seen = set()
    idp = Path(home) / "active_days.id"
    while time.time() < stop_at:
        try:
            seen.add(idp.read_text())
        except FileNotFoundError:
            pass
    return sorted(seen)


def _slow_creator(args):
    home = args
    import os

    real_write = os.write

    def slow_write(fd, data):
        time.sleep(0.3)  # widen the create-to-content window an O_EXCL+write design exposes
        return real_write(fd, data)

    os.write = slow_write
    try:
        return ad._new_install_id(Path(home) / ad.NAME)
    finally:
        os.write = real_write


def test_install_id_file_is_never_observed_empty(home):
    """A reader polling while the creator's write is slow must never see an empty id file
    (fails on an open(O_EXCL)-then-write design, passes with temp file + os.link)."""
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    with ctx.Pool(2) as pool:
        r = pool.map_async(_slow_reader, [(str(home), time.time() + 1.0)])
        time.sleep(0.1)
        iid = pool.apply(_slow_creator, (str(home),))
        seen = r.get(timeout=10)[0]
    assert seen, "reader never saw the id file: the check found nothing to check"
    assert "" not in seen
    assert seen == [iid]


def test_orphan_tmp_files_are_swept_only_when_old_and_exact_pattern(home):
    import os

    old = home / "active_days.id.123.abcd.tmp"
    new = home / "active_days.id.124.abcd.tmp"
    other = home / "other.tmp"
    for p in (old, new, other):
        p.write_text("x")
    past = time.time() - 7200
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    ad.mark_active(T0 + 0)  # first row of the day triggers the sweep
    assert not old.exists() and new.exists() and other.exists()


def test_sweep_ignores_lookalike_names_and_removes_dangling_symlinks(home, tmp_path):
    import os

    past = time.time() - 7200
    keep = [home / "active_days.id.backup.tmp", home / "active_days.id.12.XYZ.tmp",
            home / "active_days.id.12.abcd.tmp.bak", home / "active_days.id..abcd.tmp"]
    for p in keep:
        p.write_text("x")
        os.utime(p, (past, past))
    dangling = home / "active_days.id.55.beef01.tmp"
    dangling.symlink_to(tmp_path / "does-not-exist")
    os.utime(dangling, (past, past), follow_symlinks=False)
    target = tmp_path / "outside.txt"
    target.write_text("keep me")
    linked = home / "active_days.id.56.beef02.tmp"
    linked.symlink_to(target)
    os.utime(linked, (past, past), follow_symlinks=False)
    young = home / "active_days.id.57.beef03.tmp"
    young.symlink_to(tmp_path / "also-missing")  # lstat mtime is now: too young
    assert dangling.is_symlink() and not dangling.exists()  # the case under test exists
    ad._sweep_orphan_tmps(home / "active_days.jsonl")
    assert all(p.exists() for p in keep)
    assert not dangling.is_symlink() and not linked.is_symlink()
    assert target.read_text() == "keep me"  # not followed
    assert young.is_symlink()
