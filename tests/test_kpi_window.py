"""`llm-router kpi --since/--until`: an absolute window replaces ``--days``.

A relative window empties as the ledgers go quiet (the proxy has had no Claude Code
traffic since 2026-10-06 10:41 BST), so every historical check pins its window. Rows
after ``--until`` and rows before ``--since`` must not count. Without the flags the
output is what it was (the pre-O3 goldens in test_kpi_offload_share.py pin that, and
``test_no_flags_adds_no_window_key`` pins the new key's absence).
"""
from __future__ import annotations

import json
import time

import pytest

from llm_router import hook_latency as hl
from llm_router import northstar as ns
from llm_router import session_kind
from llm_router.commands import kpi

from tests import _o3_fixture as fx
from tests._o3_fixture import NOW, proxy_row

DAY = 86400.0
SINCE = NOW - 3 * DAY
UNTIL = NOW - 1 * DAY


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
    session_kind.tag_session("s-org", "/Users/someone/Projects/app", env={})
    yield
    session_kind._FOUND.clear()
    failopen.reset_unpersisted()
    failopen.reset_cache()


def _ledger(rows):
    from llm_router.proxy import ledger as pl

    fx.write_jsonl(pl.ledger_path(), rows)


def _rows_in_three_bands(n_before=60, n_in=70, n_after=80):
    """before ``since`` = haiku, inside the window = sonnet, after ``until`` = opus, so
    the tier mix says which band a row was counted from."""
    out, i = [], 0
    for tier, n, t0 in (("haiku", n_before, SINCE - 3600 - n_before),
                        ("sonnet", n_in, SINCE + 3600),
                        ("opus", n_after, UNTIL + 3600)):
        for k in range(n):
            out.append(proxy_row(i, tier=tier, kind="organic", ts=t0 + k))
            i += 1
    return out


def test_proxy_rows_outside_the_window_do_not_count():
    _ledger(_rows_in_three_bands())
    card = kpi.compute_scorecard(since=SINCE, until=UNTIL, schema_since=1.0)
    d4 = card["kpis"]["D4"]
    assert d4["calls_by_tier"] == {"sonnet": 70}          # n=70 checked, nothing else
    assert d4["seen"] == 70
    g3 = card["kpis"]["G3"]
    assert g3["window_rows"] == 70
    assert card["generated_ts"] == UNTIL                   # "now" is the window's end


def test_a_row_exactly_on_each_edge_is_inside():
    rows = [proxy_row(0, tier="sonnet", kind="organic", ts=SINCE + 1.0),
            proxy_row(1, tier="sonnet", kind="organic", ts=UNTIL),
            proxy_row(2, tier="sonnet", kind="organic", ts=SINCE - 1.0),
            proxy_row(3, tier="sonnet", kind="organic", ts=UNTIL + 1.0)]
    _ledger(rows)
    pop = kpi._proxy_population(rows, (UNTIL - SINCE) / DAY, frozenset({"organic"}), UNTIL, until=UNTIL)
    assert sorted(r["ts"] for r in pop["window"]) == [SINCE + 1.0, UNTIL]


def test_o3_uses_only_turns_inside_the_window():
    rows = _rows_in_three_bands()
    _ledger(rows)
    o3 = kpi.compute_scorecard(since=SINCE, until=UNTIL)["o3"]
    assert o3["breakdown"]["n"] == 70 and o3["breakdown"]["claude_n"] == 70
    assert o3["breakdown"]["haiku_n"] == 0
    # same ledger, a window that covers only the haiku band
    h = kpi.compute_scorecard(since=SINCE - 7200, until=SINCE - 1800)["o3"]
    assert h["breakdown"]["haiku_n"] == 60 and h["breakdown"]["n"] == 60


