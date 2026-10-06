"""O3 offload share: the definition on fixtures, the other KPIs untouched, and unknown
never rendered as a number. Definition: docs/repo_goals/KPIS.md (O3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_router import offload_share as osh
from llm_router import paths, session_kind, user_signal
from llm_router.commands import kpi

from tests import _o3_fixture as fx
from tests._o3_fixture import NOW, proxy_row

ORG = frozenset({"organic"})
GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    # G2 reads process-global fail-open counts another test on this worker may have left
    # behind (CI 3.13 failure on e29c34f); the golden needs it to start from nothing.
    from llm_router import failopen

    failopen.reset_unpersisted()
    failopen.reset_cache()
    session_kind._FOUND.clear()
    session_kind.tag_session("s-org", "/Users/someone/Projects/app", env={})
    yield
    session_kind._FOUND.clear()
    failopen.reset_unpersisted()
    failopen.reset_cache()


def _build(rows, local=(), band=frozenset(), outcomes=()):
    return osh.build_units(rows, local, now=NOW, days=7, allowed=ORG, band_redone=band,
                           outcome_redos=outcomes,
                           kind_of=lambda sid, stamp: stamp)


def _u(rows, **kw):
    return _build(rows, **kw)["units"]


def _conv(sid, haiku_ts, *, escalation_after_turns=None, extra_turns=3, t0=None):
    """One conversation: a Haiku call (human turn 1), a continuation, then human turns
    2..; the escalation row (opus, tier_reason=escalation) is the first call of human turn
    ``escalation_after_turns + 1``."""
    t0 = haiku_ts if t0 is None else t0
    rows = [proxy_row(0, sid=sid, tier="haiku", ts=t0, kind="organic"),
            proxy_row(1, sid=sid, tier="haiku", step="continuation", ts=t0 + 1, kind="organic")]
    for k in range(1, extra_turns + 1):
        esc = escalation_after_turns == k
        rows.append(proxy_row(10 + k, sid=sid, tier="opus" if esc else "sonnet",
                              reason="escalation" if esc else "policy", ts=t0 + 100 * k, kind="organic"))
    return rows


# ── the definition ───────────────────────────────────────────────────────────

def test_escalation_within_next_two_human_turns_is_a_redo():
    for k, expect in ((1, True), (2, True), (3, False)):
        units = _u(_conv(f"s{k}", NOW - 5000, escalation_after_turns=k, extra_turns=4))
        haiku = [u for u in units if u["class"] == "haiku"]
        assert len(haiku) == 2 and all(u["redone"] is expect for u in haiku), k


def test_no_escalation_is_not_a_redo_and_closed_window_is_not_open():
    haiku = [u for u in _u(_conv("s", NOW - 5000, extra_turns=3)) if u["class"] == "haiku"]
    assert [u["redone"] for u in haiku] == [False, False]
    assert [u["window_open"] for u in haiku] == [False, False]


def test_recent_unit_with_no_redo_is_counted_but_flagged_window_open():
    haiku = [u for u in _u(_conv("s", NOW - 50, extra_turns=1)) if u["class"] == "haiku"]
    assert [u["redone"] for u in haiku] == [False, False] and all(u["window_open"] for u in haiku)


def test_escalation_in_another_session_does_not_redo_this_unit():
    rows = _conv("sA", NOW - 5000) + _conv("sB", NOW - 5000, escalation_after_turns=1)
    a = [u for u in _u(rows) if u["session_id"] == "sA" and u["class"] == "haiku"]
    assert a and not any(u["redone"] for u in a)


def test_receipt_band_redo_marks_the_unit_by_msg_id():
    rows = _conv("s", NOW - 5000)
    units = _u(rows, band={rows[0]["msg_id"]})
    assert [(u["class"], u["why"]) for u in units if u["class"] == "haiku"] == [
        ("haiku", "receipt_band"), ("haiku", None)]


def test_usage_outcome_redo_marks_the_nearest_local_unit_once():
    local = [{"session_id": "s-org", "ts": "2026-10-06T14:00:00+00:00", "kind": "local_shadow"}] * 2
    t = 1791295200.0  # 2026-10-06T12:40Z is not it: use the parsed value below
    from datetime import datetime
    t = datetime.fromisoformat(local[0]["ts"]).timestamp()
    out = [{"outcome": "redone", "session_id": "s-org", "ts": t + 30},
           {"outcome": "used", "session_id": "s-org", "ts": t + 1},
           {"outcome": "redone", "session_id": "other", "ts": t}]
    units = osh.build_units([], local, now=t + 3600, days=7, allowed=ORG, band_redone=frozenset(),
                            outcome_redos=out, kind_of=lambda sid, stamp: "organic")["units"]
    assert [u["why"] for u in units] == ["usage_outcome", None]  # one verdict, one unit


def test_what_is_and_is_not_a_unit():
    rows = [proxy_row(1, tier="haiku", kind="organic"),                                  # haiku
            proxy_row(2, tier="sonnet", kind="organic"),                                 # claude
            proxy_row(3, tier="haiku", reason="side_call", kind="organic"),              # side call
            proxy_row(4, tier="haiku", kind="organic", upstream_status=529),             # failed
            proxy_row(5, tier=None, model=None, kind="organic", decision="served"),      # local
            proxy_row(6, tier="haiku", kind="research"),                                 # other kind
            proxy_row(7, tier="haiku", kind=None),                                       # untagged
            proxy_row(8, tier="haiku", kind="organic", ts=NOW - 8 * 86400),              # out of window
            proxy_row(9, tier="sonnet", kind="organic", decision="rejected")]            # not a unit
    built = _build(rows)
    assert sorted(u["class"] for u in built["units"]) == ["claude", "haiku", "local"]
    assert (built["side_call_excluded"], built["other_kind"], built["untagged"]) == (1, 1, 1)


def test_local_unit_without_session_id_is_excluded_and_counted():
    built = _build([], local=[{"session_id": None, "ts": "2026-10-06T14:00:00+00:00"}])
    assert built["units"] == [] and built["local_no_session"] == 1


# ── the number, through the scorecard ───────────────────────────────────────

def _write_ledger(rows):
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), rows)


def _population(n_haiku, haiku_redone, n_local, n_claude, *, pv="v-new", t0=NOW - 86400):
    """Each conversation: one Haiku call, then 3 sonnet human turns (a closed window); the first
    ``haiku_redone`` have a `claude:` escalation (opus) as their next human turn instead."""
    rows, i = [], 0
    for h in range(n_haiku):
        rows += [proxy_row(0, sid=f"h{h}", tier="haiku", ts=t0 + i, kind="organic", pv=pv)]
        for k in (1, 2, 3):
            esc = h < haiku_redone and k == 1
            rows.append(proxy_row(k, sid=f"h{h}", tier="opus" if esc else "sonnet",
                                  reason="escalation" if esc else "policy", ts=t0 + i + 10 * k,
                                  kind="organic", pv=pv))
        i += 100
    rows += [proxy_row(900 + j, sid=f"l{j}", decision="served", tier=None, model="local", ts=t0 + j,
                       kind="organic", pv=pv) for j in range(n_local)]
    rows += [proxy_row(800 + j, sid=f"c{j}", tier="sonnet", ts=t0 + j, kind="organic", pv=pv)
             for j in range(n_claude)]
    return rows


def test_o3_value_on_a_fixture_with_known_answer():
    # 60 haiku (6 redone) + 10 local + 80 claude + (6 escalation opus + 3*60-... see below)
    rows = _population(n_haiku=60, haiku_redone=6, n_local=10, n_claude=80)
    _write_ledger(rows)
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    units = [r for r in rows if r["tier_reason"] != "side_call"]
    n = len(units)                      # every non-side-call row is a unit: haiku, sonnet, opus, local
    kept = (60 - 6) + 10                # haiku not redone + local
    assert o3["measurable"] and o3["n"] == n
    assert o3["value"] == f"{kept / n * 100:.1f}% (n={n})"
    b = o3["breakdown"]
    assert (b["haiku_n"], b["haiku_redone"], b["local_n"]) == (60, 6, 10)
    assert "Haiku 10.0% (n=60)" in o3["lines"][1]
    assert "local too few to tell (n=10)" in o3["lines"][1]


def test_o3_blind_below_min_n_never_prints_a_percentage():
    _write_ledger(_population(n_haiku=5, haiku_redone=5, n_local=0, n_claude=3))
    card = kpi.compute_scorecard(days=7, now=NOW)
    o3 = card["o3"]
    assert not o3["measurable"] and "too few to tell" in o3["value"] and "%" not in o3["value"]
    assert all("%" not in ln for ln in o3["lines"])
    health = kpi.compute_health(card)["o3"]
    assert health["state"] == "blind"
    assert "O3" in kpi.render_scorecard(card) and "O3" in kpi.render_health(kpi.compute_health(card))


def test_o3_with_no_units_is_not_measurable_not_zero():
    _write_ledger([])
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    assert o3["value"].startswith("not measurable") and "0%" not in o3["value"]


def test_per_class_redo_rate_is_too_few_when_the_class_is_small_even_if_total_is_large():
    _write_ledger(_population(n_haiku=3, haiku_redone=3, n_local=0, n_claude=200))
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    assert o3["measurable"]
    assert "Haiku too few to tell (n=3)" in o3["lines"][1]
    assert "local not measurable" in o3["lines"][1]


def test_local_share_is_unknown_not_zero_when_local_units_have_no_session(monkeypatch):
    from llm_router import northstar as ns
    _write_ledger(_population(n_haiku=0, haiku_redone=0, n_local=0, n_claude=100))
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None: iter(
        [{"session_id": None, "ts": "2026-10-06T14:00:00+00:00"}] * 7))
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    assert "local share unknown (7 local unit(s) carry no session id" in o3["lines"][0]
    assert "local share 0.0%" not in o3["lines"][0]


def test_receipt_band_press_reaches_o3_through_the_scorecard():
    rows = _population(n_haiku=60, haiku_redone=0, n_local=0, n_claude=40)
    _write_ledger(rows)
    base = kpi.compute_scorecard(days=7, now=NOW)["o3"]["breakdown"]["haiku_redone"]
    user_signal.record(rows[0]["msg_id"], "redone", "terminal", now=NOW - 10)
    after = kpi.compute_scorecard(days=7, now=NOW)["o3"]["breakdown"]["haiku_redone"]
    assert (base, after) == (0, 1)


# ── since a policy version ───────────────────────────────────────────────────

def test_since_policy_splits_before_and_after_and_reports_unseen_version():
    old = _population(60, 30, 0, 0, pv="v-old", t0=NOW - 4 * 86400)
    new = _population(60, 0, 0, 0, pv="v-new", t0=NOW - 86400)
    _write_ledger(old + new)
    sp = kpi.compute_scorecard(days=7, now=NOW, since_policy="v-new")["o3"]["since_policy"]
    assert sp["version"] == "v-new" and sp["start_ts"] == min(r["ts"] for r in new)
    assert sp["since"]["breakdown"]["haiku_n"] == 60 and sp["since"]["breakdown"]["haiku_redone"] == 0
    assert sp["before"]["breakdown"]["haiku_n"] == 60 and sp["before"]["breakdown"]["haiku_redone"] == 30
    text = kpi.render_scorecard(kpi.compute_scorecard(days=7, now=NOW, since_policy="v-new"))
    assert "since policy v-new" in text and "before it, same window" in text
    missing = kpi.compute_scorecard(days=7, now=NOW, since_policy="nope")["o3"]["since_policy"]
    assert missing["since"]["value"] == "not measurable: policy version nope not seen in the proxy ledger"


# ── the other KPIs are untouched ─────────────────────────────────────────────

def _golden_card():
    _write_ledger(fx.baseline_rows())
    return kpi.compute_scorecard(days=7, now=NOW)


def test_other_kpis_are_byte_identical_to_the_pre_o3_golden():
    card = _golden_card()
    assert card["o3"] is not None  # O3 ran, and still moved nothing below
    assert json.dumps(card["kpis"], indent=1, sort_keys=True, default=str) == \
        (GOLDEN / "kpi_pre_o3_kpis.json").read_text()
    o3_lines = set(kpi._o3_render_lines(card["o3"]))
    full = kpi.render_scorecard(card).split("\n")
    assert o3_lines and o3_lines <= set(full)
    assert "\n".join(ln for ln in full if ln not in o3_lines) == \
        (GOLDEN / "kpi_pre_o3_scorecard.txt").read_text()
    health = kpi.compute_health(card)
    h_lines = kpi.render_health(health).split("\n")
    h_o3 = [ln for ln in h_lines if ln.startswith(f"  {kpi._LABELS['O3']}")]
    assert len(h_o3) == 1
    assert "\n".join(ln for ln in h_lines if ln not in h_o3) == \
        (GOLDEN / "kpi_pre_o3_health.txt").read_text()
    assert list(health["kpis"]) == list(kpi._ORDER) and sum(health["counts"].values()) == len(kpi._ORDER)


def test_o3_failure_cannot_break_the_scorecard(monkeypatch):
    monkeypatch.setattr(osh, "build_units", lambda *a, **k: 1 / 0)
    card = _golden_card()
    assert card["o3"]["value"].startswith("not measurable: O3 computation failed")
    assert json.dumps(card["kpis"], indent=1, sort_keys=True, default=str) == \
        (GOLDEN / "kpi_pre_o3_kpis.json").read_text()
