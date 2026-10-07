"""`llm-router kpi`: the scorecard prints what the data supports and nothing else.

Primary rule under test (project CLAUDE.md): unknown is never rendered as 0. Every
KPI with no data must say "not measurable: <reason>", and a KPI with too little
data must say "too few to tell". The numeric tests pin exact values on synthetic
inputs, so a pass shows the command found something to count.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from llm_router import northstar as ns
from llm_router import session_kind, usage_outcome
from llm_router.commands import kpi
from llm_router.proxy import ledger as pl

ALL_KEYS = ("NS", "O1", "O2", "D1", "D2", "D3", "D4", "D5",
            "G1_hook", "G1_proxy", "G2", "G3", "G4")


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_real_transcripts(monkeypatch, tmp_path):
    """Every test reads an empty transcript dir unless it patches the readers, so
    the suite never scans (or depends on) the operator's real ~/.claude/projects."""
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    # failopen keeps process-global in-memory counts that another test on the same
    # xdist worker may have left behind; G2 must start from nothing.
    from llm_router import failopen

    failopen.reset_unpersisted()
    failopen.reset_cache()
    yield
    failopen.reset_unpersisted()
    failopen.reset_cache()


def _tag(sid: str, cwd: str) -> None:
    session_kind.tag_session(sid, cwd, env={})


@pytest.fixture
def sessions():
    """An organic, a research and an untagged session id (tags are real files in
    the suite's isolated LLM_ROUTER_HOME)."""
    session_kind._FOUND.clear()
    _tag("s-org", "/Users/someone/Projects/app")
    _tag("s-res", "/Users/someone/work/scratchpad/p1")
    yield {"organic": "s-org", "research": "s-res", "untagged": "s-none"}
    session_kind._FOUND.clear()


def _unit(sid, kind="local_edit", outcome=ns.OUTCOME_USED, lever=None):
    return {"session_id": sid, "kind": kind, "outcome": outcome, "lever": lever, "ts": time.time()}


def _units(monkeypatch, rows):
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False: iter(rows))


def _row(sid="s-org", **kw):
    base = {"ts": time.time(), "session_id": sid, "session_kind": "organic", "tier": "sonnet",
            "tier_proposed": "sonnet", "tier_policy_version": "v1", "tier_retry": False,
            "added_latency_s": 0.01}
    base.update(kw)
    return base


def _write_proxy_rows(rows):
    path = pl.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _kpis(**kw):
    return kpi.compute_scorecard(days=7, **kw)["kpis"]


def _diag(**kw):
    return kpi.compute_scorecard(days=7, **kw)["kpis_diag"]


def _first_attempted_kind():
    return sorted(ns.ATTEMPTED_KINDS)[0]


# ── the rule: empty data is never a number ──────────────────────────────────

def test_empty_data_is_not_measurable_never_a_false_zero():
    k = _kpis()
    assert set(k) == set(ALL_KEYS)  # something was checked: all 13 lines exist
    for key in ALL_KEYS:
        assert k[key]["value"].startswith("not measurable: "), (key, k[key])
        assert k[key]["measurable"] is False and k[key]["n"] is None, key
    # G4's old point-in-time reading survives as a detail line, not as the value.
    assert any("point-in-time" in ln for ln in k["G4"]["lines"])


def test_empty_rendering_has_no_bare_zero_rates():
    text = kpi.render_scorecard(kpi.compute_scorecard(days=7))
    assert text.count("not measurable") >= 12
    assert not re.search(r"\b0(\.0+)?%", text)
    assert "$0.00" not in text


def test_below_fifty_says_too_few_to_tell(monkeypatch, sessions):
    _units(monkeypatch, [_unit(sessions["organic"]) for _ in range(kpi.MIN_N - 1)])
    k = _kpis()
    for key in ("NS", "D1"):
        assert k[key]["value"] == f"too few to tell (n={kpi.MIN_N - 1})", key
        assert k[key]["measurable"] is False


