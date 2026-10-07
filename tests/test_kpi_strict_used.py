"""M0.2 (owner decision D-2): NS and D2 count only STRICT-used units.

Strict-used = served by a non-Claude model AND verify.verify_status in {pass_f2p,
pass_f2p_model} AND task_type not Q&A AND outcome != redo. The heuristic outcome "used" is
neither required nor enough. A unit with no ``verify`` never counts. The old heuristic
numerators stay visible as ``kpis_diag.NS_heuristic`` / ``D2_heuristic``, outside
``KPI_CODES`` ("not a target").

Eleven cases from the plan; only cases 2, 3, 7 and 10 count.
"""

from __future__ import annotations

import time

import pytest

from llm_router import northstar as ns
from llm_router import session_kind
from llm_router.commands import kpi

LOCAL = "ollama/qwen3-coder:30b"


def _verify(status):
    return {"verify_status": status}


# (id, unit fields, counts as strict-used?)
CASES = [
    (1, dict(model=LOCAL, task_type="code", outcome=ns.OUTCOME_USED, signal="tool_result_reused"), False),
    (2, dict(model=LOCAL, task_type="code", outcome=ns.OUTCOME_USED, verify=_verify("pass_f2p")), True),
    (3, dict(model=LOCAL, task_type="code", outcome=ns.OUTCOME_USED, verify=_verify("pass_f2p_model")), True),
    (4, dict(model=LOCAL, task_type="code", outcome=ns.OUTCOME_USED, verify=_verify("pass_p2p")), False),
    (5, dict(model=LOCAL, task_type="query", outcome=ns.OUTCOME_USED, verify=_verify("pass_f2p")), False),
    (6, dict(model=LOCAL, task_type="code", outcome=ns.OUTCOME_USED, signal="user_keep"), False),
    (7, dict(model=LOCAL, task_type="code", outcome=ns.OUTCOME_USED, signal="user_keep",
             verify=_verify("pass_f2p")), True),
    (8, dict(model=LOCAL, task_type="code", outcome=ns.OUTCOME_USED, verify=_verify("fail")), False),
    (9, dict(model="claude-sonnet-4-5", task_type="code", outcome=ns.OUTCOME_USED,
             verify=_verify("pass_f2p")), False),
    (10, dict(kind=ns.UNIT_AGENT_ROUTE_CODEX, model="gpt-5-codex", task_type="code",
              outcome=ns.OUTCOME_UNKNOWN, signal="agent_route_codex_delegated",
              verify=_verify("pass_f2p")), True),
    (11, dict(kind=ns.UNIT_ROUTED_EDIT, model=LOCAL, task_type="code", outcome=ns.OUTCOME_REDO,
              verify=_verify("pass_f2p")), False),
]


def _unit(**kw):
    base = dict(kind=ns.UNIT_ROUTED_MCP, session_id="s-org", ts=time.time(), model=LOCAL, task_type="code",
                outcome=ns.OUTCOME_UNKNOWN)
    base.update(kw)
    return ns.Unit(**base)


@pytest.mark.parametrize("case_id,fields,expected", CASES, ids=[f"case{c[0]}" for c in CASES])
def test_is_strict_used_case(case_id, fields, expected):
    assert ns.is_strict_used(_unit(**fields)) is expected


@pytest.mark.parametrize("case_id,fields,expected", CASES, ids=[f"case{c[0]}" for c in CASES])
def test_is_strict_used_accepts_the_dict_form_kpi_reads(case_id, fields, expected):
    assert ns.is_strict_used(_unit(**fields).to_dict()) is expected


def test_exactly_four_of_eleven_count():
    assert [c[0] for c in CASES if c[2]] == [2, 3, 7, 10]
    assert sum(ns.is_strict_used(_unit(**c[1])) for c in CASES) == 4


def test_a_unit_with_no_verify_never_counts():
    assert ns.is_strict_used(_unit(outcome=ns.OUTCOME_USED)) is False
    assert ns.is_strict_used(_unit(outcome=ns.OUTCOME_USED, verify={})) is False
    assert ns.is_strict_used(_unit(outcome=ns.OUTCOME_USED, verify={"verify_status": None})) is False