def test_ns_d1_units_outside_the_window_do_not_count(monkeypatch):
    def unit(ts):
        return {"session_id": "s-org", "kind": sorted(ns.ATTEMPTED_KINDS)[0],
                "outcome": ns.OUTCOME_USED, "lever": None, "ts": ts}
    units = ([unit(SINCE + 100 + i) for i in range(55)]
             + [unit(UNTIL + 100 + i) for i in range(45)]
             + [unit(SINCE - 100 - i) for i in range(40)])
    seen_days = []

    def fake_units(days=30, session_id=None, root=None, backfill=False):
        seen_days.append(days)
        return iter(units)
    monkeypatch.setattr(ns, "units", fake_units)
    card = kpi.compute_scorecard(since=SINCE, until=UNTIL)
    assert card["joins"]["window_units"] == 55 and card["joins"]["counted"] == 55
    assert card["kpis"]["NS"]["n"] == 55
    # the reader's own cutoff is relative to the wall clock: it must reach back to SINCE
    assert seen_days and seen_days[0] >= (time.time() - SINCE) / DAY


def test_hook_latency_rows_outside_the_window_do_not_count(tmp_path):
    for k in range(60):
        hl.record("auto-route", "UserPromptSubmit", 100.0, now=SINCE + 60 + k)
    for k in range(40):
        hl.record("auto-route", "UserPromptSubmit", 9000.0, now=UNTIL + 60 + k)
    g1 = kpi.compute_scorecard(since=SINCE, until=UNTIL)["kpis"]["G1_hook"]
    assert g1["hooks"]["auto-route"]["n"] == 60
    assert g1["hooks"]["auto-route"]["p50_ms"] == 100.0


def test_window_is_reported_in_json_and_text():
    _ledger(_rows_in_three_bands())
    card = kpi.compute_scorecard(since=SINCE, until=UNTIL)
    assert card["window"] == {"since": kpi._iso(SINCE), "until": kpi._iso(UNTIL),
                              "since_ts": SINCE, "until_ts": UNTIL}
    assert card["window_days"] == pytest.approx(2.0)
    text = kpi.render_scorecard(card)
    assert f"window={kpi._iso(SINCE)}..{kpi._iso(UNTIL)}" in text
    health = kpi.render_health(kpi.compute_health(card))
    assert f"window={kpi._iso(SINCE)}..{kpi._iso(UNTIL)}" in health


def test_no_flags_adds_no_window_key():
    _ledger(fx.baseline_rows())
    card = kpi.compute_scorecard(days=7, now=NOW)
    assert "window" not in card and card["window_days"] == 7
    assert "window=7d" in kpi.render_scorecard(card)


def test_one_edge_alone_or_a_backwards_window_is_refused():
    with pytest.raises(ValueError, match="both"):
        kpi.compute_scorecard(since=SINCE)
    with pytest.raises(ValueError, match="both"):
        kpi.compute_scorecard(until=UNTIL)
    with pytest.raises(ValueError, match="before"):
        kpi.compute_scorecard(since=UNTIL, until=SINCE)


def test_cli_since_until_replace_days(capsys):
    _ledger(_rows_in_three_bands())
    rc = kpi.cmd_kpi(["--days", "1", "--since", kpi._iso(SINCE), "--until", kpi._iso(UNTIL), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["window"]["since"] == kpi._iso(SINCE)
    assert data["kpis"]["D4"]["calls_by_tier"] == {"sonnet": 70}


def test_cli_accepts_epoch_seconds_and_a_bare_date(capsys):
    rc = kpi.cmd_kpi(["--since", "2026-09-29", "--until", "1791279712", "--json"])
    assert rc == 0
    w = json.loads(capsys.readouterr().out)["window"]
    assert w["since"] == "2026-09-29T00:00:00Z" and w["until_ts"] == 1791279712.0


@pytest.mark.parametrize("argv", [["--since", "2026-09-29"], ["--until", "2026-09-29"],
                                  ["--since", "nope", "--until", "2026-09-29"],
                                  ["--since", "2026-10-06", "--until", "2026-09-29"]])
def test_cli_bad_window_is_an_argparse_error(argv, capsys):
    with pytest.raises(SystemExit) as e:
        kpi.cmd_kpi(argv)
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--since" in err or "--until" in err
