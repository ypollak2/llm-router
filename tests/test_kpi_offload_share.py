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
    built = osh.build_units([], local, now=t + 3600, days=7, allowed=ORG, band_redone=frozenset(),
                            outcome_redos=out, kind_of=lambda sid, stamp: "organic")
    assert built["units"] == []   # M0.3a: a local MCP unit is not a turn
    assert [u["why"] for u in built["local_assist"]] == ["usage_outcome", None]  # one verdict, one unit


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
    assert o3["value"].startswith(f"{kept / n * 100:.1f}% (n={n} turns, ")
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


def test_mcp_local_units_without_session_are_reported_apart_not_as_a_lower_bound(monkeypatch):
    # M0.3a (deliberate update of the pinned test): MCP-local units are not turns, so they no
    # longer make the O3 headline a lower bound; they get their own line.
    from llm_router import northstar as ns
    _write_ledger(_population(n_haiku=0, haiku_redone=0, n_local=0, n_claude=100))
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None: iter(
        [{"session_id": None, "ts": "2026-10-06T14:00:00+00:00"}] * 7))
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    assert "lower bound" not in o3["lines"][0] and "local share 0.0% (0/100)" in o3["lines"][0]
    assert "local MCP answers inside Claude turns" in "".join(o3["lines"])
    assert "7 more with no session id excluded" in "".join(o3["lines"])
    assert o3["breakdown"]["local_assist_n"] == 0 and o3["breakdown"]["local_no_session"] == 7


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
    # P0.14-a: the proxy ledger liveness line is new and time-dependent, so it is not in the golden.
    full = [ln for ln in full if not ln.startswith(("proxy_rows_24h:", "WARN proxy ledger"))]
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


# ── human turn is the headline unit; per call is secondary ───────────────────

def _agent_loop_rows(n_turns, calls_per_turn, *, first_tier, pv="v-new", t0=NOW - 86400, prefix="a"):
    """Each turn: a first call on `first_tier`, then (calls_per_turn - 1) Sonnet continuations."""
    rows = []
    for i in range(n_turns):
        sid, t = f"{prefix}{i}", t0 + i * 1000
        rows.append(proxy_row(0, sid=sid, tier=first_tier, ts=t, kind="organic", pv=pv))
        rows += [proxy_row(k, sid=sid, tier="sonnet", step="continuation", ts=t + k, kind="organic", pv=pv)
                 for k in range(1, calls_per_turn)]
        for k in (1, 2, 3):  # three later human turns close the window
            rows.append(proxy_row(50 + k, sid=sid, tier="sonnet", ts=t + 100 * k, kind="organic", pv=pv))
    return rows


def test_headline_counts_turns_not_calls():
    # 60 Haiku-first turns with 9 calls each, 60 Sonnet-first turns with 1 call each.
    rows = _agent_loop_rows(60, 9, first_tier="haiku") + _agent_loop_rows(60, 1, first_tier="sonnet", prefix="b")
    _write_ledger(rows)
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    b = o3["breakdown"]
    # turns: per Haiku conv 1 haiku + 3 sonnet turns; per Sonnet conv 4 sonnet turns
    assert b["unit"] == "human_turn" and b["n"] == 60 * 4 + 60 * 4 and b["haiku_n"] == 60
    assert o3["value"].startswith(f"{60 / 480 * 100:.1f}% (n=480 turns")
    # per call: 60 haiku-first convs contribute 9 haiku-or-sonnet calls but only the FIRST is
    # Haiku (continuations are Sonnet here), so the call-level share differs from the turn-level one
    pc = b["per_call"]
    assert pc["n"] > b["n"] and pc["offload_kept"] == 60
    assert any(ln.startswith("per call: ") and "calls per turn" in ln for ln in o3["lines"])


def test_continuation_served_by_haiku_does_not_make_a_turn_offloaded():
    rows = []
    for i in range(60):  # first call Sonnet, then 8 Haiku continuations: the TURN is not offloaded
        sid, t = f"c{i}", NOW - 86400 + i * 1000
        rows.append(proxy_row(0, sid=sid, tier="sonnet", ts=t, kind="organic"))
        rows += [proxy_row(k, sid=sid, tier="haiku", step="continuation", ts=t + k, kind="organic")
                 for k in range(1, 9)]
    _write_ledger(rows)
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    assert o3["breakdown"]["offload_kept"] == 0 and o3["breakdown"]["per_call"]["offload_kept"] == 480


# ── redo signal ──────────────────────────────────────────────────────────────

def test_escalation_under_pressure_is_a_redo_reason():
    rows = _conv("s", NOW - 5000, escalation_after_turns=1)
    for r in rows:
        if r["tier_reason"] == "escalation":
            r["tier_reason"] = "escalation_under_pressure"
    assert all(u["redone"] for u in _u(rows) if u["class"] == "haiku")
    assert "escalation_under_pressure" in osh.ESCALATION_REASONS and "escalation" in osh.ESCALATION_REASONS


def test_redo_signal_sparse_is_stated_with_n_escalations():
    rows = _population(n_haiku=60, haiku_redone=1, n_local=0, n_claude=40)
    _write_ledger(rows)
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    assert o3["breakdown"]["n_escalations"] == 1
    assert any(ln.startswith("redo signal sparse: n_escalations=1") and "NOT proven low" in ln
               for ln in o3["lines"])


# ── caveats ──────────────────────────────────────────────────────────────────