# ── NS / D1 / D2 ────────────────────────────────────────────────────────────

def _mixed_units(sid):
    attempted = _first_attempted_kind()
    rows = [_unit(sid, attempted, ns.OUTCOME_USED) for _ in range(48)]
    rows += [_unit(sid, attempted, ns.OUTCOME_REDO) for _ in range(16)]
    rows += [_unit(sid, "claude_only", ns.OUTCOME_NOT_ROUTED) for _ in range(16)]
    return rows


def test_ns_d1_d2_exact_values(monkeypatch, sessions):
    _units(monkeypatch, _mixed_units(sessions["organic"]))
    k = _kpis()
    # M0.2: NS and D2 count strict-used only; these units carry no verify record, so 0.
    assert k["NS"]["value"] == "0.0% (n=80)"
    assert k["D1"]["value"] == "80.0% (n=80)"      # 64 attempted / 80 units
    assert k["D2"]["value"] == "0.0% (n=64)"
    # the old heuristic numerators, kept as diagnostics
    d = _diag()
    assert d["NS_heuristic"]["value"] == "60.0% (n=80)"      # 48 used / 80 units
    assert d["D2_heuristic"]["value"] == "75.0% (n=64)"      # 48 used / 64 attempted


def test_research_and_untagged_sessions_are_excluded_by_default(monkeypatch, sessions):
    rows = _mixed_units(sessions["organic"])
    rows += _mixed_units(sessions["research"]) * 3
    rows += _mixed_units(sessions["untagged"]) * 3
    _units(monkeypatch, rows)
    assert _kpis()["NS"]["value"] == "0.0% (n=80)"
    assert _diag()["NS_heuristic"]["value"] == "60.0% (n=80)"


def test_all_untagged_units_explain_why_they_are_not_counted(monkeypatch, sessions):
    _units(monkeypatch, _mixed_units(sessions["untagged"]))
    k = _kpis()
    for key in ("NS", "D1", "D2"):
        assert k[key]["value"].startswith("not measurable: 80 unit(s) in window, all from"), key
        assert "never counted as organic" in k[key]["value"]


def test_include_research_widens_the_population_but_never_untagged(monkeypatch, sessions):
    rows = _mixed_units(sessions["organic"]) + _mixed_units(sessions["research"])
    rows += _mixed_units(sessions["untagged"])
    _units(monkeypatch, rows)
    assert _kpis(include_research=True)["NS"]["value"] == "0.0% (n=160)"
    assert _diag(include_research=True)["NS_heuristic"]["value"] == "60.0% (n=160)"


def test_proxy_lever_counts_as_attempted(monkeypatch, sessions):
    sid = sessions["organic"]
    rows = [_unit(sid, "claude_only", ns.OUTCOME_USED, lever="proxy") for _ in range(50)]
    _units(monkeypatch, rows)
    assert _kpis()["D1"]["value"] == "100.0% (n=50)"


# ── D3 ──────────────────────────────────────────────────────────────────────

def test_d3_redo_rate_counts_decided_events_only(monkeypatch):
    def v(outcome, kind="organic"):
        return {"outcome": outcome, "session_kind": kind}

    rows = ([v(usage_outcome.OUTCOME_USED)] * 45 + [v(usage_outcome.OUTCOME_REDONE)] * 15
            + [v(usage_outcome.OUTCOME_UNKNOWN)] * 30
            + [v(usage_outcome.OUTCOME_REDONE, "research")] * 99)
    monkeypatch.setattr(usage_outcome, "judge_recent", lambda days=7, root=None: rows)
    d3 = _kpis()["D3"]
    assert d3["value"] == "25.0% (n=60)"           # 15 redone / (45 + 15); unknown is not in n
    assert d3["unknown_window_open"] == 30


