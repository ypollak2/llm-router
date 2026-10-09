"""Session-kind override file (``session_kind_overrides.json``): the owner can say
"this session is research" and every reader obeys it, even where the ledger already
stamped the session organic.

Why: session b9f04425 is one research session, yet its proxy rows are stamped (or
tagged) ``organic``; it supplies 99.3% of the organic turn-first rows in the pinned
window W0, so every live gate would have measured it. Precedence under test:
override file, then the tag file, then the row's own stamp.
"""
from __future__ import annotations

import json

import pytest

from llm_router import edit_ledger, northstar as ns, offload_share as osh
from llm_router import paths, session_kind, session_kind_backfill as skb
from llm_router.commands import kpi
from llm_router.proxy import ledger as pl

from tests import _o3_fixture as fx
from tests._o3_fixture import NOW, proxy_row

SID = "b9f04425-6176-4bea-b46e-1cfdfd44785e"
OTHER = "s-other"


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


def _write_overrides(obj) -> None:
    p = session_kind.overrides_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


def _override(sid=SID, kind="research", reason="p_eval REPORT.txt: this research session"):
    _write_overrides({sid: {"kind": kind, "reason": reason}})


# ── the file ─────────────────────────────────────────────────────────────────

def test_overrides_path_is_in_the_state_dir():
    assert session_kind.overrides_path() == paths.state_path("session_kind_overrides.json")


def test_no_file_means_no_overrides():
    assert session_kind.overrides() == {}
    assert session_kind.override_of(SID) is None


def test_a_valid_entry_is_read():
    _override()
    assert session_kind.overrides() == {SID: {"kind": "research",
                                              "reason": "p_eval REPORT.txt: this research session"}}
    assert session_kind.override_of(SID) == "research"
    assert session_kind.override_of(OTHER) is None
    assert session_kind.override_of(None) is None


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "null", '"research"', "{"])
def test_a_broken_file_is_no_overrides_not_a_crash(raw):
    _write_overrides(raw)
    assert session_kind.overrides() == {}
    assert session_kind.kind_of(SID) is None


def test_a_bad_entry_is_skipped_and_the_good_one_kept():
    _write_overrides({"a": {"kind": "bogus"}, "b": "research", "c": {"reason": "no kind"},
                      "d": {"kind": "Research "}, "e": {"kind": "harness", "reason": "x"}})
    assert set(session_kind.overrides()) == {"d", "e"}
    assert session_kind.override_of("d") == "research"  # same normalising as the env override
    assert session_kind.override_of("e") == "harness"


def test_editing_the_file_is_seen_without_a_restart():
    _override(kind="research")
    assert session_kind.override_of(SID) == "research"
    _write_overrides({SID: {"kind": "harness", "reason": "a longer reason than before"}})
    assert session_kind.override_of(SID) == "harness"
    session_kind.overrides_path().unlink()
    assert session_kind.override_of(SID) is None


# ── precedence ───────────────────────────────────────────────────────────────

def test_override_beats_the_tag_file():
    session_kind.tag_session(SID, "/Users/someone/Projects/app", env={})
    assert session_kind.kind_of(SID) == "organic"
    _override()
    assert session_kind.kind_of(SID) == "research"


def test_a_session_without_an_override_is_unchanged():
    _override()
    session_kind.tag_session(OTHER, "/Users/someone/Projects/app", env={})
    assert session_kind.kind_of(OTHER) == "organic"
    assert session_kind.kind_of("never-tagged") is None


def test_kind_index_override_beats_tag_stamp_and_ledger():
    session_kind.tag_session(SID, "/Users/someone/Projects/app", env={})
    idx = session_kind.KindIndex([{"session_id": SID, "session_kind": "organic"}], backfill=False)
    assert idx.resolve(SID, stamp="organic").kind == "organic"
    _override()
    res = session_kind.KindIndex([{"session_id": SID, "session_kind": "organic"}],
                                 backfill=False).resolve(SID, stamp="organic")
    assert (res.kind, res.source) == ("research", session_kind.SOURCE_OVERRIDE)
    assert res.ledger_disagrees is True   # the rows say organic: that stays visible
    other = session_kind.KindIndex([{"session_id": OTHER, "session_kind": "organic"}],
                                   backfill=False).resolve(OTHER, stamp="organic")
    assert (other.kind, other.source) == ("organic", session_kind.SOURCE_STAMP)