def test_partly_attributable_local_still_prints_the_no_session_caveat(monkeypatch):
    # M0.3a (deliberate update of the pinned test): the caveat moved from the headline line to the
    # "local MCP answers inside Claude turns" line, and the 3 attributable units are not turns.
    from llm_router import northstar as ns
    _write_ledger(_population(n_haiku=0, haiku_redone=0, n_local=0, n_claude=100))
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None: iter(
        [{"session_id": "s-org", "ts": "2026-10-06T14:00:00+00:00"}] * 3
        + [{"session_id": None, "ts": "2026-10-06T14:00:00+00:00"}] * 4))
    o3 = kpi.compute_scorecard(days=7, now=NOW, since_policy="v-new")["o3"]
    joined = "".join(o3["lines"])
    assert "3 (0 redone); 4 more with no session id excluded" in joined
    assert o3["breakdown"]["local_assist_n"] == 3 and o3["breakdown"]["n"] == 100
    assert "4 more with no session id excluded" in "".join(o3["since_policy"]["since"].get("lines", ()))


def test_window_open_turns_are_in_the_headline_value():
    rows = _population(n_haiku=60, haiku_redone=0, n_local=0, n_claude=0)
    # every conversation ends right after the Haiku call: the session ended, windows are open
    short = [r for r in rows if r["tier"] == "haiku"]
    _write_ledger(short)
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    assert o3["measurable"] and ", 60 window-open)" in o3["value"]


# ── M0-2: the O3 integrity warning (owner decision 2026-10-07) ───────────────

_CAVEAT = ("O3 turn count unvalidated: may overcount turns (integrity 3.89x on the only "
           "measurable session, research; owner accepted 2026-10-07)")


def test_the_caveat_text_is_the_owner_wording_built_from_the_gate_constant():
    assert kpi.O3_INTEGRITY_GATE["turns"] == 813 and kpi.O3_INTEGRITY_GATE["typed_prompts"] == 209
    assert kpi.o3_caveat() == _CAVEAT


def test_the_caveat_follows_the_constant_not_a_string_in_each_surface(monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(kpi.O3_INTEGRITY_GATE, "turns", 100)
    monkeypatch.setitem(kpi.O3_INTEGRITY_GATE, "typed_prompts", 100)
    new = _CAVEAT.replace("3.89x", "1.00x")
    card = _golden_card()
    assert card["o3"]["caveat"] == new and card["o3"]["integrity"]["ratio"] == 1.0
    assert new in kpi.render_scorecard(card)
    assert new in kpi.render_health(kpi.compute_health(card))
    assert "3.89" not in kpi.render_scorecard(card) + kpi.render_health(kpi.compute_health(card))


def test_the_caveat_scope_follows_sessions_measurable(monkeypatch):
    assert "on the only measurable session" in kpi.o3_caveat()
    monkeypatch.setitem(kpi.O3_INTEGRITY_GATE, "sessions_measurable", 3)
    text = kpi.o3_caveat()
    assert "across 3 measurable sessions" in text and "only measurable session" not in text
    assert "3.89x" in text


def test_json_carries_the_caveat_and_the_measured_ratio(capsys):
    _write_ledger(fx.baseline_rows())
    assert kpi.cmd_kpi(["--json"]) == 0
    o3 = json.loads(capsys.readouterr().out)["o3"]
    assert o3["caveat"] == _CAVEAT
    assert o3["integrity"] == {**kpi.O3_INTEGRITY_GATE, "ratio": 3.89}
    assert o3["integrity"]["status"] == "owner-accepted with warning" and o3["integrity"]["gate"] == "M0-2"


def test_scorecard_prints_the_caveat_under_the_o3_line(capsys):
    _write_ledger(fx.baseline_rows())
    assert kpi.cmd_kpi([]) == 0
    lines = capsys.readouterr().out.split("\n")
    i = next(k for k, ln in enumerate(lines) if ln.startswith(f"  {kpi._LABELS['O3']}"))
    assert lines[i + 1].strip() == f"WARNING: {_CAVEAT}"


def test_health_prints_the_caveat_on_the_o3_line_and_in_json(capsys):
    _write_ledger(fx.baseline_rows())
    assert kpi.cmd_kpi(["--health"]) == 0
    o3_line = [ln for ln in capsys.readouterr().out.split("\n")
               if ln.startswith(f"  {kpi._LABELS['O3']}")]
    assert len(o3_line) == 1 and o3_line[0].endswith(f"| WARNING: {_CAVEAT}")
    assert kpi.cmd_kpi(["--health", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["o3"]["caveat"] == _CAVEAT


def test_the_caveat_is_there_when_o3_is_blind_or_failed(monkeypatch):
    _write_ledger(_population(n_haiku=5, haiku_redone=5, n_local=0, n_claude=3))
    card = kpi.compute_scorecard(days=7, now=NOW)
    assert not card["o3"]["measurable"] and card["o3"]["caveat"] == _CAVEAT
    assert _CAVEAT in kpi.render_health(kpi.compute_health(card))
    monkeypatch.setattr(osh, "build_units", lambda *a, **k: 1 / 0)
    failed = _golden_card()["o3"]
    assert failed["value"].startswith("not measurable: O3 computation failed") and failed["caveat"] == _CAVEAT


def test_the_weekly_markdown_carries_the_caveat(tmp_path):
    path = kpi.write_weekly(_golden_card(), tmp_path)
    row = next(ln for ln in path.read_text().split("\n") if ln.startswith(f"| {kpi._LABELS['O3']}"))
    assert f"(WARNING: {_CAVEAT})" in row


def test_the_caveat_does_not_move_the_other_kpis():
    card = _golden_card()
    assert json.dumps(card["kpis"], indent=1, sort_keys=True, default=str) == \
        (GOLDEN / "kpi_pre_o3_kpis.json").read_text()