def test_claude_main_call_and_missing_model_are_not_non_claude():
    assert ns.is_non_claude(_unit(kind=ns.UNIT_CLAUDE_MAIN, model="gemini-2.5-pro")) is False
    assert ns.is_non_claude(_unit(model=None)) is False
    assert ns.is_non_claude(_unit(model="anthropic/claude-haiku")) is False
    assert ns.is_non_claude(_unit(model="Claude-Opus-4")) is False
    assert ns.is_non_claude(_unit(model=LOCAL)) is True


def test_qa_task_types_are_the_plan_list():
    assert ns.QA_TASK_TYPES == {"query", "research", "generate", "analyze", "coordinate", "introspect",
                                "summary", "classification", "extraction"}
    assert ns.STRICT_VERIFY == {"pass_f2p", "pass_f2p_model"}


def test_unit_to_dict_is_unchanged_without_verify():
    assert "verify" not in _unit().to_dict()
    assert _unit(verify=_verify("pass_f2p")).to_dict()["verify"] == {"verify_status": "pass_f2p"}


# ── kpi: NS / D2 strict, NS_heuristic / D2_heuristic beside them ─────────────

@pytest.fixture
def organic(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    session_kind._FOUND.clear()
    session_kind.tag_session("s-org", "/Users/someone/Projects/app", env={})
    yield "s-org"
    session_kind._FOUND.clear()


def _stream(monkeypatch, units):
    rows = []
    for u in units:  # the stream before build_sessions stamps a kind: kpi resolves it by session id
        row = u.to_dict()
        row.pop("session_kind"), row.pop("session_kind_source")
        rows.append(row)
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False: iter(rows))


def test_kpi_ns_d2_are_strict_and_heuristic_is_shown_apart(monkeypatch, organic):
    # 60 heuristic-used local units with no verify, 10 strict-used, 30 Claude-only turns.
    units = [_unit(outcome=ns.OUTCOME_USED, signal="tool_result_reused") for _ in range(60)]
    units += [_unit(outcome=ns.OUTCOME_UNKNOWN, verify=_verify("pass_f2p")) for _ in range(10)]
    units += [_unit(kind=ns.UNIT_CLAUDE_MAIN, model="claude-opus-4", outcome=ns.OUTCOME_NOT_ROUTED)
              for _ in range(30)]
    _stream(monkeypatch, units)
    card = kpi.compute_scorecard(days=7)
    k, diag = card["kpis"], card["kpis_diag"]
    assert k["NS"]["value"] == "10.0% (n=100)"           # 10 strict / 100 units
    assert k["D2"]["value"] == "14.3% (n=70)"            # 10 strict / 70 attempted
    assert k["D1"]["value"] == "70.0% (n=100)"           # D1 does not use "used"
    assert diag["NS_heuristic"]["value"] == "60.0% (n=100)"
    assert diag["D2_heuristic"]["value"] == "85.7% (n=70)"
    assert "not a target" in diag["NS_heuristic"]["reason"]
    assert "not a target" in diag["D2_heuristic"]["reason"]
    assert "strict" in k["NS"]["reason"] and "pass_f2p" in k["NS"]["reason"]
    assert "strict" in k["D2"]["reason"]
    assert "NS_heuristic" not in k and "NS_heuristic" not in kpi.KPI_CODES


def test_kpi_without_any_verify_has_ns_zero_and_heuristic_nonzero(monkeypatch, organic):
    units = [_unit(outcome=ns.OUTCOME_USED) for _ in range(80)]
    _stream(monkeypatch, units)
    card = kpi.compute_scorecard(days=7)
    assert card["kpis"]["NS"]["value"] == "0.0% (n=80)"
    assert card["kpis"]["D2"]["value"] == "0.0% (n=80)"
    assert card["kpis_diag"]["NS_heuristic"]["value"] == "100.0% (n=80)"


def test_kpi_diag_not_measurable_when_no_units(monkeypatch, organic):
    _stream(monkeypatch, [])
    diag = kpi.compute_scorecard(days=7)["kpis_diag"]
    assert diag["NS_heuristic"]["measurable"] is False
    assert diag["D2_heuristic"]["measurable"] is False


def test_scorecard_text_prints_the_strict_rule_and_the_diag_lines(monkeypatch, organic):
    _stream(monkeypatch, [_unit(outcome=ns.OUTCOME_USED) for _ in range(80)])
    text = kpi.render_scorecard(kpi.compute_scorecard(days=7))
    assert "NS_heuristic" in text and "not a target" in text
    assert "strict-used" in text