def test_d3_all_unknown_is_not_measurable(monkeypatch):
    rows = [{"outcome": usage_outcome.OUTCOME_UNKNOWN, "session_kind": "organic"}] * 80
    monkeypatch.setattr(usage_outcome, "judge_recent", lambda days=7, root=None: rows)
    assert _kpis()["D3"]["value"].startswith("not measurable: ")


# ── proxy-ledger KPIs: D4, G1, G3 ───────────────────────────────────────────

def test_d4_tier_mix_and_g3_completeness():
    rows = [_row(tier="opus") for _ in range(10)] + [_row(tier="sonnet") for _ in range(40)]
    rows += [_row(tier="haiku") for _ in range(50)]
    rows += [_row(session_kind="research") for _ in range(500)]   # excluded
    bad = _row()
    del bad["tier_retry"]                                         # one incomplete organic row
    rows.append(bad)
    _write_proxy_rows(rows)
    k = _kpis()
    assert k["D4"]["value"].startswith("haiku=49.5%, sonnet=40.6%, opus=9.9%")  # n=101 organic
    assert k["D4"]["n"] == 101
    # G3 is not session-kind filtered (completeness is the writer's property, and the
    # tag is one of the fields under test): all 601 rows count, 600 carry every key.
    assert k["G3"]["n"] == 601
    assert k["G3"]["value"].startswith("99.8% (n=601; ")
    assert _kpis(include_research=True)["G3"]["n"] == 601
    assert _kpis(include_research=True)["D4"]["n"] == 601         # --include widens D4, not G3


def test_g3_null_decision_fields_do_not_count_as_complete():
    """A key that is PRESENT but null means the field was never computed --
    that is not the same as a complete row (G3 target >=99%)."""
    rows = [_row(tier_proposed=None, tier_policy_version=None, tier_retry=None) for _ in range(60)]
    _write_proxy_rows(rows)
    g3 = _kpis()["G3"]
    assert g3["value"].startswith("0.0% (n=60; ")
    # The two decision fields were never computed; tier_retry's null is its legal value.
    assert g3["fields"]["tier_policy_version"]["coverage"] == 0.0
    assert g3["fields"]["tier_proposed"]["coverage"] == 0.0
    assert g3["fields"]["tier_retry"]["coverage"] == 1.0


def test_untagged_proxy_rows_are_not_organic_for_d4_but_count_against_g3():
    """Untagged is never organic (D4/G1 exclude it), and it is a completeness failure
    (G3 counts it): filtering G3 by the tag would hide the very rows it must report."""
    _write_proxy_rows([_row(session_kind=None) for _ in range(100)])
    k = _kpis()
    assert k["D4"]["value"].startswith("not measurable: ")
    assert k["G3"]["value"].startswith("0.0% (n=100; ")
    assert k["G3"]["fields"]["session_kind"]["coverage"] == 0.0


def test_g1_proxy_reports_tier_decision_s_split_turn_first_and_continuation():
    """G1_proxy read ``added_latency_s``, which is 0.0 on every forwarded row, so it
    printed 0 ms (BUGS.md). It now reads ``tier_decision_s``, split by turn-first
    (``step_class`` != continuation) and continuation, side calls left out."""
    first = [_row(tier_decision_s=(i + 1) / 1000, added_latency_s=0.0) for i in range(60)]
    cont = [_row(step_class="continuation", tier_decision_s=0.004, added_latency_s=0.0)
            for _ in range(60)]
    side = [_row(tier_reason="side_call", tier_decision_s=5.0, added_latency_s=0.0)
            for _ in range(10)]
    _write_proxy_rows(first + cont + side)
    g1 = _kpis()["G1_proxy"]
    # 60 values 1..60 ms: nearest rank on n-1 gives p50 = 31 ms, p95 = 57 ms.
    assert g1["value"] == ("turn-first p50=31ms p95=57ms (n=60) | "
                           "continuation p50=4ms p95=4ms (n=60)")
    assert g1["measurable"] is True and g1["n"] == 120
    assert g1["turn_first"] == {"n": 60, "p50_s": 0.031, "p95_s": 0.057}
    assert g1["continuation"] == {"n": 60, "p50_s": 0.004, "p95_s": 0.004}
    assert g1["side_call_excluded"] == 10
    assert _kpis()["G1_hook"]["value"].startswith("not measurable: ")  # never instrumented