def test_override_resolves_a_session_that_had_no_evidence_at_all():
    _override()
    res = session_kind.KindIndex(backfill=False).resolve(SID)
    assert (res.kind, res.source) == ("research", session_kind.SOURCE_OVERRIDE)


def test_tag_session_still_writes_what_the_hook_saw_and_reports_the_effective_kind():
    _override()
    kind = session_kind.tag_session(SID, "/Users/someone/Projects/app", env={})
    assert kind == "research"                                    # effective
    tag = json.loads(paths.state_path(f"session_kind_{SID}.json").read_text())
    assert tag["kind"] == "organic"                              # the hook's own classification
    assert session_kind.tag_kind_of(SID) == "organic"


def test_writers_stamp_the_overridden_kind(tmp_path):
    session_kind.tag_session(SID, "/Users/someone/Projects/app", env={})
    _override()
    assert edit_ledger._session_kind_of(SID) == "research"


# ── readers: every KPI obeys it ──────────────────────────────────────────────

def _write_ledger(rows):
    fx.write_jsonl(pl.ledger_path(), rows)


def _rows(sid, n, kind="organic", tier="sonnet", t0=None):
    return [proxy_row(i, sid=sid, tier=tier, kind=kind, ts=(t0 if t0 is not None else NOW - 3600) + i)
            for i in range(n)]


def test_o3_counts_an_overridden_session_as_research_and_others_as_before():
    session_kind.tag_session(SID, "/Users/someone/Projects/app", env={})
    session_kind.tag_session(OTHER, "/Users/someone/Projects/app", env={})
    _write_ledger(_rows(SID, 90, tier="haiku") + _rows(OTHER, 60, t0=NOW - 7200))
    before = kpi.compute_scorecard(days=7, now=NOW)["o3"]["breakdown"]
    assert (before["n"], before["haiku_n"]) == (150, 90)
    _override()
    card = kpi.compute_scorecard(days=7, now=NOW)
    after = card["o3"]["breakdown"]
    assert (after["n"], after["haiku_n"], after["claude_n"]) == (60, 0, 60)   # only OTHER
    assert card["o3"]["excluded"]["other_kind"] == 90
    both = kpi.compute_scorecard(days=7, now=NOW, include_research=True)["o3"]["breakdown"]
    assert both["n"] == 150                                     # still visible with --include research


def test_ns_d1_resolve_the_override_through_the_scorecard(monkeypatch):
    kind0 = sorted(ns.ATTEMPTED_KINDS)[0]
    units = [{"session_id": SID, "kind": kind0, "outcome": ns.OUTCOME_USED, "lever": None,
              "ts": NOW - 10} for _ in range(60)]
    units += [{"session_id": OTHER, "kind": kind0, "outcome": ns.OUTCOME_USED, "lever": None,
               "ts": NOW - 10} for _ in range(55)]
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False: iter(units))
    session_kind.tag_session(SID, "/Users/someone/Projects/app", env={})
    session_kind.tag_session(OTHER, "/Users/someone/Projects/app", env={})
    assert kpi.compute_scorecard(days=7, now=NOW)["joins"]["counted"] == 115
    _override()
    card = kpi.compute_scorecard(days=7, now=NOW)
    assert card["joins"]["counted"] == 55
    assert card["joins"]["joined_by_kind"] == {"organic": 55, "research": 60}
    assert card["kpis"]["NS"]["n"] == 55 and card["kpis"]["D1"]["n"] == 55


