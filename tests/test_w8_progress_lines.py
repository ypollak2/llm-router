"""W8 (owner decision D-364): the two progress lines beside a blind NS.

  off-Claude  calls answered by a non-Claude model / all calls
  <Opus       Claude calls on Sonnet or Haiku / Claude calls

Same refresher pass, same window (7 d), same population (organic) as NS; a call is one unique API message,
sub-agents included. Each line carries its k/n, unit, window and population, and every surface that prints
it (``llm-router kpi``, the status line's extra row) prints the same words.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router import northstar as ns
from llm_router import session_kind, statusline_refresh as sr, statusline_tick as tick
from llm_router.commands import kpi

LOCAL = "ollama/qwen3.5:latest"
OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"
NOW = 1_791_234_000.0


@pytest.fixture
def sessions(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    session_kind._FOUND.clear()
    session_kind.tag_session("s-org", "/Users/someone/Projects/app", env={})
    session_kind.tag_session("s-res", "/Users/someone/work/scratchpad/p1", env={})
    yield {"organic": "s-org", "research": "s-res", "untagged": "s-none"}
    session_kind._FOUND.clear()


def _u(sid="s-org", kind=ns.UNIT_CLAUDE_MAIN, model=OPUS, ts=None, **kw):
    return {"session_id": sid, "kind": kind, "outcome": ns.OUTCOME_NOT_ROUTED, "lever": None,
            "ts": ts if ts is not None else time.time(), "model": model, **kw}


def _stream(monkeypatch, rows):
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False, **_: iter(rows))


def _mix(rows, include_research=False, win=None):
    from llm_router.proxy import ledger as pl
    from llm_router import session_kind as sk

    return kpi._call_mix(rows, kpi._allowed_kinds(include_research), sk.KindIndex(pl.read_rows()), win)


# ── the figures ──────────────────────────────────────────────────────────────────────────────────

def test_the_two_figures_from_a_hand_built_population(sessions):
    rows = ([_u(model=OPUS) for _ in range(10)]
            + [_u(kind=ns.UNIT_SIDECHAIN, model=SONNET) for _ in range(30)]
            + [_u(kind=ns.UNIT_SIDECHAIN, model=HAIKU) for _ in range(5)]
            + [_u(model="claude-fable-1")]                                               # a Claude call, no tier word
            + [_u(kind=ns.UNIT_ROUTED_MCP, model=LOCAL) for _ in range(2)]               # joined MCP calls
            + [_u(lever="proxy", model=LOCAL)]                                           # a proxy-served message
            + [_u(kind=ns.UNIT_USER_PROMPT, model=None) for _ in range(20)]              # no call
            + [_u(kind=ns.UNIT_ROUTED_MCP, model=None) for _ in range(3)])               # unjoined: in neither figure
    cm = _mix(rows)
    assert cm["outside_claude"] == {"k": 3, "n": 49}               # 3 off-Claude of 46 Claude + 3 off
    assert cm["below_opus"] == {"k": 35, "n": 46}                   # 30 Sonnet + 5 Haiku of 10+30+5+1 Claude calls
    assert cm["claude_by_family"] == {"opus": 10, "sonnet": 30, "haiku": 5, "claude-other": 1}
    assert cm["unclassified"] == 3 and cm["unit"] == "calls" and cm["population"] == "organic"


def test_only_the_organic_population_counts_and_research_widens_it_on_request(sessions):
    rows = [_u(sid="s-org") for _ in range(8)] + [_u(sid="s-res") for _ in range(5)] + [_u(sid="s-none") for _ in range(4)]
    assert _mix(rows)["below_opus"]["n"] == 8
    wide = _mix(rows, include_research=True)
    assert wide["below_opus"]["n"] == 13 and wide["population"] == "organic+research"   # untagged stays out


def test_the_window_cuts_the_same_units_ns_cuts(sessions):
    win = kpi._Window(NOW - 100, NOW)
    rows = [_u(ts=NOW - 10), _u(ts=NOW - 50), _u(ts=NOW - 500), _u(ts=NOW + 500)]
    assert _mix(rows, win=win)["below_opus"]["n"] == 2


def test_a_stamp_already_on_the_unit_is_trusted_like_ns_does(sessions):
    rows = [_u(session_id="x", session_kind="organic"), _u(session_id="x", session_kind="research"),
            _u(session_id="x", session_kind=None)]
    assert _mix(rows)["below_opus"]["n"] == 1


def test_the_population_is_the_one_ns_counts_minus_the_user_prompts(monkeypatch, sessions):
    """Same unit list, same filter: the calls plus the prompts plus the unclassified ARE NS's counted units."""
    rows = ([_u() for _ in range(40)] + [_u(kind=ns.UNIT_USER_PROMPT, model=None) for _ in range(15)]
            + [_u(kind=ns.UNIT_ROUTED_MCP, model=None) for _ in range(2)]
            + [_u(sid="s-res") for _ in range(9)])
    _stream(monkeypatch, rows)
    card = kpi.compute_scorecard(days=7)
    cm = card["call_mix"]
    assert card["joins"]["counted"] == 57
    assert cm["outside_claude"]["n"] + 15 + cm["unclassified"] == card["joins"]["counted"]


