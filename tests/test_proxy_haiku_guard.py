"""P0.11 (PLAN v16, D-20 = A): the Haiku Option A guard in the repo.

The guard turns ``haiku_rewrite`` off through ``tier_overrides.json`` when the evidence
goes bad, and never edits ``claude_tiers.yaml``. Fixtures are synthetic ledger rows
(ids, models, timestamps only; no prompt text).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest

from llm_router import paths
from llm_router.commands import kpi
from llm_router.proxy import haiku_guard as hg
from llm_router.proxy import server as ps
from llm_router.proxy import tiers as pt

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-5"
NOW = 1_791_400_000.0  # 2026-10-07T..Z, a fixed clock


# ── fixtures ────────────────────────────────────────────────────────────────


def _turn(sid: str, ts: float, *, model: str = HAIKU, reason: str = "haiku_rewrite", **extra) -> dict:
    return {"ts": ts, "session_id": sid, "session_kind": "organic", "decision": "forwarded",
            "upstream_status": 200, "requested_model": SONNET, "served_model": model,
            "tier": "haiku" if "haiku" in model else "sonnet", "tier_reason": reason,
            "step_class": "turn_first", "msg_id": f"msg_{sid}_{int(ts)}", **extra}


def _redo_ledger(n_turns: int, n_redone: int, now: float = NOW) -> list[dict]:
    """``n_turns`` Haiku-served human turns, one per session; the first ``n_redone`` are
    followed by an escalation in the next human turn (the proxy's own redo detector)."""
    rows = []
    for i in range(n_turns):
        t0 = now - 3600.0 - i * 60.0
        rows.append(_turn(f"s{i}", t0))
        if i < n_redone:
            rows.append(_turn(f"s{i}", t0 + 20.0, model=SONNET, reason="escalation"))
    return rows


def _retry_ledger(n: int, k: int, now: float = NOW) -> list[dict]:
    """``n`` Haiku-decided continuation calls, ``k`` of them refused and retried unchanged."""
    return [_turn("r", now - 600.0 - i, step_class="continuation",
                  tier_retry={"status": 400, "detail": "x"} if i < k else None) for i in range(n)]


def _summary(directory: Path, day: str, k: int, n: int, *, control=(9, 10), mtime: float | None = None):
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"summary-{day.replace('-', '')}-haiku.json"
    p.write_text(json.dumps({"date": day, "arms": {
        "haiku": {"k_acceptable": k, "n_rated": n},
        "control": {"k_acceptable": control[0], "n_rated": control[1]}}}))
    if mtime is not None:
        import os
        os.utime(p, (mtime, mtime))
    return p


def _yaml(tmp_path: Path, haiku_rewrite: bool = True) -> Path:
    text = pt.DEFAULT_POLICY_PATH.read_text()
    assert "\nhaiku_rewrite: false" in text
    p = tmp_path / "claude_tiers.yaml"
    p.write_text(text.replace("\nhaiku_rewrite: false", f"\nhaiku_rewrite: {str(haiku_rewrite).lower()}"))
    return p


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _eval(rows, **kw):
    kw.setdefault("audits", [])
    kw.setdefault("shadow_rows", [])
    return hg.evaluate(rows, since=NOW - 7 * 86400.0, until=NOW, kinds=frozenset({"organic"}), **kw)


# ── triggers ────────────────────────────────────────────────────────────────


def test_redo_30_haiku_turns_6_redone_trips():
    ev = _eval(_redo_ledger(30, 6))
    r = ev["triggers"]["redo"]
    assert (r["k"], r["n"]) == (6, 30)  # 20% > 15%
    assert r["evaluable"] and r["tripped"] and ev["tripped"] == ["redo"]


def test_subagent_first_rows_are_not_turns_for_the_redo_trigger():
    """O3-STEP-1, live behaviour change: ``subagent_first`` is a live proxy label. The guard has no
    transcript join, so those rows are not human turns: they neither count as Haiku turns nor push an
    escalation out of the unit's 2-turn window. Base (counts them as turns): n=40 k=0; head: n=40 k=40."""
    rows = []
    for i in range(40):
        t0 = NOW - 3600.0 - i * 60.0
        sid = f"s{i}"
        rows.append(_turn(sid, t0))
        rows += [_turn(sid, t0 + 5.0 + j, model=SONNET, reason="policy", step_class="subagent_first")
                 for j in range(2)]
        rows.append(_turn(sid, t0 + 20.0, model=SONNET, reason="escalation"))
    r = _eval(rows)["triggers"]["redo"]
    assert (r["k"], r["n"]) == (40, 40) and r["tripped"]
    only_sub = [_turn(f"u{i}", NOW - 3600.0 - i * 60.0, step_class="subagent_first") for i in range(40)]
    assert _eval(only_sub)["triggers"]["redo"]["n"] == 0


def test_redo_at_the_bar_does_not_trip_and_below_min_n_is_not_evaluable():
    at_bar = _eval(_redo_ledger(40, 6))["triggers"]["redo"]  # 15.0%, not > 15%
    assert at_bar["evaluable"] and not at_bar["tripped"]
    few = _eval(_redo_ledger(29, 29))["triggers"]["redo"]  # 100% redone but n < 30
    assert not few["evaluable"] and not few["tripped"]
    assert few["value"].startswith("not evaluable (n<min")


def test_redo_ignores_other_session_kinds():
    rows = [dict(r, session_kind="research") for r in _redo_ledger(30, 6)]
    assert _eval(rows)["triggers"]["redo"]["n"] == 0


def test_tier_retry_2_of_100_trips_2_of_99_does_not():
    hit = _eval(_retry_ledger(100, 2))["triggers"]["tier_retry"]
    assert (hit["k"], hit["n"], hit["tripped"]) == (2, 100, True)
    short = _eval(_retry_ledger(99, 2))["triggers"]["tier_retry"]
    assert (short["k"], short["n"], short["evaluable"], short["tripped"]) == (2, 99, False, False)
    one = _eval(_retry_ledger(100, 1))["triggers"]["tier_retry"]  # exactly 1%: not > 1%
    assert one["evaluable"] and not one["tripped"]


def test_tier_retry_ignores_other_session_kinds():
    rows = [dict(r, session_kind="research") for r in _retry_ledger(100, 2)]
    t = _eval(rows)["triggers"]["tier_retry"]
    assert (t["k"], t["n"], t["tripped"]) == (0, 0, False)


def test_tier_retry_counts_only_router_decided_haiku_calls():
    rows = _retry_ledger(100, 2)
    side = [dict(r, tier_reason="side_call") for r in _retry_ledger(50, 50)]
    client_haiku = [dict(r, requested_model=HAIKU) for r in _retry_ledger(50, 50)]
    t = _eval(rows + side + client_haiku)["triggers"]["tier_retry"]
    assert (t["k"], t["n"]) == (2, 100)


def _retried_original(i: int = 0) -> dict:
    """Haiku was decided, the rewrite 4xx'd, the proxy retried the original: served on Sonnet."""
    return _turn("r", NOW - 600.0 - i, step_class="continuation", model=SONNET, reason="haiku_rewrite",
                 tier_retry={"status": 400, "detail": "x"}) | {"tier": "haiku"}


def test_served_on_haiku_is_not_the_same_as_decided_haiku():
    normal = _turn("r", NOW - 10, step_class="continuation")
    retried = _retried_original()
    arm_served = _turn("r", NOW - 11, step_class="continuation", tier_arm_assignment="treatment")
    arm_retried = dict(retried, tier_arm_assignment="treatment_retried_original")
    assert [hg.is_haiku_decided(r) for r in (normal, retried, arm_served, arm_retried)] == [True] * 4
    assert [hg.is_haiku_served(r) for r in (normal, retried, arm_served, arm_retried)] == [True, False, True, False]


def test_watch_reports_served_next_to_decided_and_retries_stay_in_the_retry_trigger():
    rows = _retry_ledger(10, 0) + [_retried_original(i) for i in range(3)]
    ev = hg.watch(NOW - 86400.0, NOW, rows=rows)
    assert (ev["haiku_decided_calls"], ev["haiku_served_calls"]) == (13, 10)
    t = ev["triggers"]["tier_retry"]
    assert (t["k"], t["n"]) == (3, 13)  # a retried row is the numerator: it must stay decided
    assert "n=13 (served on Haiku: n=10)" in hg.render_watch(ev)


def test_two_consecutive_7_of_10_days_trip(tmp_path):
    _summary(tmp_path, "2026-10-06", 7, 10)
    _summary(tmp_path, "2026-10-07", 7, 10)
    t = hg.audit_daily_trigger(hg.read_audits(tmp_path), "2026-10-07")
    assert t["evaluable"] and t["tripped"]


def test_one_7_of_10_day_does_not_trip(tmp_path):
    _summary(tmp_path, "2026-10-06", 9, 10)
    _summary(tmp_path, "2026-10-07", 7, 10)
    t = hg.audit_daily_trigger(hg.read_audits(tmp_path), "2026-10-07")
    assert t["evaluable"] and not t["tripped"]
    alone = tmp_path / "alone"
    _summary(alone, "2026-10-07", 7, 10)
    t2 = hg.audit_daily_trigger(hg.read_audits(alone), "2026-10-07")
    assert t2["evaluable"] and not t2["tripped"]


def test_two_low_days_that_are_not_consecutive_do_not_trip(tmp_path):
    _summary(tmp_path, "2026-10-05", 7, 10)
    _summary(tmp_path, "2026-10-07", 7, 10)
    assert not hg.audit_daily_trigger(hg.read_audits(tmp_path), "2026-10-07")["tripped"]


def test_daily_audit_below_10_is_not_evaluable(tmp_path):
    _summary(tmp_path, "2026-10-06", 0, 9)
    _summary(tmp_path, "2026-10-07", 0, 9)
    t = hg.audit_daily_trigger(hg.read_audits(tmp_path), "2026-10-07")
    assert not t["evaluable"] and not t["tripped"] and "not evaluable (n<min: 9<10)" in t["value"]


def test_audit_batch_m08b_trip(tmp_path):
    _summary(tmp_path, "2026-10-07", 22, 30)  # 73% < 75% at n >= 30
    assert hg.audit_batch_trigger(hg.read_audits(tmp_path))["tripped"]
    other = tmp_path / "ok"
    _summary(other, "2026-10-07", 23, 30)  # 76.7%
    assert not hg.audit_batch_trigger(hg.read_audits(other))["tripped"]


def _verdicts(k: int, n: int, cannot: int = 0) -> list[dict]:
    rows = [{"ts": NOW - 100 - i, "acceptable": i < k, "cannot_judge": False} for i in range(n)]
    return rows + [{"ts": NOW - 50, "acceptable": None, "cannot_judge": True}] * cannot


def test_shadow_below_26_of_30_at_n20_trips():
    assert hg.shadow_trigger(_verdicts(17, 20), until=NOW)["tripped"]       # 85% < 86.7%
    assert not hg.shadow_trigger(_verdicts(18, 20), until=NOW)["tripped"]   # 90%
    assert not hg.shadow_trigger(_verdicts(26, 30), until=NOW)["tripped"]   # exactly 26/30
    assert hg.shadow_trigger(_verdicts(43, 50), until=NOW)["tripped"]       # 86.0% < 86.7%
    t = hg.shadow_trigger(_verdicts(0, 19, cannot=5), until=NOW)
    assert not t["evaluable"] and not t["tripped"] and t["cannot_judge"] == 5
    assert hg.shadow_trigger(_verdicts(26, 30), until=NOW)["wilson95"] is not None


def test_shadow_old_verdicts_are_outside_the_window():
    old = [dict(r, ts=NOW - 15 * 86400.0) for r in _verdicts(0, 20)]
    assert hg.shadow_trigger(old, until=NOW)["n"] == 0


def test_wilson_interval():
    lo, hi = hg.wilson(26, 30)
    assert 0.70 < lo < 0.71 and 0.94 < hi < 0.95
    assert hg.wilson(0, 0) is None


# ── the override file and precedence ────────────────────────────────────────


def test_override_beats_yaml_and_deleting_it_restores_the_yaml(tmp_path):
    y = _yaml(tmp_path, True)
    assert pt.ClaudeTierPolicy.load(y).haiku_rewrite is True
    hg.write_override("test", NOW)
    p = pt.ClaudeTierPolicy.load(y)
    assert p.haiku_rewrite is False and p.haiku_override["reason"] == "test"
    hg.override_path().unlink()
    assert pt.ClaudeTierPolicy.load(y).haiku_rewrite is True


def test_override_cannot_turn_the_rewrite_on(tmp_path):
    y = _yaml(tmp_path, False)
    hg.override_path().parent.mkdir(parents=True, exist_ok=True)
    hg.override_path().write_text(json.dumps({"haiku_rewrite": True}))
    assert pt.ClaudeTierPolicy.load(y).haiku_rewrite is False
    assert hg.read_override() is None


def test_corrupt_override_is_ignored_and_recorded(tmp_path):
    y = _yaml(tmp_path, True)
    hg.override_path().parent.mkdir(parents=True, exist_ok=True)
    hg.override_path().write_text("{not json")
    assert pt.ClaudeTierPolicy.load(y).haiku_rewrite is True


def test_override_lives_in_the_state_dir():
    assert hg.override_path() == paths.state_path("tier_overrides.json")


# ── run_once: the trip, end to end ──────────────────────────────────────────


@pytest.mark.parametrize("trigger", ["redo", "tier_retry", "audit_daily"])
def test_run_once_trip_writes_override_turns_policy_off_and_leaves_yaml_untouched(tmp_path, monkeypatch,
                                                                                trigger):
    now = time.time()
    rows = {"redo": _redo_ledger(30, 6, now), "tier_retry": _retry_ledger(100, 2, now),
            "audit_daily": []}[trigger]
    audits = tmp_path / "audits"
    if trigger == "audit_daily":
        today = hg._utc_day(now)
        yday = hg._utc_day(now - 86400.0)
        _summary(audits, yday, 7, 10, mtime=now - 7200)
        _summary(audits, today, 7, 10, mtime=now - 3600)
    monkeypatch.setenv("LLM_ROUTER_HAIKU_GUARD_AUDIT_DIR", str(audits))
    y = _yaml(tmp_path, True)
    before = _sha(y)
    policy = pt.ClaudeTierPolicy.load(y)
    notes = []
    ev = hg.run_once(policy, now=now, read_rows=lambda: rows, notify=lambda r, e: notes.append(r))
    assert ev["action"] == "trip" and trigger in ev["tripped"]
    assert policy.haiku_rewrite is False
    assert json.loads(hg.override_path().read_text())["haiku_rewrite"] is False
    assert pt.ClaudeTierPolicy.load(y).haiku_rewrite is False
    assert _sha(y) == before  # the YAML is never edited
    assert len(notes) == 1
    status = hg.status_path().read_text().splitlines()
    assert len(status) == 1 and " TRIP " in status[0]
    # a second run does not trip again
    again = hg.run_once(policy, now=now, read_rows=lambda: rows, notify=lambda r, e: notes.append(r))
    assert again["action"] == "already_off" and len(notes) == 1


def test_run_once_ignores_daily_audits_older_than_the_window(tmp_path, monkeypatch):
    """Two low audit days a month ago are not today's evidence: after the owner deletes the
    override, the guard must not trip again on them (audit_daily dated outside the 7-day window)."""
    now = time.time()
    audits = tmp_path / "audits"
    _summary(audits, hg._utc_day(now - 31 * 86400.0), 7, 10, mtime=now - 31 * 86400.0)
    _summary(audits, hg._utc_day(now - 30 * 86400.0), 7, 10, mtime=now - 30 * 86400.0)
    monkeypatch.setenv("LLM_ROUTER_HAIKU_GUARD_AUDIT_DIR", str(audits))
    policy = pt.ClaudeTierPolicy.load(_yaml(tmp_path, True))
    ev = hg.run_once(policy, now=now, read_rows=lambda: [], notify=lambda r, e: None)
    t = ev["triggers"]["audit_daily"]
    assert ev["action"] == "ok" and not t["tripped"] and not t["evaluable"]
    assert policy.haiku_rewrite is True and not hg.override_path().exists()


def test_run_once_without_a_policy_does_not_rewrite_an_existing_override():
    """CLI / direct ``run_once(None)``: an override already on disk is left as written and no
    second notification goes out, even when the triggers still trip."""
    hg.write_override("earlier trip", NOW - 3600.0)
    before = hg.override_path().read_text()
    notes = []
    ev = hg.run_once(None, now=NOW, read_rows=lambda: _redo_ledger(30, 6, NOW),
                     notify=lambda r, e: notes.append(r))
    assert ev["tripped"] == ["redo"] and ev["action"] == "already_off"
    assert hg.override_path().read_text() == before and notes == []


def test_run_once_quiet_ledger_is_ok_and_writes_nothing_but_status(tmp_path):
    y = _yaml(tmp_path, True)
    policy = pt.ClaudeTierPolicy.load(y)
    ev = hg.run_once(policy, now=NOW, read_rows=lambda: _redo_ledger(30, 1, NOW))
    assert ev["action"] == "ok" and not ev["tripped"]
    assert policy.haiku_rewrite is True and not hg.override_path().exists()
    assert " OK n_haiku=30 " in hg.status_path().read_text()


def test_run_once_error_never_trips(tmp_path):
    def boom():
        raise RuntimeError("ledger gone")
    y = _yaml(tmp_path, True)
    policy = pt.ClaudeTierPolicy.load(y)
    ev = hg.run_once(policy, now=NOW, read_rows=boom)
    assert ev["action"] == "error" and policy.haiku_rewrite is True and not hg.override_path().exists()
    assert " ERROR RuntimeError" in hg.status_path().read_text()


def test_run_once_reads_the_real_ledger_path(tmp_path):
    now = time.time()
    led = paths.state_path("proxy_calls.jsonl")
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text("".join(json.dumps(r) + "\n" for r in _redo_ledger(30, 6, now)))
    policy = pt.ClaudeTierPolicy.load(_yaml(tmp_path, True))
    ev = hg.run_once(policy, now=now, notify=lambda r, e: None)
    assert ev["action"] == "trip" and policy.haiku_rewrite is False


def test_status_line_carries_no_message_ids(tmp_path):
    hg.run_once(None, now=NOW, read_rows=lambda: _redo_ledger(30, 6, NOW), notify=lambda r, e: None)
    text = hg.status_path().read_text() + hg.override_path().read_text()
    assert "msg_" not in text and "s1" not in text.split("since=")[0]


# ── in the proxy: on start, then on a timer ─────────────────────────────────


def _proxy(tmp_path, haiku_rewrite: bool, run):
    cfg = ps.ProxyConfig(steps=frozenset(), upstream="http://127.0.0.1:9", tiers=ps.TIERS_ON,
                         tier_policy=str(_yaml(tmp_path, haiku_rewrite)),
                         ledger_path=tmp_path / "proxy_calls.jsonl", warm_up=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    return ps.build_app(cfg, client=client, haiku_guard_run=run)


@pytest.mark.timing
async def test_proxy_start_runs_the_guard_and_a_trip_turns_the_live_policy_off(tmp_path):
    seen = []

    def run(policy):
        seen.append(policy)
        hg.write_override("synthetic", NOW)

    app = _proxy(tmp_path, True, run)
    async with app.router.lifespan_context(app):
        for _ in range(200):
            if seen and not seen[0].haiku_rewrite:
                break
            await asyncio.sleep(0.01)
    assert seen and seen[0].haiku_rewrite is False


async def test_proxy_with_the_rewrite_off_starts_no_guard(tmp_path):
    seen = []
    app = _proxy(tmp_path, False, lambda p: seen.append(p))
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
    assert seen == []


@pytest.mark.timing
async def test_guard_loop_repeats_on_its_interval_and_survives_a_bad_run():
    calls = []

    def run(policy):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first run fails")

    task = asyncio.create_task(hg.guard_loop(None, interval_s=0.01, run=run))
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 3


# ── the daily watch: llm-router kpi --haiku-watch ───────────────────────────


def _write_ledger(rows):
    led = paths.state_path("proxy_calls.jsonl")
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _iso(ts: float) -> str:
    return hg._stamp(ts)


def test_watch_with_too_little_data_prints_not_evaluable_and_fails_the_day(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_HAIKU_GUARD_AUDIT_DIR", str(tmp_path / "audits"))
    _write_ledger(_retry_ledger(5, 0, NOW))
    code = kpi.cmd_kpi(["--haiku-watch", "--since", _iso(NOW - 86400), "--until", _iso(NOW)])
    out = capsys.readouterr().out
    assert code == 1
    assert "Haiku-decided calls: n=5" in out
    for name in ("tier_retry", "audit_daily", "shadow"):
        assert f"{name} [D-20]: not evaluable (n<min" in out
    assert "day: FAIL (not evaluable: audit_daily, tier_retry, shadow)" in out


def test_watch_with_every_trigger_evaluable_passes_the_day(tmp_path, monkeypatch, capsys):
    audits = tmp_path / "audits"
    monkeypatch.setenv("LLM_ROUTER_HAIKU_GUARD_AUDIT_DIR", str(audits))
    verdicts = tmp_path / "verdicts.jsonl"
    verdicts.write_text("".join(json.dumps(r) + "\n" for r in _verdicts(20, 20)))
    monkeypatch.setenv("LLM_ROUTER_HAIKU_GUARD_SHADOW_VERDICTS", str(verdicts))
    day = hg._utc_day(NOW - 1)
    _summary(audits, day, 9, 10)
    _write_ledger(_retry_ledger(120, 1, NOW))
    code = kpi.cmd_kpi(["--haiku-watch", "--since", _iso(NOW - 86400), "--until", _iso(NOW), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["day_pass"] and data["not_evaluable"] == []
    assert data["haiku_decided_calls"] == 120
    assert data["triggers"]["tier_retry"]["k"] == 1
    assert data["triggers"]["audit_daily"]["control"] == [9, 10]
    assert data["triggers"]["shadow"]["wilson95"][1] == 1.0


def test_watch_needs_an_absolute_window():
    with pytest.raises(SystemExit):
        kpi.cmd_kpi(["--haiku-watch"])