def test_d4_proxy_population_obeys_the_override_over_the_rows_own_stamp():
    rows = _rows(SID, 70, kind="organic", tier="opus") + _rows(OTHER, 60, kind="organic", tier="sonnet")
    # a row of the overridden session written before tagging existed: no stamp at all
    rows += [proxy_row(900 + i, sid=SID, tier="opus", kind=None, ts=NOW - 3000 + i) for i in range(5)]
    _write_ledger(rows)
    d4 = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["D4"]
    assert d4["calls_by_tier"] == {"opus": 70, "sonnet": 60}
    _override()
    pop = kpi._proxy_population(rows, 7, frozenset({"organic"}), NOW)
    assert (len(pop["allowed"]), pop["other_kind"], pop["untagged"]) == (60, 75, 0)
    d4 = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["D4"]
    assert d4["calls_by_tier"] == {"sonnet": 60}


def test_g3_measures_the_writer_so_the_override_does_not_change_it():
    """G3 asks whether the ledger RECORDED a session_kind. An override does not change what
    was written, so G3 must read the same with and without one."""
    rows = fx.baseline_rows(120)
    for r in rows:
        r["session_id"] = SID if r["tier_proposed"] == "opus" else OTHER
        r.update(tier_retry=False)
    _write_ledger(rows)
    g3_before = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["G3"]
    assert g3_before["measurable"] is True
    _override()
    g3_after = kpi.compute_scorecard(days=7, now=NOW)["kpis"]["G3"]

    def audit(g3):  # the proxy tier-field audit; "prd"/"lines" are the per-writer verdict
        return {k: v for k, v in g3.items() if k not in ("prd", "lines")}

    assert audit(g3_after) == audit(g3_before)
    # The per-writer verdict (P0.8-c) scores the organic population, where the owner's
    # override IS the session's kind: the overridden session's rows move out, counted apart.
    before, after = g3_before["prd"]["writers"]["proxy"], g3_after["prd"]["writers"]["proxy"]
    moved = after["excluded"].get("research", 0)
    assert moved > 0 and before["excluded"].get("research", 0) == 0
    assert after["n"] == before["n"] - moved


def test_override_reaches_usage_outcome_redo_resolution():
    from llm_router import usage_outcome as uo

    session_kind.tag_session(SID, "/Users/someone/Projects/app", env={})
    _override()
    assert uo.session_kind.kind_of(SID) == "research"


# ── the backfill must not treat an override as a gap or as live truth ────────

def test_backfill_does_not_write_a_row_for_an_overridden_session(tmp_path):
    _override()
    ledger = tmp_path / "proxy_calls.jsonl"
    ledger.write_text(json.dumps({"session_id": SID, "ts": 1.0}) + "\n")
    res = skb.backfill_sessions(dry_run=True, home=tmp_path, root=tmp_path / "none",
                                sidecar=tmp_path / "side.jsonl", now=NOW)
    assert res["candidates"] == 1 and res["new_rows"] == 0
    assert res["skipped_live_tag"] == 1


def test_validate_against_live_uses_the_tag_file_not_the_override(tmp_path):
    session_kind.tag_session(SID, "/Users/someone/Projects/app", env={})
    _override()
    ledger = tmp_path / "proxy_calls.jsonl"
    ledger.write_text(json.dumps({"session_id": SID, "ts": 1.0}) + "\n")
    out = skb.validate_against_live(home=tmp_path, root=tmp_path / "none")
    assert out["n"] == 1 and list(out["confusion"]) == ["organic"]


def test_build_units_obeys_a_kind_of_that_resolves_overrides():
    """The O3 builder takes the resolver as an argument; the scorecard passes the index's."""
    _override()
    idx = session_kind.KindIndex(backfill=False)
    rows = _rows(SID, 3, kind="organic")
    built = osh.build_units(rows, [], now=NOW, days=7, allowed=frozenset({"organic"}),
                            kind_of=lambda sid, stamp: idx.resolve(sid, stamp=stamp).kind)
    assert built["units"] == [] and built["other_kind"] == 3