# ── the words ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("k,n,text", [
    (4, 7116, "0.06%"), (6181, 7112, "86.9%"), (0, 7116, "0.0%"), (7116, 7116, "100.0%"), (1, 100, "1.0%"),
    (1, 101, "0.99%"), (5, 1000, "0.50%"),
])
def test_the_share_text(k, n, text):
    assert kpi._mix_pct(k, n) == text and tick._mix_pct(k, n) == text


def test_each_line_names_its_k_over_n_unit_window_and_population():
    off = kpi._call_mix_segment("off-Claude", {"k": 4, "n": 7116}, "calls", "7d", "organic")
    low = kpi._call_mix_segment("<Opus", {"k": 6181, "n": 7112}, "Claude calls", "7d", "organic")
    assert off == "off-Claude 0.06% (4/7116 calls, 7d, organic)"
    assert low == "<Opus 86.9% (6181/7112 Claude calls, 7d, organic)"


def test_below_the_minimum_n_the_line_is_na_with_its_counts_never_a_share_of_a_handful():
    seg = kpi._call_mix_segment("off-Claude", {"k": 3, "n": kpi.MIN_N - 1}, "calls", "7d", "organic")
    assert seg == f"off-Claude n/a (3/{kpi.MIN_N - 1} calls, 7d, organic)" and "%" not in seg
    assert tick.MIX_MIN_N == kpi.MIN_N


def test_session_ids_are_in_the_card_only_when_the_status_line_refresher_asks(monkeypatch, sessions):
    _stream(monkeypatch, [_u() for _ in range(60)])
    assert "sessions" not in kpi.compute_scorecard(days=7)["call_mix"]               # `llm-router kpi --json` carries no id
    assert kpi.compute_scorecard(days=7, call_mix_sessions=True)["call_mix"]["sessions"] == {"s-org": 60}


def test_the_scorecard_prints_the_two_lines_only_when_a_call_was_counted(monkeypatch, sessions):
    _stream(monkeypatch, [])
    assert "off-Claude" not in kpi.render_scorecard(kpi.compute_scorecard(days=7))      # an empty window: unchanged text
    _stream(monkeypatch, [_u() for _ in range(60)] + [_u(kind=ns.UNIT_ROUTED_MCP, model=None)])
    text = kpi.render_scorecard(kpi.compute_scorecard(days=7))
    assert "  off-Claude 0.0% (0/60 calls, 7d, organic)" in text.splitlines()
    assert "  <Opus 0.0% (0/60 Claude calls, 7d, organic)" in text.splitlines()
    assert "1 call(s) with no known model are in neither figure" in text


# ── the cache and the tick ───────────────────────────────────────────────────────────────────────

def _card(**over):
    cm = {"unit": "calls", "population": "organic", "outside_claude": {"k": 4, "n": 7116},
          "below_opus": {"k": 6181, "n": 7112}, "unclassified": 0,
          "sessions": {"1a45a332-67b7-47bd-9378-2cfb4d47507c": 6871, "bad id;rm -rf": 5, "s2": 0, "s3": True}}
    cm.update(over)
    return {"window_days": 7, "call_mix": cm}


def test_the_cache_carries_both_figures_window_population_and_the_clean_session_counts():
    out = sr._call_mix(_card())
    assert out["outside_claude"] == {"k": 4, "n": 7116} and out["below_opus"] == {"k": 6181, "n": 7112}
    assert (out["window"], out["population"]) == ("7d", "organic")
    assert out["sessions"] == {"1a45a332-67b7-47bd-9378-2cfb4d47507c": 6871}      # unsafe id, zero and bool are dropped