def test_g1_proxy_is_never_zero_when_forwarded_rows_added_nothing():
    """The regression itself: added_latency_s all 0.0 must not read as 0 ms."""
    _write_proxy_rows([_row(tier_decision_s=0.022, added_latency_s=0.0) for _ in range(100)])
    g1 = _kpis()["G1_proxy"]
    assert "p95=22ms" in g1["value"] and "p95=0ms" not in g1["value"]


def test_g1_proxy_thin_segment_says_too_few_and_missing_field_is_not_measurable():
    _write_proxy_rows([_row(tier_decision_s=0.01) for _ in range(60)]
                      + [_row(step_class="continuation", tier_decision_s=0.01) for _ in range(5)])
    g1 = _kpis()["G1_proxy"]
    assert g1["value"] == ("turn-first p50=10ms p95=10ms (n=60) | "
                           "continuation too few to tell (n=5)")
    assert g1["continuation"] == {"n": 5, "p50_s": None, "p95_s": None}
    _write_proxy_rows([_row(added_latency_s=0.05) for _ in range(100)])  # no tier_decision_s
    g1 = _kpis()["G1_proxy"]
    assert g1["value"] == "not measurable: no proxy decisions with tier_decision_s in window"
    _write_proxy_rows([_row(tier_decision_s=0.01) for _ in range(20)])
    g1 = _kpis()["G1_proxy"]
    assert g1["value"] == "too few to tell (n=20)" and g1["measurable"] is False


# ── O1 / G2 / G4 ────────────────────────────────────────────────────────────

def test_o1_is_labelled_est_and_not_a_zero_when_empty():
    o1 = _kpis()["O1"]["value"]
    assert o1.startswith("not measurable: ")


def test_o1_value_is_always_labelled_est(monkeypatch):
    """O1 is a saving ESTIMATE (project rule: savings displays show 'est.' only).
    Whatever dashboard_data.summary() returns, the printed value must carry it."""
    from llm_router import dashboard_data as dd

    class FakeSummary:
        def display(self):
            return "est. saved $42.00"

        estimated_n = 500
        estimated_usd = 42.0

    monkeypatch.setattr(dd, "summary", lambda period: FakeSummary())
    o1 = _kpis()["O1"]
    assert "est." in o1["value"], o1
    assert o1["measurable"] is True


def test_o1_summary_failure_is_not_measurable_and_writes_nothing(monkeypatch, tmp_path):
    """Read-only: an O1 read failure must not touch ~/.llm-router (not even via
    the failopen counter -- this command never persists anything)."""
    from llm_router import dashboard_data as dd
    from llm_router import failopen

    def boom(period):
        raise RuntimeError("usage.db is locked")

    monkeypatch.setattr(dd, "summary", boom)
    home_files_before = sorted(failopen.store_path().parent.glob("*")) if failopen.store_path().parent.is_dir() else []
    o1 = _kpis()["O1"]
    assert o1["value"] == "not measurable: dashboard_data.summary() raised RuntimeError; see llm-router doctor"
    home_files_after = sorted(failopen.store_path().parent.glob("*")) if failopen.store_path().parent.is_dir() else []
    assert home_files_before == home_files_after


def test_g2_with_only_untimestamped_events_is_an_all_time_line_not_a_rate():
    """Rows from before per-event timestamps cannot be placed in a window."""
    from llm_router import failopen

    path = failopen.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join('{"c":"CHZ-FO-LEGACY","e":"X"}\n' for _ in range(3)), encoding="utf-8")
    failopen.reset_cache()
    g2 = _kpis()["G2"]
    assert g2["value"].startswith("not measurable: ")
    assert g2["lines"] == ["all-time: 3 fail-open event(s) recorded, 3 of them from before "
                           "per-event timestamps (cannot be placed in any window)"]


