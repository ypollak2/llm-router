"""P1.7 decision ledger, shadow gate, outcome linking, drift, daily line, prereg (PLAN v16).

Synthetic prompts only. These tests prove MECHANICS (D-42: counts prove mechanics, not accuracy).
Every live criterion (P1.7-a, b, d, e rates at n >= 300 / 100 organic rows) needs organic data and
is NOT proven here; the instruments that will measure them are.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from llm_router import decision_ledger as DL
from llm_router import decision_report as DR
from llm_router import engine, northstar, outcome_link

ROOT = Path(__file__).resolve().parents[1]
SECRET_WORDS = "zebra-quartz-8841 passphrase"  # must never reach a ledger file


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DECIDE_CACHE", "memory")
    for k in ("LLM_ROUTER_DECISION_LEDGER", "LLM_ROUTER_DECISION_SHADOW", "LLM_ROUTER_DECISION_SHADOW_RATE"):
        monkeypatch.delenv(k, raising=False)
    engine.clear_cache(persistent=False)
    yield
    engine.clear_cache(persistent=False)


def _prompts(n: int) -> list[str]:
    rng = random.Random(11)
    verbs = ["explain", "write", "fix", "summarize", "compare", "implement", "refactor", "list"]
    nouns = ["a json parser", "the capital of Portugal", "kafka vs sqs", "this stack trace", "a haiku"]
    return ["%s %s %d %s" % (rng.choice(verbs), rng.choice(nouns), i, SECRET_WORDS) for i in range(n)]


def _rows() -> list[dict]:
    return DL.read_ledger()


# ── P1.7-a / P1.7-e: the row ─────────────────────────────────────────────────

def test_every_decide_writes_one_complete_row_p17_a_and_e():
    texts = _prompts(300)
    for t in texts:
        engine.decide(t, door="hook", session_id="s1", task_id="t1")
    for t in texts[:50]:                      # cache hits are decisions too
        engine.decide(t, door="mcp", session_id="s1")
    rows = _rows()
    assert len(rows) == 350                   # the check found something to check
    assert {r["layer"] for r in rows} >= {"cache"}
    assert all(DL.row_complete(r) for r in rows)
    assert all(DL.row_policy_complete(r) for r in rows)
    assert all(r["policy_id"] == "none" and r["policy_stage"] == "none" and r["grade_basis"] == "none"
               for r in rows)
    assert all(r["policy_version"] and r["engine_version"] == engine.ENGINE_VERSION for r in rows)
    assert len({r["decision_id"] for r in rows}) == 350


def test_row_holds_no_prompt_text_and_the_file_is_0600():
    engine.decide("please summarise " + SECRET_WORDS, door="gateway", session_id="s")
    raw = DL.ledger_path().read_text()
    assert SECRET_WORDS not in raw and "summarise" not in raw
    assert (os.stat(DL.ledger_path()).st_mode & 0o777) == 0o600
    row = _rows()[0]
    assert row["text_sha"] == __import__("llm_router.prompt_key", fromlist=["key"]).key(
        "please summarise " + SECRET_WORDS)


def test_the_completeness_check_rejects_incomplete_rows():
    good = {"layer": "L1", "reason": "x", "confidence": 0.7, "abstain": False}
    assert DL.row_complete(good)
    assert DL.row_complete({**good, "confidence": None, "abstain": True})       # abstain stands in
    assert not DL.row_complete({**good, "confidence": None, "abstain": False})  # neither
    assert not DL.row_complete({**good, "layer": "L9"})
    assert not DL.row_complete({**good, "reason": ""})
    assert not DL.row_complete({**good, "confidence": True})                     # bool is not a number
    assert not DL.row_policy_complete({"policy_id": None, "policy_stage": "none", "grade_basis": "none"})
    assert not DL.row_policy_complete({"policy_id": "p", "policy_stage": "bogus", "grade_basis": "none"})


def test_the_fallback_decision_is_complete_via_abstain():
    d = engine._fallback(RuntimeError("x"), "pv")
    row = DL.build_row(d, door="proxy", text="hello")
    assert row["confidence"] is None and row["abstain"] is True and DL.row_complete(row)


def test_policy_fields_are_written_as_none_never_null_even_for_junk():
    d = engine.decide("write a function", door="proxy")
    row = DL.build_row(d, door="proxy", text="t", policy_id=None, policy_stage="weird", grade_basis=None)
    assert (row["policy_id"], row["policy_stage"], row["grade_basis"]) == ("none", "none", "none")
    row = DL.build_row(d, door="proxy", text="t", policy_id="pol1", policy_stage="shadow",
                       grade_basis="probe", inventory_sig="abc")
    assert (row["policy_id"], row["policy_stage"], row["grade_basis"], row["inventory_sig"]) == (
        "pol1", "shadow", "probe", "abc")


def test_the_ledger_kill_switch_and_a_failed_write_never_stall_decide(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_DECISION_LEDGER", "off")
    engine.decide("a", door="hook")
    assert not DL.ledger_path().exists()
    monkeypatch.setenv("LLM_ROUTER_DECISION_LEDGER", "on")
    blocker = tmp_path / "blocked"
    blocker.write_text("file, not a dir")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(blocker / "sub"))   # mkdir under a file fails
    assert engine.decide("b", door="hook").tier in engine.TIERS


# ── shadow: off by default, gated, 5-10%, tier-stratified ────────────────────

def _gate(passed=True, n=100, added=1.0, free=80, msgs=1800, load=1.0, extra_rows=()) -> dict:
    row = {"fixture": "agentic", "messages": msgs, "off": {"n": n, "p95": 1.0}, "shadow": {"n": n, "p95": 1.0},
           "added_p95_ms": added}
    m = {"free_pct": free, "loadavg": [load, load, load]}
    return {"passed": passed, "rows": [row, *extra_rows], "meta": {"machine_before": dict(m), "machine_after": dict(m)}}


def test_shadow_is_off_by_default_and_every_sampled_flag_is_false():
    assert DL.shadow_enabled() is False
    for t in _prompts(200):
        engine.decide(t, door="hook")
    assert not any(r["shadow"] for r in _rows())


def test_shadow_on_needs_the_p17c_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_DECISION_SHADOW", "on")
    assert DL.shadow_enabled() is False                    # no gate file
    g = DL.gate_path()
    g.write_text(json.dumps(_gate(passed=True, added=9.0)))
    assert DL.shadow_enabled() is False                    # "passed": true edited by hand, bars not met
    g.write_text(json.dumps(_gate(passed=True, n=40)))
    assert DL.shadow_enabled() is False                    # n < 100 per arm
    g.write_text(json.dumps(_gate(passed=True, msgs=600)))
    assert DL.shadow_enabled() is False                    # history < 1,800
    g.write_text(json.dumps(_gate(passed=True, free=10)))
    assert DL.shadow_enabled() is False                    # contended (memory)
    g.write_text(json.dumps(_gate(passed=True, load=9.0)))
    assert DL.shadow_enabled() is False                    # contended (CPU)
    g.write_text(json.dumps(_gate(passed=False)))
    assert DL.shadow_enabled() is False
    g.write_text(json.dumps(_gate(passed=True)))
    assert DL.shadow_enabled() is True
    monkeypatch.setenv("LLM_ROUTER_DECISION_SHADOW", "off")
    assert DL.shadow_enabled() is False                    # a passing gate alone enables nothing


def test_gate_verdict_fails_on_an_empty_or_small_history_set():
    v = DL.gate_verdict({"rows": [], "meta": {}})
    assert v["passed"] is False and v["rows_checked"] == 0
    v = DL.gate_verdict(_gate(msgs=600))
    assert v["passed"] is False and v["rows_checked"] == 0   # nothing at >= 1,800 was checked
    v = DL.gate_verdict(_gate())
    assert v["passed"] is True and v["rows_checked"] == 1
    v = DL.gate_verdict(_gate(added=5.01))
    assert v["passed"] is False
    assert DL.gate_verdict(_gate(added=5.0))["passed"] is True


def test_sampling_rate_is_within_5_to_10_percent_and_stratified_by_tier():
    ids = ["%032x" % i for i in range(40000)]
    for rate in (0.05, 0.10):
        for tier in DL.TIERS:
            share = sum(DL.shadow_sampled(i, tier, rate=rate) for i in ids) / len(ids)
            assert abs(share - rate) < 0.01, (rate, tier, share)
    # out-of-band rates are clamped into 5-10%
    assert abs(sum(DL.shadow_sampled(i, "sonnet", rate=0.9) for i in ids) / len(ids) - 0.10) < 0.01
    assert abs(sum(DL.shadow_sampled(i, "sonnet", rate=0.0) for i in ids) / len(ids) - 0.05) < 0.01
    # the same (id, tier) decides the same way, and the tier is part of the key
    assert DL.shadow_sampled(ids[3], "haiku", rate=0.1) == DL.shadow_sampled(ids[3], "haiku", rate=0.1)
    assert [DL.shadow_sampled(i, "haiku", rate=0.1) for i in ids[:2000]] != [
        DL.shadow_sampled(i, "opus", rate=0.1) for i in ids[:2000]]


def test_rate_env_is_clamped(monkeypatch):
    assert DL.shadow_rate() == DL.RATE_DEFAULT
    for raw, want in (("0.07", 0.07), ("0.5", 0.10), ("0.001", 0.05), ("nan", 0.05), ("junk", 0.05)):
        monkeypatch.setenv("LLM_ROUTER_DECISION_SHADOW_RATE", raw)
        assert DL.shadow_rate() == want


def test_the_private_switch_marks_sampled_rows_at_the_configured_rate(monkeypatch):
    monkeypatch.setattr(DL, "_force_shadow", True)
    monkeypatch.setenv("LLM_ROUTER_DECISION_SHADOW_RATE", "0.10")
    for t in _prompts(3000):
        engine.decide(t, door="hook")
    rows = _rows()
    share = sum(r["shadow"] for r in rows) / len(rows)
    assert len(rows) == 3000 and 0.07 < share < 0.13, share


@pytest.mark.parametrize("value", ["measure", "force", "1.0", "MEASURE", "all"])
def test_no_environment_value_bypasses_the_gate(monkeypatch, value):
    """The old ``measure`` value turned the shadow on with no gate file. No env value may do that."""
    monkeypatch.setenv("LLM_ROUTER_DECISION_SHADOW", value)
    assert not DL.gate_path().exists() and DL.shadow_enabled() is False
    for t in _prompts(300):
        engine.decide(t, door="hook")
    assert not any(r["shadow"] for r in _rows())
    assert DL._force_shadow is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None, "1.0", True])
def test_gate_verdict_rejects_non_finite_and_non_numbers(bad):
    for field in ("added", "n", "free", "load", "p95"):
        g = _gate()
        if field == "added":
            g["rows"][0]["added_p95_ms"] = bad
        elif field == "n":
            g["rows"][0]["off"]["n"] = bad
        elif field == "p95":
            g["rows"][0]["shadow"]["p95"] = bad
        elif field == "free":
            g["meta"]["machine_after"]["free_pct"] = bad
        else:
            g["meta"]["machine_before"]["loadavg"] = [bad, 1, 1]
        v = DL.gate_verdict(g)
        assert v["passed"] is False, (field, bad)
        assert v["rows_checked"] == 1                       # it looked, and said no
    assert DL.gate_verdict(_gate())["passed"] is True       # the clean control passes


def test_nan_in_a_gate_file_does_not_open_the_shadow(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_DECISION_SHADOW", "on")
    g = _gate(passed=True)
    g["rows"][0]["added_p95_ms"] = float("nan")
    DL.gate_path().write_text(json.dumps(g))     # json.dumps writes NaN, json.loads reads it back
    assert DL.shadow_enabled() is False


def test_every_row_at_1800_messages_must_pass_not_just_one():
    bad = {"fixture": "dense", "messages": 1800, "off": {"n": 100, "p95": 1.0},
           "shadow": {"n": 100, "p95": 9.0}, "added_p95_ms": 8.0}
    v = DL.gate_verdict(_gate(extra_rows=[bad]))
    assert v["passed"] is False and v["rows_checked"] == 2 and any("dense@1800" in w for w in v["why"])
    thin = {**bad, "added_p95_ms": 0.1, "off": {"n": 99, "p95": 1.0}}
    assert DL.gate_verdict(_gate(extra_rows=[thin]))["passed"] is False


def test_contention_is_checked_at_the_start_and_at_the_end():
    g = _gate()
    g["meta"]["machine_after"]["loadavg"] = [4.01, 1, 1]
    assert DL.gate_verdict(g)["passed"] is False
    g["meta"]["machine_after"]["loadavg"] = [4.0, 1, 1]
    assert DL.gate_verdict(g)["passed"] is True
    g = _gate()
    g["meta"]["machine_before"].pop("loadavg")
    assert DL.gate_verdict(g)["passed"] is False            # unknown load is not "uncontended"


def test_decisions_jsonl_is_size_capped_with_two_generations(monkeypatch):
    monkeypatch.setattr(DL, "MAX_BYTES", 20_000)
    for t in _prompts(400):
        engine.decide(t, door="hook")
    p = DL.ledger_path()
    assert p.with_name(p.name + ".1").exists()
    assert p.stat().st_size <= 20_000 + 1_000
    rows = DL.read_ledger()
    assert 0 < len(rows) < 400                              # oldest generations dropped: that is the cap
    assert all(DL.row_complete(r) for r in rows)
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_labels_need_a_known_tier_and_a_source():
    assert DL.record_label("d1", "sonnet", "gt500")
    assert not DL.record_label("d1", "gigantic", "gt500")
    assert not DL.record_label("d1", "sonnet", "")
    assert len(DL.read_jsonl(DL.labels_path())) == 1


# ── outcome linking (P1.7-b mechanics) ───────────────────────────────────────

T0 = 1_800_000_000.0


def _dec(i, session="s", ts=T0):
    return {"decision_id": "d%d" % i, "session_id": session, "ts": ts}


def test_outcome_precedence_window_and_pending():
    sigs = outcome_link.signals(
        usage_rows=[{"session_id": "s", "ts": T0 + 60, "outcome": "used"},
                    {"session_id": "s", "ts": T0 + 90, "outcome": "redone"},
                    {"session_id": "other", "ts": T0 + 60, "outcome": "used"},
                    {"session_id": "late", "ts": T0 + 25 * 3600, "outcome": "used"}],
        proxy_rows=[{"session_id": "esc", "ts": T0 + 10, "tier_reason": "escalation"},
                    {"session_id": "esc", "ts": T0 + 20, "tier_reason": "policy"}],
        unit_rows=[{"session_id": "ver", "kind": "llm_edit", "ts": T0 + 5},   # no stored unit_id (real shape)
                   {"unit_id": northstar.unit_id("ver", "llm_edit", T0 + 5),
                    "verify": {"verify_status": "pass_f2p"}},
                   {"unit_id": "u2", "session_id": "vf", "ts": T0 + 5},       # explicit id also accepted
                   {"unit_id": "u2", "verify": {"verify_status": "fail"}}])
    ds = [_dec(1, "s"), _dec(2, "esc"), _dec(3, "ver"), _dec(4, "vf"), _dec(5, "late"), _dec(6, "none"),
          _dec(7, None)]
    # inside the window: only linked ones are written, the rest stay pending
    rows = {r["decision_id"]: r for r in outcome_link.link_decisions(ds, sigs, now=T0 + 3600)}
    assert rows["d1"]["outcome"] == "redone" and rows["d1"]["source"] == "usage_outcomes"  # redone beats used
    assert all(r["link"] == "session" for r in rows.values())
    assert rows["d2"]["outcome"] == "escalated" and rows["d2"]["source"] == "proxy_calls"
    assert rows["d3"]["outcome"] == "success" and rows["d3"]["source"] == "verify"
    assert rows["d4"]["outcome"] == "redone" and rows["d4"]["source"] == "verify"
    assert set(rows) == {"d1", "d2", "d3", "d4"}            # d5, d6, d7 pending: window still open
    # after 24 h the unmatched are unknown; a signal 25 h after the decision is outside the window
    rows = {r["decision_id"]: r for r in outcome_link.link_decisions(ds, sigs, now=T0 + 24 * 3600 + 1)}
    assert rows["d5"]["outcome"] == "unknown" and rows["d6"]["outcome"] == "unknown"
    assert rows["d7"]["outcome"] == "unknown"               # no session id: never guessed
    assert {r["outcome"] for r in rows.values()} <= set(outcome_link.OUTCOMES)


def test_a_signal_before_the_decision_does_not_link():
    sigs = [{"session_id": "s", "ts": T0 - 5, "outcome": "success", "source": "usage_outcomes"}]
    assert outcome_link.link_decisions([_dec(1)], sigs, now=T0 + 3600) == []


def test_link_is_idempotent_and_reads_the_ledgers(tmp_path):
    d = {"decision_id": "dX", "session_id": "s9", "ts": T0, "layer": "L1"}
    DL._append(DL.ledger_path(), d)
    DL._append(tmp_path / "usage_outcomes.jsonl", {"session_id": "s9", "ts": T0 + 30, "outcome": "used"})
    first = outcome_link.link(now=T0 + 100)
    assert first["n_new"] == 1 and first["by_outcome"] == {"success": 1}
    assert outcome_link.link(now=T0 + 200)["n_new"] == 0
    assert len(DL.read_jsonl(outcome_link.outcomes_path())) == 1
    assert outcome_link.link(now=T0 + 300, dry_run=True)["n_new"] == 0


def test_linked_share_counts_unknown_as_not_linked_and_empty_as_not_informative():
    assert outcome_link.linked_share([], [], now=T0)["share"] is None
    ds = [_dec(i) for i in range(10)] + [_dec(99, ts=T0 + 3600)]   # d99 not yet due
    outs = [{"decision_id": "d%d" % i, "outcome": "success"} for i in range(9)] + [
        {"decision_id": "d9", "outcome": "unknown"}]
    s = outcome_link.linked_share(ds, outs, now=T0 + 24 * 3600 + 1)
    assert (s["n_due"], s["n_linked"], s["n_unknown"]) == (10, 9, 1)
    assert s["share"] == 0.9 and s["share_any_row"] == 1.0
    assert s["link_kind"] == "session-level link (upper bound)"


# ── drift ────────────────────────────────────────────────────────────────────

def test_psi_basics():
    assert DR.psi([10, 10, 10], [10, 10, 10]) == pytest.approx(0.0)
    assert DR.psi([10, 10, 10], [20, 20, 20]) == pytest.approx(0.0)    # scale-free
    assert DR.psi([50, 30, 20], [20, 30, 50]) > 0.2
    assert 0 < DR.psi([50, 30, 20], [45, 33, 22]) < 0.1
    assert DR.psi([10, 0, 10], [10, 5, 10]) > 0                         # empty bin: finite
    with pytest.raises(ValueError):
        DR.psi([0, 0], [1, 1])


def _day(offset, n, tiers, conf, base=T0 - (T0 % 86400)):
    rows = []
    for i in range(n):
        rows.append({"ts": base + offset * 86400 + 100 + i, "tier": tiers[i % len(tiers)],
                     "confidence": conf[i % len(conf)], "layer": "L1", "abstain": False})
    return rows


def test_drift_warns_on_a_shifted_day_and_is_uninformative_on_thin_data():
    base_day = (T0 - (T0 % 86400))
    day = __import__("datetime").datetime.fromtimestamp(base_day, __import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    ref = [r for k in range(-7, 0) for r in _day(k, 40, ["haiku", "sonnet", "sonnet", "opus"], [0.9, 0.6, 0.4])]
    steady = _day(0, 60, ["haiku", "sonnet", "sonnet", "opus"], [0.9, 0.6, 0.4])
    shifted = _day(0, 60, ["opus"], [0.2])
    assert DR.drift(ref + steady, day)["warn"] is False
    d = DR.drift(ref + steady, day)
    assert d["informative"] and d["psi_tier"] < 0.05
    d = DR.drift(ref + shifted, day)
    assert d["informative"] and d["warn"] is True and d["psi_tier"] > 0.2 and d["psi_conf"] > 0.2
    thin = DR.drift(ref + _day(0, 5, ["opus"], [0.2]), day)
    assert thin["informative"] is False and thin["warn"] is False and thin["psi_tier"] is None
    assert DR.drift([], day)["informative"] is False


def test_confidence_bins_put_none_in_its_own_bin():
    b = DR.conf_bins([{"confidence": 0.0}, {"confidence": 1.0}, {"confidence": None}, {"confidence": 0.55}])
    assert b[0] == 1 and b[9] == 1 and b[5] == 1 and b[10] == 1 and sum(b) == 4


# ── daily accuracy line, kpi line ────────────────────────────────────────────

def _shadow_day(n_label, agree, under, day_ts=T0):
    decs, labs = [], []
    for i in range(n_label):
        tier = "sonnet"
        ref = "sonnet" if i < agree else ("opus" if i < agree + under else "haiku")
        did = "x%d" % i
        decs.append({"decision_id": did, "ts": day_ts + i, "tier": tier, "shadow": True, "layer": "L1",
                     "abstain": False, "confidence": 0.8})
        labs.append({"decision_id": did, "ref_tier": ref, "source": "gt500" if i % 2 else "judge:haiku"})
    return decs, labs


def test_daily_accuracy_counts_agreement_and_estimated_under_route():
    decs, labs = _shadow_day(40, agree=30, under=6)
    day = __import__("datetime").datetime.fromtimestamp(T0, __import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    a = DR.daily_accuracy(decs, labs, day)
    assert (a["n_labelled"], a["agree"], a["under"]) == (40, 30, 6) and a["informative"]
    assert a["sources"] == {"gt500": 20, "judge": 20}
    assert "agree 30/40" in DR.accuracy_text(a)
    empty = DR.daily_accuracy([], [], day)
    assert empty["n_labelled"] == 0 and "not informative" in DR.accuracy_text(empty)
    few = DR.daily_accuracy(*_shadow_day(5, 5, 0), day)
    assert few["informative"] is False


def test_write_daily_is_once_per_day_and_days_covered_needs_informative_days(tmp_path):
    decs, labs = _shadow_day(40, 30, 6)
    for r in decs:
        DL._append(DL.ledger_path(), r)
    for lb in labs:
        DL._append(DL.labels_path(), lb)
    day = __import__("datetime").datetime.fromtimestamp(T0, __import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    rec = DR.write_daily(day)
    assert rec and "agree 30/40" in rec["line"]
    assert DR.write_daily(day) is None
    daily = DL.read_jsonl(DR.daily_path())
    assert len(daily) == 1
    DR.write_daily("2020-01-01")
    daily = DL.read_jsonl(DR.daily_path())
    days = [day, "2020-01-01", "2020-01-02"]
    assert DR.days_covered(daily, days) == {"window_days": 3, "with_record": 2, "informative": 1}


def test_one_escalation_marks_every_decision_of_the_session():
    """Documents the session-level join: it is an upper bound, and says so."""
    sigs = [{"session_id": "s", "ts": T0 + 100, "outcome": "escalated", "source": "proxy_calls"}]
    ds = [_dec(i, "s", ts=T0 + i) for i in range(5)]
    rows = outcome_link.link_decisions(ds, sigs, now=T0 + 3600)
    assert [r["outcome"] for r in rows] == ["escalated"] * 5


def test_backfill_writes_seven_days_once_and_empty_days_as_n0(tmp_path):
    from datetime import datetime, timezone

    now = T0 + 86400 * 3
    written = DR.backfill_daily(7, now=now)
    assert len(written) == 7
    daily = DL.read_jsonl(DR.daily_path())
    assert len(daily) == 7 and all(r["accuracy"]["n_labelled"] == 0 for r in daily)
    assert all("not informative" in r["line"] for r in daily)
    assert DR.backfill_daily(7, now=now) == []               # idempotent
    assert len(DR.backfill_daily(7, now=now + 86400)) == 1   # next day adds just the new day
    days = [r["day"] for r in DL.read_jsonl(DR.daily_path())][:7]
    assert DR.days_covered(DL.read_jsonl(DR.daily_path()), days) == {
        "window_days": 7, "with_record": 7, "informative": 0}  # records exist, none informative
    assert datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d") not in days  # today is not complete


def test_classifier_line_reports_n_and_none_when_empty():
    assert DR.classifier_line([], [], [], now=T0, engine_version="engine/1") is None
    decs, labs = _shadow_day(10, 8, 1)
    line = DR.classifier_line(decs, labs, [], now=T0 + 3600, engine_version="engine/1")
    assert "n=10" in line and "abstain 0.0%" in line and "shadow agree 8/10" in line
    assert "GT held-out: none stored for engine/1" in line and "0 due" in line
    assert "session-level link (upper bound)" in line
    line = DR.classifier_line(decs, [], [], now=T0 + 3600, engine_version="engine/1")
    assert "n/a (0 labelled)" in line
    line = DR.classifier_line(decs, [], [], now=T0 + 3600, engine_version="engine/1",
                              heldout={"engine_version": "engine/1", "summary": "exact 0.5", "n": 234})
    assert "GT held-out exact 0.5 (n=234, stored)" in line
    line = DR.classifier_line(decs, [], [], now=T0 + 3600, engine_version="engine/2",
                              heldout={"engine_version": "engine/1", "summary": "exact 0.5", "n": 234})
    assert "none stored for engine/2" in line


def test_kpi_renders_the_classifier_line_from_the_state_dir():
    from llm_router.commands import kpi

    assert kpi._decision_ledger_line() is None
    engine.decide("explain this", door="hook")
    line = kpi._decision_ledger_line()
    assert line and line.startswith("classifier [%s]" % engine.ENGINE_VERSION) and "live 7d n=1" in line


# ── columns: routing_decisions and the proxy ledger ──────────────────────────

@pytest.mark.asyncio
async def test_routing_decisions_gets_the_columns_and_policy_defaults_to_none(temp_db):
    from llm_router import cost

    kw = dict(prompt="hash-this", task_type="code", profile="balanced", classifier_type="heuristic",
              classifier_model=None, classifier_confidence=0.9, classifier_latency_ms=0.0,
              complexity="moderate", recommended_model="anthropic/claude-sonnet-4-6",
              base_model="anthropic/claude-sonnet-4-6", was_downshifted=False, budget_pct_used=0.1,
              quality_mode="balanced", final_model="anthropic/claude-sonnet-4-6",
              final_provider="anthropic", success=True, input_tokens=1, output_tokens=1, cost_usd=0.0,
              latency_ms=1.0)
    await cost.log_routing_decision(**kw)
    await cost.log_routing_decision(**kw, layer="L3", confidence=0.7, reason="L3 lexical", abstain=False,
                                    engine_version="engine/1", policy_id="p1", policy_stage="shadow",
                                    grade_basis="probe", inventory_sig="sig")
    db = await cost._get_db()
    try:
        async with db.execute("SELECT layer, confidence, reason, abstain, engine_version, policy_id, "
                              "policy_stage, grade_basis, inventory_sig FROM routing_decisions "
                              "ORDER BY id") as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    assert len(rows) == 2
    assert tuple(rows[0]) == (None, None, None, None, None, "none", "none", "none", None)
    assert tuple(rows[1]) == ("L3", 0.7, "L3 lexical", 0, "engine/1", "p1", "shadow", "probe", "sig")


def test_proxy_ledger_rows_carry_cls_and_policy_fields(tmp_path):
    from llm_router.proxy import ledger

    p = tmp_path / "pc.jsonl"
    ledger.write_row({"ts": 1}, p)
    ledger.write_row({"ts": 2, "cls_layer": "L1", "cls_confidence": 0.6, "policy_id": "pol"}, p)
    a, b = [json.loads(x) for x in p.read_text().splitlines()]
    for k in ("cls_layer", "cls_confidence", "cls_reason", "inventory_sig"):
        assert k in a and a[k] is None
    assert (a["policy_id"], a["policy_stage"], a["grade_basis"]) == ("none", "none", "none")
    assert (b["cls_layer"], b["cls_confidence"], b["policy_id"]) == ("L1", 0.6, "pol")


# ── P1.7-d prereg and the P1.7-c harness ─────────────────────────────────────

def test_prereg_hash_matches_and_names_the_fixed_seed():
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_prereg
    from llm_router import shadow_eval

    md = ROOT / "docs" / "prereg" / "PREREG-shadowlab.md"
    res = check_prereg.check(md)
    assert res["match"] is True and res["sha256"] == hashlib.sha256(md.read_bytes()).hexdigest()
    text = md.read_text()
    assert str(shadow_eval.BOOT_SEED) in text and "B = 2,000" in text and str(shadow_eval.MIN_N) in text
    assert hashlib.sha256(text.encode() + b" ").hexdigest() != res["recorded"]


def test_prereg_check_counts_rows_before_and_after_the_freeze(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_prereg

    md = tmp_path / "PREREG-x.md"
    md.write_text("body")
    md.with_suffix(".sha256").write_text("%s  PREREG-x.md\nfrozen_utc 2026-10-11T12:00:00Z\n"
                                         % hashlib.sha256(b"body").hexdigest())
    log = tmp_path / "s.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in [
        {"kind": "classifier_shadow", "ts": "2026-10-11T11:59:59Z"},
        {"kind": "classifier_shadow", "ts": "2026-10-11T12:00:01Z"},
        {"kind": "classifier_shadow_drop", "ts": "2026-10-11T13:00:00Z"}]) + "\nnot json\n")
    r = check_prereg.check(md, log)
    assert (r["before_freeze"], r["after_freeze"], r["match"]) == (1, 1, True)
    md.write_text("body edited")
    assert check_prereg.check(md)["match"] is False


def _run_harness(tmp_path, extra, *, loadavg=None):
    """Run the P1.7-c harness in a subprocess. Returns (proc, out_path, gate_home)."""
    home = tmp_path / "gatehome"
    home.mkdir()
    env = {**os.environ, "HOME": str(tmp_path), "LLM_ROUTER_HOME": str(home),
           "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)])}
    out = tmp_path / "res.json"
    script = str(ROOT / "scripts" / "bench_shadow_continuation.py")
    args = ["--n", "3", "--warmup", "1", "--sizes", "0", "--fixtures", "one_prompt",
            "--out", str(out), "--gate", *extra]
    if loadavg is None:
        cmd = [sys.executable, script, *args]
    else:  # fake a loaded machine: patch getloadavg, then run the script as __main__
        cmd = [sys.executable, "-c",
               "import os,runpy,sys;os.getloadavg=lambda:(%r,%r,%r);sys.argv=[%r]+sys.argv[1:];"
               "runpy.run_path(%r,run_name='__main__')" % (loadavg, loadavg, loadavg, script, script),
               *args]
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    return p, out, home


def _diag(p):
    return "rc=%s\nSTDOUT:\n%s\nSTDERR:\n%s" % (p.returncode, p.stdout[-1500:], p.stderr[-1500:])


def test_harness_runs_and_refuses_to_pass_a_tiny_run(tmp_path):
    """Smoke only: n = 3 per arm at 0 messages. It must produce rows and a FAILING verdict
    (nothing at >= 1,800 messages was checked), and must not write a gate file. Exit 1 means
    "verdict failed" ONLY: a harness error exits 2 (an uncaught exception used to exit 1 too and
    leave no out-file, which made a CI flake look like a verdict failure)."""
    p, out, home = _run_harness(tmp_path, [])
    assert p.returncode == 1, _diag(p)
    assert out.exists(), "exit 1 but no out-file written\n" + _diag(p)
    res = json.loads(out.read_text())
    assert res["rows"] and res["rows"][0]["off"]["n"] == 3 and res["rows"][0]["shadow"]["n"] == 3
    assert res["verdict"]["passed"] is False and res["verdict"]["rows_checked"] == 0
    assert res["meta"]["decision_rows"] == 8 and res["meta"]["decision_rows_shadow"] >= 0
    assert not (home / DL.GATE_FILENAME).exists()
    assert "gate NOT written" in p.stdout


def test_harness_on_a_loaded_machine_fails_the_verdict_and_writes_out(tmp_path):
    """Contention is a failing verdict (exit 1, out written, reason names the load), not a crash."""
    p, out, home = _run_harness(tmp_path, [], loadavg=99.0)
    assert p.returncode == 1, _diag(p)
    res = json.loads(out.read_text())
    assert res["rows"] and res["verdict"]["passed"] is False
    assert any("load1" in w for w in res["verdict"]["why"]), res["verdict"]
    assert not (home / DL.GATE_FILENAME).exists()


def test_harness_error_exits_2_writes_out_and_no_gate(tmp_path):
    """A harness exception (here an unknown fixture) is exit 2 with a reason, never exit 1."""
    p, out, home = _run_harness(tmp_path, ["--fixtures", "no_such_fixture"])
    assert p.returncode == 2, _diag(p)
    res = json.loads(out.read_text())
    assert res["verdict"]["passed"] is False and "harness error" in res["verdict"]["reason"]
    assert "no_such_fixture" in res["verdict"]["reason"]
    assert not (home / DL.GATE_FILENAME).exists()