@pytest.mark.parametrize("bad", [
    {"call_mix": None}, {"call_mix": "x"}, {},
    {"call_mix": {"population": "organic", "outside_claude": {"k": 5, "n": 3}, "below_opus": {"k": 1, "n": 2}}},
    {"call_mix": {"population": "organic", "outside_claude": {"k": True, "n": 3}, "below_opus": {"k": 1, "n": 2}}},
    {"call_mix": {"population": "organic", "outside_claude": {"k": 0, "n": 0}, "below_opus": {"k": 0, "n": 0}}},
    {"call_mix": {"population": "organic", "outside_claude": {"k": 1, "n": 2}}},
])
def test_an_unknown_call_mix_is_none_not_two_zeros(bad):
    assert sr._call_mix({"window_days": 7, **bad}) is None


def _cache(**over):
    return {"v": 1, "written_at": NOW - 5, "mode": "smart", "call_mix": sr._call_mix(_card()), **over}


def test_the_progress_row_is_the_two_lines_in_the_segment_style():
    row = tick.render_progress(_cache(), now=NOW)
    assert row == ("off-Claude 0.06% (4/7116 calls, 7d, organic) · "
                   "<Opus 86.9% (6181/7112 Claude calls, 7d, organic)")
    assert len(row) <= tick.MAX_CHARS


def test_every_surface_prints_the_same_words_for_the_same_figures():
    for k, n in [(4, 7116), (6181, 7112), (0, 80), (7, 49), (50, 50)]:
        for label, noun in (("off-Claude", "calls"), ("<Opus", "Claude calls")):
            fig = {"k": k, "n": n}
            assert tick._mix_segment(label, fig, noun, "7d", "organic") == \
                kpi._call_mix_segment(label, fig, noun, "7d", "organic")


@pytest.mark.parametrize("cache", [
    None, [], {"written_at": NOW - 5}, {"written_at": NOW - 5, "call_mix": "x"},
    {"written_at": NOW - 3600, "call_mix": sr._call_mix(_card())},                          # a stale cache is not data
    {"written_at": NOW + 3600, "call_mix": sr._call_mix(_card())},                          # nor one from the future
    {"written_at": NOW - 5, "call_mix": {**sr._call_mix(_card()), "window": None}},          # a share with no window
    {"written_at": NOW - 5, "call_mix": {**sr._call_mix(_card()), "window": "7d; rm"}},
    {"written_at": NOW - 5, "call_mix": {**sr._call_mix(_card()), "population": None}},      # ... or no population
    {"written_at": NOW - 5, "call_mix": {**sr._call_mix(_card()), "population": "\x1b[31mx"}},
    {"written_at": NOW - 5, "call_mix": {**sr._call_mix(_card()), "below_opus": {"k": 2, "n": 1}}},
    {"written_at": NOW - 5, "call_mix": {**sr._call_mix(_card()), "outside_claude": {"k": 1.5, "n": 100}}},
])
def test_no_row_at_all_when_the_cache_cannot_back_it(cache):
    assert tick.render_progress(cache, now=NOW) is None
    assert len(tick.render_rows(cache, now=NOW)) == 1


def test_render_rows_is_the_status_line_then_the_progress_row():
    rows = tick.render_rows(_cache(ns={"pct": None, "n": 2097, "window": "7d", "blind": True, "k": 0}), now=NOW)
    assert len(rows) == 2 and rows[0].startswith("llm-router · smart · NS strict 0/2097 (blind) 7d units")
    assert rows[1].startswith("off-Claude ") and all(len(tick._ANSI.sub("", r)) <= tick.MAX_CHARS for r in rows)


def test_the_tick_script_prints_both_rows_from_the_cache_file(tmp_path):
    home = tmp_path / "router_home"
    home.mkdir()
    cache = _cache(written_at=time.time() - 5, ns={"pct": None, "n": 2097, "window": "7d", "blind": True, "k": 0})
    (home / "statusline_cache.json").write_text(json.dumps(cache))
    tick_py = Path(tick.__file__)
    env = {"PATH": os.environ.get("PATH", ""), "LLM_ROUTER_HOME": str(home), "HOME": str(tmp_path),
           "LLM_ROUTER_STATUSLINE_REFRESH_CMD": "true", "NO_COLOR": "1"}
    out = subprocess.run([sys.executable, "-I", "-S", str(tick_py)], input=b"", env=env, capture_output=True,
                         timeout=30).stdout.decode().splitlines()
    assert len(out) == 2, out
    assert "NS strict 0/2097 (blind) 7d units" in out[0]
    assert out[1] == "off-Claude 0.06% (4/7116 calls, 7d, organic) · <Opus 86.9% (6181/7112 Claude calls, 7d, organic)"


