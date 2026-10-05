"""KPI definitions and joins: G3 counted where a field applies, the session tag joined
onto north-star units, D4 weighted by cost, and ``kpi --health``.

Primary KPI: G3 (ledger completeness); enables NS/D1/D2 (they cannot be read without
the join). Guardrail: G2.

The numbers these tests pin come from the real ledger of 2026-10-04: the old G3 read
0.4% (n=2,214) on data that was near-complete, NS/D1/D2 had no per-unit tag (their
"not measurable" text counted only the untagged units and skipped the tagged ones
silently), D4 counted calls only. On that machine today the join adds nothing to the
NS/D1/D2 VALUES (one session is tagged; the old code already read its tag file): what
it adds is the stamp on every unit, the proxy-rows fallback, and the joined/untagged
accounting. Every behavioural test builds the inputs the fix has to handle (side
calls, pre-schema rows, null fields, untagged units) and asserts the REASON, not just
a number. The rule they share is the repo's: an empty or all-excluded set reports
"not measurable", never 0% or 100%.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_router import northstar as ns
from llm_router import paths, session_kind, usage_outcome
from llm_router.commands import kpi
from llm_router.proxy import ledger as pl
from llm_router.proxy import server as ps
from llm_router.proxy import tiers as pt

REPO = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000.0           # a fixed "now"; every ledger fixture is placed relative to it
DAY = 86400.0


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    from llm_router import failopen

    failopen.reset_unpersisted()
    failopen.reset_cache()
    session_kind._FOUND.clear()
    yield
    session_kind._FOUND.clear()
    failopen.reset_unpersisted()
    failopen.reset_cache()


# ── proxy-row builders ──────────────────────────────────────────────────────

def _classified(ts, *, kind="organic", sid="s-org", proposed="sonnet", **kw):
    row = {"ts": ts, "session_id": sid, "decision": "forwarded", "tier_mode": "conversation",
           "tier": "sonnet", "tier_reason": "policy", "tier_detail": None,
           "session_kind": kind, "tier_policy_version": "abc123def456",
           "tier_proposed": proposed, "tier_retry": None, "added_latency_s": 0.01}
    row.update(kw)
    return row


def _side(ts, **kw):
    return _classified(ts, tier_reason="side_call", proposed=None, **kw)


def _write(rows):
    path = pl.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _g3(**kw):
    return kpi.compute_scorecard(days=kw.pop("days", 7), now=NOW, **kw)["kpis"]["G3"]


def _rows_at(n, *, builder=_classified, start=NOW - 3600, **kw):
    return [builder(start + i, **kw) for i in range(n)]


# ═════════════════════════════ G3: where a field applies ═════════════════════════════

def test_side_calls_do_not_owe_a_tier_proposal():
    """tier_proposed is null on side calls because no classifier ran: not a gap."""
    _write(_rows_at(60, builder=_side) + _rows_at(60, start=NOW - 1800))
    g3 = _g3()
    assert g3["value"].startswith("100.0% (n=120; ")
    f = g3["fields"]["tier_proposed"]
    assert (f["applicable"], f["recorded"], f["not_applicable"], f["coverage"]) == (60, 60, 60, 1.0)
    assert g3["rows_by_type"] == {"side_call": {"rows": 60, "complete": 60},
                                  "classified": {"rows": 60, "complete": 60}}


def test_a_classified_row_without_a_proposal_is_a_real_gap():
    """The same null that is fine on a side call is a defect where the classifier ran."""
    rows = _rows_at(59) + [_classified(NOW - 10, proposed=None)]
    assert _g3()["value"].startswith("not measurable: no proxy row carries ")     # empty ledger: nothing yet
    _write(rows)
    g3 = _g3()
    assert g3["value"].startswith("98.3% (n=60; ")          # 59 / 60 complete
    assert g3["fields"]["tier_proposed"]["coverage"] == round(59 / 60, 4)
    assert g3["rows_by_type"]["classified"] == {"rows": 60, "complete": 59}


@pytest.mark.parametrize("reason,detail,owes_proposal", [
    ("long_first_prompt_floor", None, False),
    ("first_call", None, False),
    ("quota_pressure", "first_call", False),               # a capped first call: classifier never ran
    ("quota_pressure", "long_first_prompt_floor", False),
    ("quota_pressure", None, True),                         # a capped classified call: it did run
    ("sticky", None, True),
    ("thinking_floor", None, True),
    ("policy", None, True),
    ("decision_error", None, True),                         # the decision raised: owed, and missing
    ("user_pinned", None, False),
    ("config_pinned", None, False),
    ("unknown_model", None, False),
    ("explicit_opus_pin", None, False),
])
def test_which_tier_reasons_owe_a_proposal(reason, detail, owes_proposal):
    row = _classified(NOW - 5, proposed=None, tier_reason=reason, tier_detail=detail)
    assert ("tier_proposed" in kpi.g3_required_fields(row)) is owes_proposal, (reason, detail)


def test_every_tier_reason_the_policy_can_emit_has_a_row_type():
    """Premise check: the reason constants in proxy/tiers.py are the vocabulary the row
    types are defined over. A reason added there without a decision here would be
    silently typed ``classified`` and demand a proposal it may never carry."""
    reasons = {v for k, v in vars(pt).items() if k.startswith("REASON_") and isinstance(v, str)}
    decided = {pt.REASON_SIDE_CALL, pt.REASON_UNKNOWN_MODEL, pt.REASON_CONFIG_PINNED,
               pt.REASON_USER_PINNED, pt.REASON_EXPLICIT_OPUS_PIN, pt.REASON_DECISION_ERROR,
               pt.REASON_FIRST_CALL, pt.REASON_LONG_FIRST_PROMPT}
    classifier_ran = {pt.REASON_POLICY, pt.REASON_THINKING_FLOOR, pt.REASON_STICKY,
                      pt.REASON_ESCALATION, pt.REASON_HAIKU_REWRITE, pt.REASON_QUOTA_PRESSURE,
                      pt.REASON_ESCALATION_UNDER_PRESSURE}
    assert reasons - decided - classifier_ran - {pt.REASON_UNSEEN} == set()
    for r in classifier_ran:
        assert kpi.g3_row_type(_classified(NOW, tier_reason=r)) == "classified", r


def test_tier_retry_null_is_complete_and_a_missing_key_is_not():
    """null tier_retry is "no retry happened"; the old definition scored ~97% of rows
    (every call that was not retried) as incomplete, which is the 0.4%."""
    rows = _rows_at(60, tier_retry=None)
    gone = _classified(NOW - 1)
    del gone["tier_retry"]
    _write(rows + [gone])
    g3 = _g3()
    assert g3["fields"]["tier_retry"]["recorded"] == 60 and g3["fields"]["tier_retry"]["applicable"] == 61
    assert g3["value"].startswith(f"{60 / 61 * 100:.1f}% (n=61; ")


def test_null_session_kind_counts_against_completeness():
    """The 12%: rows written while their session had no tag. The old filter dropped
    them before counting, so the field read 100% by construction."""
    rows = _rows_at(88) + _rows_at(12, kind=None, start=NOW - 7200)
    _write(rows)
    g3 = _g3()
    assert g3["value"].startswith("88.0% (n=100; ")
    assert g3["fields"]["session_kind"]["coverage"] == 0.88
    assert g3["fields"]["tier_policy_version"]["coverage"] == 1.0   # the other fields are fine


def test_policy_version_is_not_owed_when_tiers_are_off():
    rows = _rows_at(60, tier_mode="off", tier_policy_version=None, tier_reason=None, tier_proposed=None,
                    tier=None)
    _write(rows)
    g3 = _g3()
    assert g3["value"].startswith("100.0% (n=60; ")
    assert set(g3["rows_by_type"]) == {"tiers_off"}
    assert g3["fields"]["tier_policy_version"]["applicable"] == 0
    assert g3["fields"]["tier_policy_version"]["coverage"] is None  # n/a is not 0% and not 100%


def test_tiers_on_with_no_recorded_decision_is_a_defect_not_a_free_pass():
    """A forwarded row with tiers on must carry a decision; its absence must not be
    mistaken for "the classifier did not run"."""
    _write(_rows_at(60, tier_reason=None, proposed=None))
    g3 = _g3()
    assert g3["rows_by_type"] == {"undecided": {"rows": 60, "complete": 0}}
    assert g3["value"].startswith("0.0% (n=60; ")


def test_a_row_served_by_a_local_backend_owes_no_tier_decision():
    served = [_classified(NOW - 100 + i, decision="served", tier_reason=None, proposed=None, tier=None)
              for i in range(60)]
    _write(served)
    assert _g3()["value"].startswith("100.0% (n=60; ")


def test_a_request_with_no_session_cannot_owe_a_session_tag():
    rows = _rows_at(30, kind=None, sid=None) + _rows_at(30, kind=None, sid="unknown", start=NOW - 900)
    _write(rows)
    g3 = _g3()
    assert g3["fields"]["session_kind"]["applicable"] == 0
    assert g3["value"].startswith("100.0% (n=60; ")


# ═════════════════════════════ G3: the schema window ═════════════════════════════

def _pre_schema(n, start):
    """Rows written before the instrumentation: none of the four keys exist."""
    out = []
    for i in range(n):
        r = _classified(start + i)
        for k in kpi.G3_FIELDS:
            del r[k]
        out.append(r)
    return out


def test_pre_schema_rows_are_excluded_and_counted():
    old = _pre_schema(100, NOW - 5 * DAY)
    new = _rows_at(60, start=NOW - 3600)
    _write(old + new)
    g3 = _g3()
    assert g3["value"].startswith("100.0% (n=60; since ")
    assert g3["rows_before_schema"] == 100
    assert g3["window_rows"] == 160 and g3["rows_counted"] == 60
    assert g3["schema_since"] == kpi._iso(NOW - 3600)
    assert g3["schema_since_source"] == "first row carrying every field"
    assert "100 row(s) before schema excluded" in g3["value"]
    # the old definition on the same rows: every pre-schema row fails, so the old number
    # was a statement about history, not about the writer
    assert sum(1 for r in old + new if all(r.get(k) is not None for k in kpi.G3_FIELDS)) / 160 < 0.4


def test_schema_since_can_be_overridden_to_audit_older_rows():
    old = _pre_schema(100, NOW - 5 * DAY)
    _write(old + _rows_at(60, start=NOW - 3600))
    g3 = _g3(schema_since=NOW - 6 * DAY, days=30)
    assert g3["schema_since_source"] == "override"
    assert g3["rows_counted"] == 160 and g3["rows_before_schema"] == 0
    assert g3["value"].startswith("37.5% (n=160; ")                   # 60 / 160
    # and later: moving the start past some new rows drops them
    g3 = _g3(schema_since=NOW - 1800)
    assert g3["rows_before_schema"] > 0 and g3["schema_since"] == kpi._iso(NOW - 1800)


def test_the_schema_start_is_the_latest_first_appearance_of_any_field():
    """A row counts once EVERY field exists. If tier_proposed appears an hour after the
    others, the hour before it is pre-schema, not a failure of tier_proposed."""
    a = _rows_at(30, start=NOW - 7200)
    for r in a:
        del r["tier_proposed"]
    b = _rows_at(60, start=NOW - 3600)
    _write(a + b)
    g3 = _g3()
    assert g3["schema_since"] == kpi._iso(NOW - 3600)
    assert g3["field_first_seen"]["tier_proposed"] == kpi._iso(NOW - 3600)
    assert g3["field_first_seen"]["session_kind"] == kpi._iso(NOW - 7200)
    assert g3["rows_before_schema"] == 30


def test_window_rows_outside_days_are_not_read_as_pre_schema():
    _write(_rows_at(60, start=NOW - 20 * DAY) + _rows_at(60, start=NOW - 3600))
    g3 = _g3(days=7)
    assert g3["window_rows"] == 60 and g3["rows_before_schema"] == 0 and g3["rows_counted"] == 60
    assert g3["schema_since"] == kpi._iso(NOW - 20 * DAY)   # the schema started before the window


def test_rows_without_a_usable_timestamp_are_counted_not_dropped_silently():
    rows = _rows_at(60) + [_classified(None), {**_classified(0), "ts": "yesterday"}, {**_classified(0), "ts": True}]
    _write(rows)
    g3 = _g3()
    assert g3["undated_rows"] == 3 and g3["rows_counted"] == 60


# ═════════════════════════════ G3: empty and all-excluded are "not measurable" ═════════════════════════════

def test_g3_with_nothing_to_count_is_not_measurable_never_a_rate():
    cases = {
        "empty ledger": [],
        "no row carries the fields yet": _pre_schema(80, NOW - 3600),
        "every in-window row predates the schema": _pre_schema(80, NOW - 2 * DAY) + _rows_at(5, start=NOW - 20 * DAY),
    }
    for label, rows in cases.items():
        _write(rows)
        g3 = _g3(days=1) if label == "every in-window row predates the schema" else _g3()
        assert g3["value"].startswith("not measurable: "), (label, g3["value"])
        assert g3["n"] is None and g3["measurable"] is False
        assert not re.search(r"\b(0|100)(\.0)?%", g3["value"]), label


def test_g3_all_rows_before_an_override_start_is_not_measurable():
    _write(_rows_at(80))
    g3 = _g3(schema_since=NOW + DAY)
    assert g3["value"].startswith("not measurable: 80 row(s) in window, all before the schema start")
    assert g3["rows_before_schema"] == 80


def test_g3_below_fifty_rows_says_too_few():
    _write(_rows_at(49))
    assert _g3()["value"] == "too few to tell (n=49)"


def test_g3_is_null_safe_on_hostile_rows():
    rows = _rows_at(60) + [
        _classified(NOW - 2, tier_reason=7), _classified(NOW - 3, tier_detail=["x"]),
        {"ts": NOW - 4}, {"ts": NOW - 5, "session_id": 9, "tier_reason": {"a": 1}},
    ]
    _write(rows)
    g3 = _g3()                                    # must not raise
    assert g3["measurable"] is True and g3["rows_counted"] == 64


# ═════════════════════════════ G3: against the real writer ═════════════════════════════

async def test_rows_written_by_the_real_proxy_are_complete_under_the_new_definition(tmp_path, monkeypatch):
    """Premise check. The row taxonomy is only worth anything if it matches what the
    proxy actually writes. Drive the real handler through every row type and require
    each to be complete when the session is tagged, and incomplete ONLY on session_kind
    when it is not."""
    from llm_router.proxy import backends as pb
    from tests.test_proxy_tiers import SID, Upstream, _app, _first, _post, _req, _rows

    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "code", "complexity": "moderate", "chain_head": [], "model": None}

    monkeypatch.setattr(pb, "choose_model", choose)
    session_kind.tag_session(SID, "/Users/x/Projects/app", env={})
    app = _app(tmp_path, Upstream())
    await _post(app, _first())                                   # first_call
    await _post(app, _req())                                     # classified
    await _post(app, dict(_req(), tools=[]))                     # side_call
    await _post(_app(tmp_path, Upstream(), tiers=ps.TIERS_OFF), _req())   # tiers_off
    real = _rows(tmp_path)
    assert {kpi.g3_row_type(r) for r in real} == {"first_call", "classified", "side_call", "tiers_off"}
    for r in real:
        assert all(kpi._g3_recorded(r, f) for f in kpi.g3_required_fields(r)), (kpi.g3_row_type(r), r)

    t0 = NOW - 600
    stamped = [dict(r, ts=t0 + i) for i in range(15) for r in real]       # 60 rows
    _write(stamped)
    assert _g3()["value"].startswith("100.0% (n=60; ")

    for r in stamped:                                                      # the session loses its tag
        r["session_kind"] = None
    _write(stamped)
    g3 = _g3()
    assert g3["value"].startswith("0.0% (n=60; ")
    assert {f for f, v in g3["fields"].items() if v["coverage"] is not None and v["coverage"] < 1} == {"session_kind"}


def test_replay_of_the_2026_10_04_ledger_shape():
    """The measured composition: 2,637 rows after the #250 deploy, policy version on all,
    session tag on 88%, tier_proposed on 990 (the rest 1,625 side calls + 22 floors).
    The old definition read 0.4%; the right answer is the tag coverage, 88%."""
    n_side, n_class, n_floor = 1625, 990, 22
    rows = [_side(NOW - 86000 + i) for i in range(n_side)]
    rows += [_classified(NOW - 80000 + i) for i in range(n_class)]
    rows += [_classified(NOW - 70000 + i, proposed=None, tier_reason="long_first_prompt_floor") for i in range(18)]
    rows += [_classified(NOW - 60000 + i, proposed=None, tier_reason="quota_pressure", tier_detail="first_call")
             for i in range(n_floor - 18)]
    assert len(rows) == 2637
    for i, r in enumerate(rows):                                           # 12% null tag, spread across types
        if i % 25 < 3:
            r["session_kind"] = None
    # retry: 3% of rows were retried (the only rows the old definition could call complete)
    for i, r in enumerate(rows):
        r["tier_retry"] = {"status": 400, "detail": "x"} if i % 33 == 0 else None
    _write(rows)
    old_complete = sum(1 for r in rows if all(r.get(k) is not None for k in kpi.G3_FIELDS))
    assert old_complete / len(rows) < 0.01                                 # the old number: under 1%
    g3 = _g3(days=3)
    tagged = sum(1 for r in rows if r["session_kind"] is not None)
    assert g3["rows_counted"] == 2637
    assert g3["value"].startswith(f"{tagged / 2637 * 100:.1f}% (n=2637; ")
    assert g3["fields"]["tier_proposed"]["applicable"] == 990
    assert g3["fields"]["tier_proposed"]["not_applicable"] == 1647         # 1,625 side calls + 22 floors
    assert g3["fields"]["tier_proposed"]["coverage"] == 1.0
    assert g3["fields"]["tier_policy_version"]["coverage"] == 1.0
    assert g3["fields"]["tier_retry"]["coverage"] == 1.0
    assert 0.87 < g3["fields"]["session_kind"]["coverage"] < 0.89


# ═════════════════════════════ session kind: tagging and the join index ═════════════════════════════

def test_the_first_tag_wins_and_a_resume_from_another_cwd_does_not_flip_it():
    assert session_kind.tag_session("sess-x", "/Users/x/Projects/app", env={}) == "organic"
    assert session_kind.tag_session("sess-x", "/Users/x/work/scratchpad/p", env={}) == "organic"
    assert session_kind.kind_of("sess-x") == "organic"
    tag = json.loads(paths.state_path("session_kind_sess-x.json").read_text())
    assert tag["cwd"] == "/Users/x/Projects/app"           # the file still describes the first start


def test_the_explicit_override_still_replaces_a_tag():
    session_kind.tag_session("sess-y", "/Users/x/Projects/app", env={})
    assert session_kind.tag_session("sess-y", "/Users/x/Projects/app",
                                    env={"LLM_ROUTER_SESSION_KIND": "harness"}) == "harness"
    session_kind._FOUND.clear()
    assert session_kind.kind_of("sess-y") == "harness"
    # an invalid override is not an override
    assert session_kind.tag_session("sess-y", "/x", env={"LLM_ROUTER_SESSION_KIND": "bogus"}) == "harness"


def test_a_session_with_no_tag_is_tagged_by_a_later_call():
    """What the UserPromptSubmit hook relies on: SessionStart never ran (the session was
    already open when tagging shipped), the next prompt tags it."""
    assert session_kind.kind_of("sess-late") is None
    assert session_kind.tag_session("sess-late", "/Users/x/Projects/app", env={}) == "organic"
    assert session_kind.kind_of("sess-late") == "organic"


def test_the_kind_index_precedence_and_conflict():
    proxy_rows = [{"session_id": "a", "session_kind": "research"}, {"session_id": "a", "session_kind": None},
                  {"session_id": "b", "session_kind": "organic"}, {"session_id": "b", "session_kind": "headless"},
                  {"session_id": "c", "session_kind": "organic"}, {"session_id": "d", "session_kind": None}]
    session_kind.tag_session("t", "/Users/x/p", env={})                 # organic tag file
    session_kind.tag_session("c", "/Users/x/work/scratchpad/x", env={})            # research tag file; proxy rows say organic
    idx = session_kind.KindIndex(proxy_rows)
    r = idx.resolve("a")                                                # no tag: the proxy rows, all agreeing
    assert (r.kind, r.source) == ("research", "proxy_ledger")
    r = idx.resolve("b")                                                # no tag, rows disagree: unresolved
    assert (r.kind, r.source) == (None, "conflict")
    r = idx.resolve("c")                                                # the tag file beats the proxy rows
    assert (r.kind, r.source, r.ledger_disagrees) == ("research", "tag", True)
    r = idx.resolve("d")                                                # a null stamp is no evidence
    assert (r.kind, r.source) == (None, None)
    r = idx.resolve("zzz", stamp="harness")                             # the record's own stamp, no tag
    assert (r.kind, r.source) == ("harness", "stamp")
    r = idx.resolve("t", stamp="harness")                               # the tag still wins over a stamp
    assert (r.kind, r.source) == ("organic", "tag")
    for sid in (None, "", 7):
        assert idx.resolve(sid).kind is None                            # never organic by default


# ═════════════════════════════ NS / D1 / D2: the join ═════════════════════════════

def _iso_ts(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _transcript(root: Path, sid: str, n: int, start: float) -> None:
    proj = root / "-Users-x-proj"
    proj.mkdir(parents=True, exist_ok=True)
    with (proj / f"{sid}.jsonl").open("w", encoding="utf-8") as fh:
        for i in range(n):
            ts = start + i
            fh.write(json.dumps({"parentUuid": None, "isSidechain": False, "type": "user", "uuid": f"u{i}",
                                 "timestamp": _iso_ts(ts), "sessionId": sid,
                                 "message": {"role": "user", "content": f"please help with unrelated task {i}"}}) + "\n")


def test_units_are_stamped_with_their_sessions_tag_at_build_time(tmp_path):
    root = tmp_path / "claude-projects"
    start = time.time() - 600
    tagged, ledger_only, none, conflict = (f"{c}1c4e6a2-7b3d-4f58-9a10-2d6e8c0b4f7{i}" for i, c in enumerate("abcd"))
    for sid in (tagged, ledger_only, none, conflict):
        _transcript(root, sid, 3, start)
    session_kind.tag_session(tagged, "/Users/x/Projects/app", env={})                    # tag file
    _write([{"ts": start, "session_id": ledger_only, "session_kind": "research"},         # proxy rows only
            {"ts": start, "session_id": conflict, "session_kind": "organic"},
            {"ts": start, "session_id": conflict, "session_kind": "headless"}])
    got: dict[str, set] = {}
    for u in ns.units(days=None, root=root):
        got.setdefault(u["session_id"], set()).add((u["session_kind"], u["session_kind_source"]))
    assert got == {tagged: {("organic", "tag")}, ledger_only: {("research", "proxy_ledger")},
                   none: {(None, None)}, conflict: {(None, "conflict")}}


def test_a_codex_unit_carries_the_stamp_its_ledger_row_was_written_with(tmp_path):
    """The one writer of units (hooks/agent-route.py -> north_star_units.jsonl) stamps each
    row; the unit keeps that stamp when no tag file exists."""
    root = tmp_path / "claude-projects"
    start = time.time() - 600
    sid = "e5f6a7b8-1c2d-4e3f-8a9b-0c1d2e3f4a5b"
    _transcript(root, sid, 2, start)
    path = paths.state_path("north_star_units.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": start + 1, "lever": "agent_route_codex", "outcome": "delegated",
                                "model": "gpt-x", "session_id": sid, "session_kind": "organic",
                                "task_type": "code"}) + "\n")
    codex = [u for u in ns.units(days=None, root=root) if u["kind"] == ns.UNIT_AGENT_ROUTE_CODEX]
    assert len(codex) == 1
    assert (codex[0]["session_kind"], codex[0]["session_kind_source"]) == ("organic", "stamp")


def _stream(monkeypatch, rows):
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False: iter(rows))


def _unit(sid, kind="claude_main_call", outcome=ns.OUTCOME_NOT_ROUTED, lever=None, **kw):
    return {"session_id": sid, "kind": kind, "outcome": outcome, "lever": lever, "ts": _iso_ts(NOW - 100), **kw}


def _attempted():
    return sorted(ns.ATTEMPTED_KINDS)[0]


def _mixed(sid, **kw):
    return ([_unit(sid, _attempted(), ns.OUTCOME_USED, **kw) for _ in range(30)]
            + [_unit(sid, _attempted(), ns.OUTCOME_REDO, **kw) for _ in range(30)]
            + [_unit(sid, **kw) for _ in range(40)])


def test_units_without_a_stamp_are_joined_from_the_proxy_rows(monkeypatch):
    """The fallback for units that predate stamping: the session's proxy rows carry the
    tag even where the tag file is gone."""
    _write([{"ts": NOW, "session_id": "s-ledger", "session_kind": "organic"}] * 3)
    _stream(monkeypatch, _mixed("s-ledger") + _mixed("s-nothing") * 2)
    k = kpi.compute_scorecard(days=7, now=NOW)
    assert k["kpis"]["NS"]["value"] == "30.0% (n=100)"           # 30 used / 100 organic units
    assert k["kpis"]["D1"]["value"] == "60.0% (n=100)"
    assert k["kpis"]["D2"]["value"] == "50.0% (n=60)"
    assert k["joins"]["window_units"] == 300
    assert (k["joins"]["joined"], k["joins"]["untagged"]) == (100, 200)
    assert k["joins"]["joined_by_source"] == {"proxy_ledger": 100}
    assert k["joins"]["counted"] == 100 and k["joins"]["counted_sessions"] == 1
    assert k["joins"]["largest_session_share"] == 1.0


def test_a_stamp_already_on_the_unit_is_trusted_and_untagged_is_never_organic(monkeypatch):
    rows = (_mixed("a", session_kind="organic", session_kind_source="tag")
            + _mixed("b", session_kind=None, session_kind_source=None) * 5)
    _stream(monkeypatch, rows)
    k = kpi.compute_scorecard(days=7, now=NOW)
    assert k["kpis"]["NS"]["n"] == 100                            # only "a": the five untagged "b" copies are out
    assert (k["joins"]["joined"], k["joins"]["untagged"]) == (100, 500)
    text = kpi.render_scorecard(k)
    assert "100 joined to a session-kind tag (tag 100)" in text and "500 untagged (never counted as organic)" in text


def test_no_organic_unit_among_joined_ones_is_not_measurable_and_says_what_joined(monkeypatch):
    _stream(monkeypatch, _mixed("r", session_kind="research", session_kind_source="tag")
            + _mixed("x", session_kind=None, session_kind_source=None))
    k = kpi.compute_scorecard(days=7, now=NOW)["kpis"]
    for key in ("NS", "D1", "D2"):
        v = k[key]["value"]
        assert v.startswith("not measurable: 200 unit(s) in window, none from an organic session: "), v
        assert "100 joined to a tag (research 100)" in v and "100 untagged" in v
        assert k[key]["measurable"] is False and k[key]["n"] is None
    wide = kpi.compute_scorecard(days=7, include_research=True, now=NOW)["kpis"]
    assert wide["NS"]["value"] == "30.0% (n=100)"                 # --include research widens, untagged stays out


def test_all_untagged_units_keep_the_old_explanation(monkeypatch):
    _stream(monkeypatch, _mixed("x"))
    v = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["NS"]["value"]
    assert v.startswith("not measurable: 100 unit(s) in window, all from sessions with no session-kind tag")
    assert "never counted as organic" in v


def test_no_units_at_all_is_not_measurable_not_zero(monkeypatch):
    _stream(monkeypatch, [])
    k = kpi.compute_scorecard(days=7, now=NOW)
    for key in ("NS", "D1", "D2"):
        assert k["kpis"][key]["value"] == "not measurable: no unit in window"
    assert k["joins"]["window_units"] == 0 and k["joins"]["largest_session_share"] is None
    assert not re.search(r"\b0(\.0)?%", kpi.render_scorecard(k).split("NS/D1/D2")[0].split("G3")[0])


def test_a_tag_that_disagrees_with_the_proxy_rows_wins_and_is_reported(monkeypatch):
    session_kind.tag_session("s-flip", "/Users/x/work/scratchpad/p", env={})                 # research tag file
    _write([{"ts": NOW, "session_id": "s-flip", "session_kind": "organic"}] * 4)           # rows stamped organic
    _stream(monkeypatch, _mixed("s-flip"))
    k = kpi.compute_scorecard(days=7, now=NOW)
    assert k["kpis"]["NS"]["value"].startswith("not measurable: ")            # research, not organic
    assert k["joins"]["joined_by_kind"] == {"research": 100}
    assert k["joins"]["sessions_where_tag_and_proxy_rows_disagree"] == 1
    assert "disagrees with their proxy rows (the tag file wins)" in kpi.render_scorecard(k)


# ═════════════════════════════ D3 / D4: the same join, cost weights ═════════════════════════════

def test_d3_resolves_the_kind_through_the_same_index(monkeypatch):
    _write([{"ts": NOW, "session_id": "s-d3", "session_kind": "organic"}])
    rows = ([{"outcome": usage_outcome.OUTCOME_USED, "session_id": "s-d3", "session_kind": None, "ts": NOW - 5}] * 45
            + [{"outcome": usage_outcome.OUTCOME_REDONE, "session_id": "s-d3", "session_kind": None, "ts": NOW - 4}] * 15
            + [{"outcome": usage_outcome.OUTCOME_REDONE, "session_id": "s-other", "session_kind": None}] * 40)
    monkeypatch.setattr(usage_outcome, "judge_recent", lambda days=7, root=None: rows)
    d3 = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["D3"]
    assert d3["value"] == "25.0% (n=60)"
    assert d3["seen"] == 100 and d3["newest_ts"] == NOW - 4


def _tier_rows(opus, sonnet, haiku, *, kind="organic", sid="s-org", start=NOW - 7200, **kw):
    out = []
    for tier, n in (("opus", opus), ("sonnet", sonnet), ("haiku", haiku)):
        out += [_classified(start + len(out) + i, kind=kind, sid=sid, tier=tier, **kw) for i in range(n)]
    return out


def test_d4_prints_the_cost_weighted_share_next_to_the_call_share():
    _write(_tier_rows(opus=10, sonnet=40, haiku=50))
    d4 = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["D4"]
    w = kpi.TIER_COST_WEIGHT
    total = 10 * w["opus"] + 40 * w["sonnet"] + 50 * w["haiku"]
    assert d4["value"].startswith("haiku=50.0%, sonnet=40.0%, opus=10.0% (n=100) | cost-weighted (haiku 1 : sonnet 3.66 : opus 6.15 per call): ")
    assert d4["cost_weighted_share"] == {"opus": round(10 * w["opus"] / total, 4),
                                         "sonnet": round(40 * w["sonnet"] / total, 4),
                                         "haiku": round(50 * w["haiku"] / total, 4)}
    # 10 Opus calls are 10% of calls and 23.8% of the cost; the whole point of the second number
    assert d4["cost_weighted_share"]["opus"] == pytest.approx(0.2384, abs=1e-4)
    assert d4["calls_by_tier"] == {"opus": 10, "sonnet": 40, "haiku": 50}


def test_d4_cost_weights_match_the_policy_file_comment():
    """The weights live in a comment of claude_tiers.yaml (the measured probe), not in a
    field. If someone updates the comment, this fails until the constants follow."""
    text = (REPO / "src/llm_router/proxy/claude_tiers.yaml").read_text(encoding="utf-8")
    m = re.search(r"Opus cost\s+#?\s*(\d+\.\d+)x Sonnet and (\d+\.\d+)x Haiku", text)
    assert m, "the cost-ratio sentence moved or changed"
    opus_over_sonnet, opus_over_haiku = float(m.group(1)), float(m.group(2))
    w = kpi.TIER_COST_WEIGHT
    assert w["opus"] / w["sonnet"] == pytest.approx(opus_over_sonnet, rel=1e-6)
    assert w["opus"] / w["haiku"] == pytest.approx(opus_over_haiku, rel=1e-6)


def test_d4_excludes_research_and_harness_by_default_and_never_harness():
    rows = (_tier_rows(opus=10, sonnet=40, haiku=50)
            + _tier_rows(opus=500, sonnet=0, haiku=0, kind="research", sid="s-res")
            + _tier_rows(opus=700, sonnet=0, haiku=0, kind="harness", sid="s-har")
            + _tier_rows(opus=900, sonnet=0, haiku=0, kind=None, sid="s-none"))
    _write(rows)
    assert kpi.compute_scorecard(days=7, now=NOW)["kpis"]["D4"]["n"] == 100
    wide = kpi.compute_scorecard(days=7, include_research=True, now=NOW)["kpis"]["D4"]
    assert wide["n"] == 600 and wide["calls_by_tier"]["opus"] == 510     # research in; harness and untagged out


def test_d4_uses_the_kind_each_row_was_written_with_and_leaves_untagged_rows_out():
    """D4's population is the rows stamped when they were written. A row from before
    tagging existed (no stamp) is not organic just because its session has a tag file
    today: joining that tag onto old rows would change D4's population, not its
    definition."""
    session_kind.tag_session("s-tagged-now", "/Users/x/Projects/app", env={})     # tagged organic today
    old = _tier_rows(opus=30, sonnet=30, haiku=0, kind=None, sid="s-tagged-now")  # written before tagging
    old_no_key = _tier_rows(opus=5, sonnet=5, haiku=0, sid="s-tagged-now")
    for r in old_no_key:
        del r["session_kind"]
    _write(old + old_no_key + _tier_rows(opus=0, sonnet=60, haiku=0, kind="organic", sid="s-tagged-now"))
    d4 = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["D4"]
    assert d4["n"] == 60 and d4["calls_by_tier"] == {"sonnet": 60}
    assert d4["seen"] == 130                                                      # all in window, 70 left out


def test_d4_leaves_an_unweighted_tier_out_rather_than_guess_its_weight():
    rows = _tier_rows(opus=20, sonnet=20, haiku=0) + [_classified(NOW - 50 + i, tier="fable") for i in range(10)]
    _write(rows)
    d4 = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["D4"]
    assert d4["unweighted_calls"] == 10
    assert "fable" not in d4["cost_weighted_share"] and "10 call(s) on an unweighted tier left out" in d4["value"]
    only_fable = [_classified(NOW - 50 + i, tier="fable") for i in range(60)]
    _write(only_fable)
    v = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["D4"]["value"]
    assert "cost-weighted: not measurable (no tier with a known weight)" in v


def test_d4_with_no_organic_rows_is_not_measurable_never_a_share():
    _write(_tier_rows(opus=60, sonnet=0, haiku=0, kind="research") + _tier_rows(opus=7, sonnet=0, haiku=0, kind=None))
    v = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["D4"]["value"]
    assert v == ("not measurable: no tiered proxy call from an organic session in window "
                 "(7 untagged and 60 other-kind row(s) excluded)")


# ═════════════════════════════ O1 freshness helper ═════════════════════════════

def test_newest_timestamp_reads_every_table_and_both_text_formats(tmp_path):
    from llm_router import dashboard_data as dd

    db = tmp_path / "usage.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE usage (timestamp TEXT)")
    con.execute("CREATE TABLE savings_stats (timestamp TEXT)")
    con.execute("CREATE TABLE claude_usage (other TEXT)")                 # no timestamp column: skipped
    con.execute("INSERT INTO usage VALUES ('2026-10-04 19:27:28')")
    con.execute("INSERT INTO savings_stats VALUES ('2026-10-04T19:27:27.037972+00:00')")
    con.execute("INSERT INTO savings_stats VALUES ('2026-08-19 15:27:25')")
    con.commit()
    con.close()
    want = datetime(2026, 10, 4, 19, 27, 28, tzinfo=timezone.utc).timestamp()
    assert dd.newest_timestamp(db_path=db) == want
    assert dd.newest_timestamp(db_path=tmp_path / "absent.db") is None
    before = sorted(p.name for p in tmp_path.iterdir())
    dd.newest_timestamp(db_path=db)
    assert sorted(p.name for p in tmp_path.iterdir()) == before          # read-only: no -wal/-shm created


# ═════════════════════════════ --health ═════════════════════════════

def _health(*, data=None, **kw):
    data = data or kpi.compute_scorecard(days=7, now=NOW)
    return kpi.compute_health(data, now=NOW, **kw)


def test_health_names_measured_blind_and_stale_with_one_reason_and_the_n(monkeypatch):
    fresh = _rows_at(80, start=NOW - 3600)                                  # newest 1h old
    _write(fresh)
    h = _health()
    assert set(h["kpis"]) == set(kpi._ORDER)                                # every KPI has a state
    g3, d4, ns_ = h["kpis"]["G3"], h["kpis"]["D4"], h["kpis"]["NS"]
    assert (g3["state"], g3["n"]) == ("measured", 80) and "newest data point 1.0h old" in g3["reason"]
    assert d4["state"] == "measured"
    assert ns_["state"] == "blind" and ns_["reason"] == "no unit in window" and ns_["n"] == 0
    assert h["kpis"]["G1_hook"]["state"] == "blind"                         # no hook row was written here
    assert "no hook invocation recorded in window" in h["kpis"]["G1_hook"]["reason"]
    assert h["kpis"]["O2"]["state"] == "blind"
    assert h["counts"]["measured"] + h["counts"]["blind"] + h["counts"]["stale"] == len(kpi._ORDER)

    _write(_rows_at(80, start=NOW - 5 * DAY))                               # newest 5 days old
    h = kpi.compute_health(kpi.compute_scorecard(days=30, now=NOW), now=NOW)
    assert h["kpis"]["G3"]["state"] == "stale"
    assert h["kpis"]["G3"]["reason"] == "newest data point is 5.0d old, over the 2.0d limit"
    assert h["kpis"]["G3"]["n"] == 80                                       # stale still carries its n
    assert h["kpis"]["D4"]["state"] == "stale"


def test_health_threshold_is_stated_and_adjustable():
    _write(_rows_at(80, start=NOW - 30 * 3600))                             # 30h old
    assert _health()["kpis"]["G3"]["state"] == "measured"                   # inside the 48h default
    h = _health(stale_hours=12)
    assert h["kpis"]["G3"]["state"] == "stale" and h["stale_after_hours"] == 12
    assert "limit" in h["kpis"]["G3"]["reason"]
    text = kpi.render_health(h)
    assert "stale = newest data point older than 12.0h (live ledgers) or 30d (frozen benchmark, O2/D5)" in text


def test_a_frozen_benchmark_is_judged_on_a_month_not_two_days(monkeypatch, tmp_path):
    def bench(generated_at):
        p = tmp_path / "bench.json"
        d = {"o2": {"acceptable_rate": 0.9, "n": 120}, "d5": {"accuracy": 0.7, "n": 150}}
        if generated_at is not None:
            d["generated_at"] = generated_at
        p.write_text(json.dumps(d))
        monkeypatch.setenv("LLM_ROUTER_KPI_BENCHMARK_PATH", str(p))

    iso = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    bench(iso(NOW - 10 * DAY))
    h = _health()["kpis"]
    assert h["O2"]["state"] == "measured" and h["D5"]["state"] == "measured"
    bench(iso(NOW - 40 * DAY))
    h = _health()["kpis"]
    assert h["O2"]["state"] == "stale" and "40.0d old, over the 30.0d limit" in h["O2"]["reason"]
    bench(None)                                                              # a number nobody can date
    h = _health()["kpis"]
    assert h["O2"]["state"] == "blind" and "no timestamp" in h["O2"]["reason"] and h["O2"]["n"] == 120


def test_health_cli_exits_zero_with_blind_kpis_and_strict_exits_nonzero(capsys):
    assert kpi.cmd_kpi(["--health"]) == 0
    out = capsys.readouterr().out
    assert "blind" in out and "(exit 0; --strict exits 1 if any KPI is blind)" in out
    assert kpi.cmd_kpi(["--health", "--strict"]) == 1
    capsys.readouterr()


def test_strict_exits_zero_when_nothing_is_blind(monkeypatch, capsys):
    ok = {k: {"state": "measured", "reason": "r", "n": 60, "newest_ts": NOW, "newest": "x",
              "age_hours": 0.0, "stale_after_hours": 48.0} for k in kpi._ORDER}
    monkeypatch.setattr(kpi, "compute_health", lambda data, **kw: {
        "generated_at": data["generated_at"], "window_days": 7, "stale_after_hours": 48.0,
        "benchmark_stale_after_days": 30.0, "kpis": ok,
        "counts": {"measured": len(ok), "blind": 0, "stale": 0}})
    assert kpi.cmd_kpi(["--health", "--strict"]) == 0
    capsys.readouterr()
    # a stale KPI does not trip --strict (the contract is "a blind KPI exits non-zero")
    ok["G3"] = dict(ok["G3"], state="stale")
    monkeypatch.setattr(kpi, "compute_health", lambda data, **kw: {
        "generated_at": data["generated_at"], "window_days": 7, "stale_after_hours": 48.0,
        "benchmark_stale_after_days": 30.0, "kpis": ok,
        "counts": {"measured": len(ok) - 1, "blind": 0, "stale": 1}})
    assert kpi.cmd_kpi(["--health", "--strict"]) == 0


def test_strict_without_health_is_an_error():
    with pytest.raises(SystemExit) as exc:
        kpi.cmd_kpi(["--strict"])
    assert exc.value.code == 2


def test_health_json_is_parseable_and_carries_state_reason_n(capsys):
    assert kpi.cmd_kpi(["--health", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data["kpis"]) == set(kpi._ORDER)
    for v in data["kpis"].values():
        assert v["state"] in ("measured", "blind", "stale") and v["reason"] and v["n"] is not None


def test_schema_since_flag_is_parsed_and_rejects_nonsense(capsys):
    assert kpi.cmd_kpi(["--json", "--schema-since", "2026-10-03"]) == 0
    capsys.readouterr()
    assert kpi.cmd_kpi(["--json", "--schema-since", "2026-10-03T19:46:18Z"]) == 0
    capsys.readouterr()
    assert kpi.cmd_kpi(["--json", "--schema-since", "1791068445"]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit):
        kpi.cmd_kpi(["--schema-since", "not-a-date"])


def test_the_scorecard_never_creates_files_in_the_state_dir(tmp_path):
    """No file appears in the (isolated) state dir. This does not cover O1's pre-existing
    dashboard_data.summary(), which opens usage.db read-write (no usage.db exists here)."""
    _write(_rows_at(60))
    state = paths.llm_router_home()
    before = sorted(p.name for p in state.iterdir())
    kpi.cmd_kpi(["--days", "3"])
    kpi.cmd_kpi(["--health"])
    assert sorted(p.name for p in state.iterdir()) == before


# ═════════════════════════════ wiring ═════════════════════════════

def test_hooks_mirror_is_byte_identical_for_the_prompt_hook():
    assert (REPO / "hooks/auto-route.py").read_bytes() == (REPO / "src/llm_router/hooks/auto-route.py").read_bytes()


def test_the_prompt_hook_tags_the_session_inside_a_fail_open_guard():
    tree = ast.parse((REPO / "src/llm_router/hooks/auto-route.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "tag_session"]
    assert len(calls) == 1                                       # found something to check
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try) and calls[0] in list(ast.walk(n))]
    assert tries and tries[-1].handlers
    handler_src = "".join(ast.unparse(h) for h in tries[-1].handlers)
    assert "CHZ-FO-SESSION-KIND-TAG" in handler_src              # a failure is counted, not swallowed


# ═════════════════════════════ review findings (independent verifier, 2026-10-04) ═════════════════════════════

def test_a_served_row_owes_no_policy_version_when_tiers_are_off():
    """proxy/server.py writes tier_policy_version=None whenever tiers are off, served rows
    included. Typing the row `served` before checking `tier_mode` made every locally
    served row on a tiers-off proxy score incomplete."""
    row = _classified(NOW - 5, decision="served", tier_mode="off", tier_policy_version=None,
                      tier_reason=None, proposed=None, tier=None)
    assert kpi.g3_row_type(row) == "tiers_off"
    assert kpi.g3_required_fields(row) == ("session_kind", "tier_retry")
    served_on = dict(row, tier_mode="conversation")
    assert kpi.g3_row_type(served_on) == "served" and "tier_policy_version" in kpi.g3_required_fields(served_on)


def test_g3_says_how_many_sessions_the_rows_come_from():
    rows = _rows_at(90, sid="s-big") + _rows_at(10, sid="s-small", start=NOW - 900)
    _write(rows)
    g3 = _g3()
    assert g3["counted_sessions"] == 2 and g3["largest_session_share"] == 0.9
    assert "; 2 session(s); " in g3["value"]


def _hook(tmp_path, payload: dict) -> Path:
    """Run the real UserPromptSubmit hook in a scratch HOME; return its state dir."""
    hook = REPO / "src/llm_router/hooks/auto-route.py"
    home = tmp_path / "hookhome"
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "LLM_ROUTER_HOME": str(home / ".llm-router"),
           "LLM_ROUTER_ENFORCE": "off", "LLM_ROUTER_ZERO_CLAUDE": "0"}
    subprocess.run([sys.executable, str(hook)], input=json.dumps(payload).encode(), capture_output=True,
                   env=env, timeout=90)
    return home / ".llm-router"


@pytest.mark.parametrize("prompt", [
    "   ",                                                       # empty prompt exit
    "<task-notification>background task done</task-notification>",  # system-notification bypass
    "Another Claude session sent a message: here is my report",  # sub-agent report bypass
    "fix the llm_router hook that is broken",                    # self-reference bypass
])
def test_the_prompt_hook_tags_the_session_even_when_the_prompt_bypasses_routing(tmp_path, prompt):
    """13% of real invocations exit through one of these before routing. A session whose
    prompts all do (this repo's own development) must still get its tag."""
    sid = "5d1c9a3e-77b2-4c10-8f0a-3b6e2d9c1a40"
    state = _hook(tmp_path, {"session_id": sid, "prompt": prompt, "cwd": "/Users/x/work/scratchpad/p"})
    tag = json.loads((state / f"session_kind_{sid}.json").read_text())
    assert tag["kind"] == "research"                              # the cwd was passed, not dropped
    assert tag["cwd"] == "/Users/x/work/scratchpad/p"


def test_the_prompt_hook_does_not_flip_an_existing_tag(tmp_path):
    sid = "6e2d0b4f-88c3-4d21-9a1b-4c7f3e0d2b51"
    first = _hook(tmp_path, {"session_id": sid, "prompt": "   ", "cwd": "/Users/x/Projects/app"})
    _hook(tmp_path, {"session_id": sid, "prompt": "   ", "cwd": "/Users/x/work/scratchpad/p"})
    tag = json.loads((first / f"session_kind_{sid}.json").read_text())
    assert (tag["kind"], tag["cwd"]) == ("organic", "/Users/x/Projects/app")


def test_a_forced_kind_rewrites_a_tag_only_when_it_changes_it():
    session_kind.tag_session("sess-f", "/Users/x/Projects/app", env={})
    path = paths.state_path("session_kind_sess-f.json")
    before = path.read_text()
    session_kind.tag_session("sess-f", "/Users/x/Projects/app", env={"LLM_ROUTER_SESSION_KIND": "organic"})
    assert path.read_text() == before                              # same kind: not rewritten on every prompt
    session_kind.tag_session("sess-f", "/Users/x/Projects/app", env={"LLM_ROUTER_SESSION_KIND": "research"})
    assert json.loads(path.read_text())["kind"] == "research"


def test_the_stale_hours_flag_reaches_compute_health(capsys):
    _write(_rows_at(80, start=time.time() - 30 * 3600))              # 30h old: fresh at 48h, stale at 12h
    assert kpi.cmd_kpi(["--health", "--json", "--days", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["kpis"]["G3"]["state"] == "measured"
    assert kpi.cmd_kpi(["--health", "--json", "--days", "3", "--stale-hours", "12"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kpis"]["G3"]["state"] == "stale" and out["stale_after_hours"] == 12


def test_the_strict_footer_says_when_it_exits_nonzero(capsys):
    assert kpi.cmd_kpi(["--health", "--strict"]) == 1
    assert "exit 1: --strict and a KPI is blind" in capsys.readouterr().out
    assert kpi.cmd_kpi(["--health"]) == 0
    assert "(exit 0; --strict exits 1 if any KPI is blind)" in capsys.readouterr().out


def test_health_n_for_too_few_is_the_n_behind_the_number(monkeypatch):
    rows = [{"outcome": usage_outcome.OUTCOME_USED, "session_id": "s", "session_kind": "organic", "ts": NOW - 5}] * 23
    rows += [{"outcome": usage_outcome.OUTCOME_UNKNOWN, "session_id": "s", "session_kind": "organic"}] * 200
    monkeypatch.setattr(usage_outcome, "judge_recent", lambda days=7, root=None: rows)
    h = _health()["kpis"]["D3"]
    assert h["state"] == "blind" and h["n"] == 23 and "n=23" in h["reason"]


def test_g1_hook_g2_and_g4_are_judged_from_their_own_logs(monkeypatch):
    """G2 was an all-time count and G4 a snapshot of this moment, so --health had to say
    "cannot go stale" / "not a rate" for them; G1's hook half was "not instrumented". All
    three now read their own timestamped log, so health judges them like any other KPI:
    by the newest data point behind the number, and blind below 50."""
    from llm_router import failopen
    from llm_router import hook_latency as hl
    from llm_router import provider_bench_log as bl

    monkeypatch.setattr(failopen, "_now", lambda: NOW - 3600)
    failopen.record("CHZ-FO-TEST", RuntimeError("x"))
    failopen.reset_cache()
    for i in range(60):
        hl.record("enforce-route", "PreToolUse", 10.0, now=NOW - 3600 - i)      # newest 1h old
    for i in range(3):
        bl.log_bench(f"p{i}", "cli", NOW + 3600, NOW - 3600)
    h = _health()["kpis"]
    assert (h["G1_hook"]["state"], h["G1_hook"]["n"]) == ("measured", 60)
    assert "newest data point 1.0h old" in h["G1_hook"]["reason"]
    assert (h["G2"]["state"], h["G2"]["n"]) == ("measured", 60)               # 60 hook calls, no proxy rows
    assert "newest data point 1.0h old" in h["G2"]["reason"]
    # 3 benches is below 50: blind, with the n behind it; the rare-event note is only for measured ones.
    assert (h["G4"]["state"], h["G4"]["n"]) == ("blind", 3)
    assert h["G4"]["reason"] == f"too few to tell: n=3, need {kpi.MIN_N}"

    h = kpi.compute_health(kpi.compute_scorecard(days=30, now=NOW + 5 * DAY), now=NOW + 5 * DAY)["kpis"]
    assert h["G1_hook"]["state"] == "stale" and h["G2"]["state"] == "stale"   # the feed stopped 5 days ago
    assert h["G1_hook"]["reason"] == "newest data point is 5.0d old, over the 2.0d limit"


def test_malformed_ledger_rows_do_not_crash_the_scorecard():
    good = _rows_at(60)
    junk = [{"ts": float("inf"), "session_id": "x"}, {"ts": 1e20}, {"ts": NOW - 1, "tier": ["opus"]},
            {"ts": NOW - 1, "tier": {"a": 1}, "session_kind": "organic"},
            {"ts": NOW - 2, "session_kind": "organic", "tier": 7}]
    path = pl.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in good + junk) + "\n[1, 2]\n42\n\"text\"\n", encoding="utf-8")
    data = kpi.compute_scorecard(days=7, now=NOW)                    # must not raise
    assert data["kpis"]["G3"]["measurable"] is True
    assert data["kpis"]["D4"]["calls_by_tier"] == {"sonnet": 60}      # only string tiers are tiers
    kpi.compute_health(data, now=NOW)
    assert kpi._iso(float("inf")) is None and kpi._iso(1e20) is None
    assert kpi._parse_ts("9999-12-31T00:00:00Z") is None            # past year 5000 is not a ledger time


def test_newest_timestamp_opens_the_database_read_only(monkeypatch, tmp_path):
    from llm_router import dashboard_data as dd

    db = tmp_path / "usage.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE usage (timestamp TEXT)")
    con.commit()
    con.close()
    opened = []
    real = sqlite3.connect

    def spy(target, *a, **k):
        opened.append(str(target))
        return real(target, *a, **k)

    monkeypatch.setattr(dd.sqlite3, "connect", spy)
    dd.newest_timestamp(db_path=db)
    assert opened and all("mode=ro" in t and "uri" not in t.split("?")[0] for t in opened), opened