def test_g4_names_a_currently_benched_provider():
    from llm_router import provider_reset

    assert provider_reset.record_provider_reset("anthropic", time.time() + 3600, "test")
    g4 = _kpis()["G4"]
    assert any(ln.startswith("benched right now: anthropic until ") for ln in g4["lines"]), g4
    # One bench was recorded and nothing contradicted it: a count, never a rate.
    assert g4["benches"] == 1 and g4["wrong"] == 0 and g4["active"] == 1
    assert g4["value"].startswith("too few to tell (n=1 benches); 0 shown wrong so far")


# ── O2 / D5 from a configured frozen benchmark ──────────────────────────────

def _bench(tmp_path, monkeypatch, content):
    path = tmp_path / "bench.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    monkeypatch.setenv("LLM_ROUTER_KPI_BENCHMARK_PATH", str(path))


def test_o2_d5_read_the_configured_benchmark(monkeypatch, tmp_path):
    _bench(tmp_path, monkeypatch, {
        "generated_at": "2026-10-01T00:00:00Z",
        "o2": {"acceptable_rate": 0.9, "n": 120},
        "d5": {"accuracy": 0.7, "under_route_rate": 0.08, "n": 150}})
    k = _kpis()
    assert k["O2"]["value"] == "90.0% acceptable vs Claude (n=120, frozen set)"
    assert k["D5"]["value"] == "70.0% exact-tier accuracy, under-route=8.0% (within <=10% gate) (n=150)"
    _bench(tmp_path, monkeypatch, {"d5": {"accuracy": 0.7, "under_route_rate": 0.25, "n": 150}})
    assert "OVER the <=10% gate" in _kpis()["D5"]["value"]


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    {},
    {"o2": {"acceptable_rate": 0.9}, "d5": {"accuracy": 0.7}},            # no n
    {"o2": {"acceptable_rate": 0.9, "n": 0}, "d5": {"accuracy": 0.7, "n": 0}},
])
def test_unusable_benchmark_is_not_measurable(monkeypatch, tmp_path, content):
    _bench(tmp_path, monkeypatch, content)
    k = _kpis()
    for key in ("O2", "D5"):
        assert k[key]["value"].startswith("not measurable: "), (key, k[key])


def test_small_benchmark_n_is_too_few(monkeypatch, tmp_path):
    _bench(tmp_path, monkeypatch, {"o2": {"acceptable_rate": 1.0, "n": 10}})
    assert _kpis()["O2"]["value"] == "too few to tell (n=10)"