def test_end_to_end_from_the_unit_stream_to_the_two_rows(monkeypatch, sessions):
    rows = ([_u() for _ in range(20)] + [_u(kind=ns.UNIT_SIDECHAIN, model=SONNET) for _ in range(75)]
            + [_u(kind=ns.UNIT_ROUTED_MCP, model=LOCAL) for _ in range(5)]
            + [_u(kind=ns.UNIT_USER_PROMPT, model=None) for _ in range(30)])
    _stream(monkeypatch, rows)
    data = sr.build(time.time())
    assert data["ns"]["blind"] is True and data["ns"]["n"] == 130 and data["ns"]["k"] == 0
    assert data["call_mix"]["outside_claude"] == {"k": 5, "n": 100} and data["call_mix"]["below_opus"] == {"k": 75, "n": 95}
    out = tick.render_rows(data, now=time.time())
    assert "NS strict 0/130 (blind) 7d units" in out[0]
    # W9: the refresher also counted Codex model responses (the label says so; `llm-router kpi` keeps "off-Claude").
    # This home has no attempt ledger, so the local figure is unknown: n/a with its window, never "0 attempts"
    assert out[1] == ("routed (incl. Codex turns and binds) 5.0% (5/100 calls, 7d, organic) · <Opus 78.9% (75/95 Claude calls, 7d,"
                      " organic) · local OK n/a (7d, organic)")


def test_an_unreadable_ledger_makes_the_seven_day_figures_na_not_a_count_without_the_extras(monkeypatch, sessions):
    """W9b: the Codex turns, the bind jobs and the local attempts are asked for and cannot be built (an unreadable bind
    ledger): the routed share and local OK are n/a. The Claude-side `<Opus` figure does not need them and stays."""
    from llm_router import session_routing

    def boom(*a, **k):
        raise session_routing.LedgerUnreadable("text_job_binds.jsonl")

    monkeypatch.setattr(session_routing, "window_extras", boom)
    rows = [_u() for _ in range(20)] + [_u(kind=ns.UNIT_SIDECHAIN, model=SONNET) for _ in range(75)] \
        + [_u(kind=ns.UNIT_ROUTED_MCP, model=LOCAL) for _ in range(5)]
    _stream(monkeypatch, rows)
    data = sr.build(time.time())
    assert data["call_mix"]["detail_unavailable"] is True
    out = tick.render_rows(data, now=time.time())
    assert out[1] == ("routed (incl. Codex turns and binds) n/a (7d, organic) · <Opus 78.9% (75/95 Claude calls, 7d,"
                      " organic) · local OK n/a (7d, organic)")
    assert "off-Claude" not in out[1] and "5.0%" not in out[1]


def test_the_widest_realistic_status_row_still_fits_and_keeps_its_hooks_warning():
    """Row 3 grew by the blind NS's labels (``NS strict 0/n (blind) 7d units``): the slow-hooks warning at its
    tail is what a cut line loses first, so the widest realistic row must fit with it."""
    cache = {"v": 1, "written_at": NOW - 5, "mode": "enforce", "ns": {"pct": None, "n": 9_999_999, "window": "7d", "blind": True, "k": 0},
             "codex": {"used": 15, "budget": 15, "resets_at": NOW + 3600}, "local_check": "local n/p",
             "hooks_slow": {"hook": "agent-depth-release", "p95_ms": 12345.0}}
    usage = {"session_pct": 100, "weekly_pct": 100, "sonnet_pct": 100, "updated_at": NOW - 7200}
    row = tick._ANSI.sub("", tick.render(cache, now=NOW, usage=usage))
    assert len(row) <= tick.MAX_CHARS and "NS strict 0/9999999 (blind) 7d units" in row, (len(row), row)
    assert row.endswith("⚠ hooks p95 12.3s agent-depth-release"), row
