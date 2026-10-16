"""P2.2-A: `kpi` carries the last coverage_check result (the reader of coverage_check.json)
or says honestly that none was run. Outside "kpis": _ORDER and --health are unchanged."""

from __future__ import annotations

import json

import pytest

from llm_router import paths
from llm_router.commands import kpi


@pytest.fixture(autouse=True)
def _scratch(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    (tmp_path / "claude-projects").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude-projects"))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    return tmp_path / "state"


RESULT = {"schema": "coverage_check/1", "generated_at": "2026-11-03T01:00:00Z", "verdict": "PASS", "n": 240,
          "passed": 240, "coverage": 1.0, "wilson95": [0.9843, 1.0],
          "window": {"since": "2026-10-20T00:00:00Z", "until": "2026-11-03T00:00:00Z"}}


def test_no_run_is_not_measurable_and_never_a_zero(_scratch):
    assert kpi._coverage_summary() == {"measurable": False, "reason": "no coverage_check run"}
    card = kpi.compute_scorecard(7)
    assert card["coverage"] == {"measurable": False, "reason": "no coverage_check run"}
    assert "coverage" not in card["kpis"] and "coverage" not in kpi._ORDER
    assert "Decision-point coverage" not in kpi.render_scorecard(card), "no run: the printed card is unchanged"


def test_the_written_result_is_carried_and_rendered_on_one_line(_scratch):
    (_scratch / "coverage_check.json").write_text(json.dumps(RESULT))
    card = kpi.compute_scorecard(7)
    assert card["coverage"] == RESULT
    json.dumps(card)
    (line,) = [ln for ln in kpi.render_scorecard(card).splitlines() if "Decision-point coverage" in ln]
    assert "100.0% (240/240, Wilson95 98.4%-100.0%) PASS" in line
    assert "2026-10-20T00:00:00Z .. 2026-11-03T00:00:00Z" in line


@pytest.mark.parametrize("text", ["{not json", "[]", json.dumps({"verdict": "PASS"}), json.dumps({"n": 3})])
def test_a_malformed_file_is_not_read_as_a_result(_scratch, text):
    (_scratch / "coverage_check.json").write_text(text)
    out = kpi._coverage_summary()
    assert out["measurable"] is False and "reason" in out


def test_a_not_informative_result_is_shown_as_such(_scratch):
    res = dict(RESULT, verdict="NOT INFORMATIVE", n=0, passed=0, coverage=None, wilson95=None)
    (_scratch / "coverage_check.json").write_text(json.dumps(res))
    (line,) = [ln for ln in kpi.render_scorecard(kpi.compute_scorecard(7)).splitlines()
               if "Decision-point coverage" in ln]
    assert "n/a (0/0) NOT INFORMATIVE" in line


def test_the_script_writes_exactly_the_file_kpi_reads(tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import coverage_check as cc

    proj = tmp_path / "proj"
    proj.mkdir()
    rc = cc.run(["--since", "2026-10-20T00:00:00Z", "--until", "2026-10-21T00:00:00Z",
                 "--projects-dir", str(proj), "--write"])
    assert rc == 1  # nothing to measure: NOT INFORMATIVE
    assert paths.state_path(kpi.COVERAGE_FILE).exists()
    assert kpi._coverage_summary()["verdict"] == "NOT INFORMATIVE"