def test_missing_benchmark_file_is_not_measurable(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_KPI_BENCHMARK_PATH", str(tmp_path / "absent.json"))
    assert _kpis()["O2"]["value"].startswith("not measurable: ")


def test_missing_configured_file_carries_its_code_never_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_KPI_BENCHMARK_PATH", str(tmp_path / "absent.json"))
    k = _kpis()
    for key in ("O2", "D5"):
        assert k[key]["measurable"] is False and k[key]["n"] is None
        assert kpi.BENCH_MISSING in k[key]["value"] and "0%" not in k[key]["value"]
    h = kpi.compute_health(kpi.compute_scorecard(days=7))["kpis"]
    assert h["O2"]["state"] == "blind" and h["D5"]["state"] == "blind"


@pytest.mark.parametrize("content", ["{not json", "[]", "null", ""])
def test_malformed_file_fails_open_with_its_code(monkeypatch, tmp_path, content):
    _bench(tmp_path, monkeypatch, content)
    k = _kpis()                                    # does not raise
    for key in ("O2", "D5"):
        assert kpi.BENCH_MALFORMED in k[key]["value"], (key, k[key])
        assert k[key]["measurable"] is False


def test_unconfigured_names_neither_code(monkeypatch):
    v = _kpis()["O2"]["value"]
    assert "LLM_ROUTER_KPI_BENCHMARK_PATH" in v and "CHZ-KPI-BENCH" not in v


def test_benchmark_older_than_30_days_is_stale_and_fresh_is_measured(monkeypatch, tmp_path):
    def stamp(age_days):
        t = time.time() - age_days * 86400
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

    body = {"o2": {"acceptable_rate": 0.8, "n": 60}, "d5": {"accuracy": 0.7, "n": 60}}
    _bench(tmp_path, monkeypatch, {**body, "generated_at": stamp(29)})
    h = kpi.compute_health(kpi.compute_scorecard(days=7))["kpis"]
    assert h["O2"]["state"] == h["D5"]["state"] == "measured"
    _bench(tmp_path, monkeypatch, {**body, "generated_at": stamp(31)})
    h = kpi.compute_health(kpi.compute_scorecard(days=7))["kpis"]
    assert h["O2"]["state"] == h["D5"]["state"] == "stale"


def test_below_min_n_stays_blind_but_shows_the_unscored_rate_with_its_n(monkeypatch, tmp_path):
    _bench(tmp_path, monkeypatch, {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                   "o2": {"acceptable_rate": 0.5, "n": 23},
                                   "d5": {"accuracy": 0.25, "under_route_rate": 0.0, "n": 24}})
    k = _kpis()
    assert k["O2"]["measurable"] is False and k["O2"]["n"] == 23
    assert k["O2"]["lines"] == ["unscored, below n=50: 50.0% acceptable (n=23)"]
    assert k["D5"]["lines"] == ["unscored, below n=50: 25.0% exact-tier accuracy, under-route=0.0% (n=24)"]


def test_d5_detail_line_qualifies_a_vacuous_under_route(monkeypatch, tmp_path):
    base = {"accuracy": 0.2, "under_route_rate": 0.0, "n": 24}
    never = {**base, "predicted_tier_counts": {"haiku": 0, "sonnet": 21, "opus": 3}}
    _bench(tmp_path, monkeypatch, {"d5": never})
    lines = _kpis()["D5"]["lines"]
    assert any("haiku 0, sonnet 21, opus 3" in ln and "under-route rate is not informative" in ln
               for ln in lines), lines
    ok = {**base, "n": 60, "predicted_tier_counts": {"haiku": 5, "sonnet": 40, "opus": 15}}
    _bench(tmp_path, monkeypatch, {"d5": ok})
    k = _kpis()["D5"]
    assert k["measurable"] and k["lines"] == ["classifier predicted: haiku 5, sonnet 40, opus 15"]


def test_committed_benchmark_file_is_consistent_and_text_free():
    path = Path(__file__).resolve().parent.parent / "docs" / "repo_goals" / "kpi_benchmark.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    items = d["items"]
    assert len(items) == d["provenance"]["n_tasks"] >= 32           # something was checked (grows as truth is extended)
    assert len({i["id"] for i in items}) == len(items)               # unique ids
    o2_pool = [i for i in items if i["pass"]["opus"]]
    assert d["o2"]["n"] == len(o2_pool)
    assert d["o2"]["n"] > 0 and d["d5"]["n"] >= 50
    assert d["o2"]["acceptable_rate"] == sum(i["pass"]["haiku"] for i in o2_pool) / len(o2_pool)
    rank = {"haiku": 0, "sonnet": 1, "opus": 2}
    graded = [i for i in items if i["cheapest_tier"] in rank]
    assert d["d5"]["n"] == len(graded) and d["d5"]["n_no_truth"] == len(items) - len(graded)
    assert d["d5"]["accuracy"] == sum(i["predicted_effective"] == i["cheapest_tier"] for i in graded) / len(graded)
    assert d["d5"]["under_route_rate"] == sum(rank[i["predicted_effective"]] < rank[i["cheapest_tier"]]
                                              for i in graded) / len(graded)
    assert all(set(i) == {"id", "pass", "cheapest_tier", "predicted_raw", "predicted_effective"}
               for i in items)                                       # ids + labels only
    counts = {t: sum(i["predicted_effective"] == t for i in graded) for t in rank}
    assert d["d5"]["predicted_tier_counts"] == counts
    assert "@" not in path.read_text() and "sk-" not in path.read_text()


# ── CLI surface ─────────────────────────────────────────────────────────────

def test_write_weekly_writes_a_dated_markdown_scorecard(tmp_path, capsys):
    out = tmp_path / "weekly"
    assert kpi.cmd_kpi(["--days", "3", "--write-weekly", str(out)]) == 0
    files = list(out.glob("kpi-????-??-??.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert body.startswith("# llm-router KPI scorecard")
    assert "window 3d, organic only" in body
    assert body.count("\n| ") >= 14          # header row + 13 KPI rows
    assert "not measurable" in body
    assert "wrote" in capsys.readouterr().out


def test_json_output_matches_compute_scorecard_shape(capsys):
    assert kpi.cmd_kpi(["--json", "--days", "2"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["window_days"] == 2 and data["include_research"] is False
    assert set(data["kpis"]) == set(ALL_KEYS)


def test_include_research_flag_is_parsed(capsys):
    assert kpi.cmd_kpi(["--json", "--include", "research"]) == 0
    assert json.loads(capsys.readouterr().out)["include_research"] is True


def test_main_dispatches_kpi_to_cmd_kpi():
    """cli.main() installs host config as a side effect, so its dispatch is read
    from the AST: an `args[0] == "kpi"` branch whose body calls cmd_kpi."""
    import ast

    import llm_router.cli as cli

    src = Path(cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    branches = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.If) and "'kpi'" in ast.unparse(n.test) and "args[0]" in ast.unparse(n.test)
    ]
    assert len(branches) == 1
    assert "cmd_kpi(args[1:])" in ast.unparse(branches[0])
    assert "llm-router kpi" in src  # help text


def test_the_benchmark_env_var_is_registered():
    from llm_router.env_registry import ENV_REGISTRY

    assert "LLM_ROUTER_KPI_BENCHMARK_PATH" in ENV_REGISTRY


def test_d5_missing_haiku_key_is_unknown_not_never_predicted(monkeypatch, tmp_path):
    base = {"accuracy": 0.2, "under_route_rate": 0.0, "n": 24}
    for counts in ({"sonnet": 21, "opus": 3}, {}):
        _bench(tmp_path, monkeypatch, {"d5": {**base, "predicted_tier_counts": counts}})
        lines = _kpis()["D5"]["lines"]
        assert not any("never predicted haiku" in ln for ln in lines), lines


def test_d5_measured_headline_drops_gate_wording_when_haiku_never_predicted(monkeypatch, tmp_path):
    d5 = {"accuracy": 0.7, "under_route_rate": 0.0, "n": 150,
          "predicted_tier_counts": {"haiku": 0, "sonnet": 100, "opus": 50}}
    _bench(tmp_path, monkeypatch, {"generated_at": "2026-10-01T00:00:00Z", "d5": d5})
    data = kpi.compute_scorecard(days=7)
    v = data["kpis"]["D5"]["value"]
    assert data["kpis"]["D5"]["measurable"] and "gate" not in v and "never predicted haiku" in v, v
    health = kpi.compute_health(data, now=data["kpis"]["D5"]["newest_ts"] + 3600)["kpis"]["D5"]
    assert "never predicted haiku" in health["reason"], health
    # the qualifier is absent when haiku is predicted: the gate wording stays
    _bench(tmp_path, monkeypatch, {"generated_at": "2026-10-01T00:00:00Z",
                                   "d5": {**d5, "predicted_tier_counts": {"haiku": 5, "sonnet": 95, "opus": 50}}})
    assert "(within <=10% gate)" in _kpis()["D5"]["value"]
