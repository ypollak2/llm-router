"""D-372: the local-model seam in agent-route measures and records; it starts no model and binds nothing.

The decision: when Codex cannot take a sub-agent task, try a local model first for read-only, small, suitable tasks, behind a flag,
with a strict gate and a quality guard; if the evidence says local is not good enough for any sub-agent class, build only the seam and
the logging. The evidence (``llm_router.agent_route_local``): of 843 real sub-agent prompts Codex could not take, 0 pass even the
strict gate, and the one class with parity evidence (a short question about one existing file) is not what these tasks are.
These tests pin the gate, the rows, and the absence of a runner.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

from llm_router import agent_route_local as local
from llm_router import paths
from tests._bind_support import (
    AGENT_ROUTE,
    agent_payload,
    decision_of,
    hook_env,
    init_repo,
    ledger_rows,
    run_hook,
    stub_calls,
    write_codex_stub,
)

REPO = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO / "src" / "llm_router" / "hooks" / "agent-route.py"
HOUR = 3600
SMALL_Q = "What does the parse function in cli.py return when the argument list is empty?"


@pytest.fixture
def project(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "cli.py").write_text("def parse(argv):\n    return {}\n")
    return tmp_path / "proj"


# ------------------------------------------------------------------------------------------------ the gate


def test_the_gate_admits_the_one_class_with_parity_evidence(project):
    """A short, read-only question about exactly one existing file. Without this the zero below could be a broken gate."""
    verdict = local.evaluate(SMALL_Q, "general-purpose", "query", "simple", str(project))
    assert verdict.eligible and verdict.blockers == () and verdict.files == 1 and verdict.reason_code == "eligible"


@pytest.mark.parametrize("change,blocker", [
    (dict(subagent_type="task-reviewer"), "typed_agent"),
    (dict(complexity="complex"), "not_simple"),
    (dict(complexity="moderate"), "not_simple"),
    (dict(task_type="code"), "task_type"),
    (dict(prompt=SMALL_Q + " " + "x" * 1200), "long_prompt"),
    (dict(prompt="Fix the parse function in cli.py so it returns an empty dict."), "writes_or_runs"),
    (dict(prompt="Run the tests for cli.py and tell me what fails."), "writes_or_runs"),
    (dict(prompt="What does the parse function in cli.py do, as discussed above?"), "needs_session"),
    (dict(prompt="Here is cli.py. Thanks."), "not_a_question"),
    (dict(prompt="What does the parse function return when the argument list is empty?"), "no_file"),
    (dict(prompt="What do cli.py and other.py have in common?"), "many_files"),
    (dict(prompt="What does the parse function in gone.py return?"), "file_missing"),
    (dict(prompt="What does ../outside.py return?"), "file_missing"),
])
def test_each_rule_of_the_gate_refuses(project, change, blocker):
    args = dict(prompt=SMALL_Q, subagent_type="general-purpose", task_type="query", complexity="simple")
    args.update(change)
    (project.parent / "outside.py").write_text("x = 1\n")
    (project / "other.py").write_text("y = 2\n")
    verdict = local.evaluate(args["prompt"], args["subagent_type"], args["task_type"], args["complexity"], str(project))
    assert not verdict.eligible and blocker in verdict.blockers, verdict


def test_every_failed_rule_is_reported_not_only_the_first(project):
    verdict = local.evaluate("Implement a fix for the bug across cli.py and other.py", "task-reviewer", "code", "complex",
                             str(project))
    assert set(verdict.blockers) >= {"typed_agent", "not_simple", "task_type", "writes_or_runs", "not_a_question", "many_files",
                                     "file_missing"}
    assert verdict.reason_code == verdict.blockers[0] == "typed_agent"


def test_without_a_directory_existence_is_not_checked_which_makes_the_count_an_upper_bound():
    assert local.evaluate(SMALL_Q, "general-purpose", "query", "simple", None).eligible


def test_a_real_review_brief_fails_many_rules():
    brief = ("Review PR #820 against the builder rules in coord/builder-rules.md. Run the tests, check the changelog fragment, "
             "and report every finding with file and line.\n" * 12)
    verdict = local.evaluate(brief, "task-reviewer", "research", "complex", None)
    assert not verdict.eligible and len(verdict.blockers) >= 5


# --------------------------------------------------------------------------- the hook: rows, and nothing else


def _load_hook():
    spec = importlib.util.spec_from_file_location("agent_route_local_seam", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


@pytest.fixture
def hook(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ROUTE_BANNER", "off")
    return _load_hook()


def _rows(lever="agent_route_local"):
    """The local verdict rows (``agent_route_local.jsonl``), or with a lever name the North Star ledger's rows for it."""
    f = paths.state_path(local.LOG_FILE if lever == "agent_route_local" else "north_star_units.jsonl")
    rows = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
    return rows if lever == "agent_route_local" else [r for r in rows if r.get("lever") == lever]


def _no_model(monkeypatch):
    """A local model must never be started by the seam."""
    def boom(*_a, **_k):
        raise AssertionError("the seam started a model")

    monkeypatch.setattr("llm_router.hooks.direct_executor.execute_agent", boom)
    monkeypatch.setattr("llm_router.hooks.direct_executor.execute_chain", boom)
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", boom)


@pytest.mark.parametrize("outcome", sorted(local.CAPACITY_OUTCOMES))
def test_a_capacity_decline_is_recorded_with_its_verdict(hook, monkeypatch, project, outcome):
    _no_model(monkeypatch)
    hook._record_north_star_unit("agent_route_codex", model="", outcome=outcome)
    assert hook._note_local_candidate("Review PR #820. Run the tests.", "analyze", "complex", "task-reviewer", "s", str(project)) is None
    row, = _rows()
    assert row["outcome"] == "local_ineligible" and row["declined_by"] == outcome
    assert row["reason_code"] == "typed_agent" and "not_simple" in row["blockers"] and "writes_or_runs" in row["blockers"]
    assert row["files_named"] == 0 and row["prompt_chars"] == len("Review PR #820. Run the tests.")
    assert row["subagent_type"] == "task-reviewer" and row["session_id"] == "s"


def test_a_task_that_would_pass_the_gate_is_recorded_as_a_candidate(hook, monkeypatch, project):
    _no_model(monkeypatch)
    hook._record_north_star_unit("agent_route_codex", model="", outcome="benched")
    hook._note_local_candidate(SMALL_Q, "query", "simple", "general-purpose", "s", str(project))
    row, = _rows()
    assert row["outcome"] == "local_candidate" and row["reason_code"] == "eligible" and row["blockers"] == []


@pytest.mark.parametrize("outcome", ["unsuitable", "codex_blocked", "codex_failed", "codex_session_breaker", "codex_reissue",
                                     "codex_unavailable", "delegated", ""])
def test_a_task_codex_declined_for_another_reason_is_not_a_local_question(hook, monkeypatch, project, outcome):
    _no_model(monkeypatch)
    if outcome:
        hook._record_north_star_unit("agent_route_codex", model="", outcome=outcome)
    hook._note_local_candidate(SMALL_Q, "query", "simple", "general-purpose", "s", str(project))
    assert _rows() == []


def test_the_seam_never_raises(hook, monkeypatch):
    hook._record_north_star_unit("agent_route_codex", model="", outcome="benched")
    monkeypatch.setattr(local, "evaluate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    hook._note_local_candidate("x", "query", "simple", "general-purpose", "s", None)
    from llm_router import failopen

    failopen.reset_cache()
    assert failopen.snapshot().by_code.get("CHZ-FO-AGENT-ROUTE-LOCAL-SEAM") == 1


def test_the_local_rows_stay_out_of_the_north_star_ledger(hook, project):
    """A measurement that runs nothing is not a unit: ``north_star_units.jsonl`` keeps only the Codex decision."""
    hook._record_north_star_unit("agent_route_codex", model="", outcome="benched")
    hook._note_local_candidate(SMALL_Q, "query", "simple", "general-purpose", "s", str(project))
    ledger = [json.loads(x) for x in paths.state_path("north_star_units.jsonl").read_text().splitlines()]
    assert [(r["lever"], r["outcome"]) for r in ledger] == [("agent_route_codex", "benched")]
    assert len(_rows()) == 1


def test_a_lost_log_write_is_counted_and_never_raised(hook, monkeypatch, project):
    from llm_router import capped_log, failopen

    hook._record_north_star_unit("agent_route_codex", model="", outcome="benched")
    monkeypatch.setattr(capped_log, "append", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    hook._note_local_candidate(SMALL_Q, "query", "simple", "general-purpose", "s", str(project))
    failopen.reset_cache()
    assert failopen.snapshot().by_code.get("CHZ-FO-AGENT-ROUTE-LOCAL-LOG") == 1


# ------------------------------------------------------------------- the real hook script, end to end


@pytest.fixture
def ctx(tmp_path):
    project = init_repo(tmp_path / "project")
    argv_log = tmp_path / "codex_argv.jsonl"

    class Ctx:
        home = tmp_path
        proj = project
        log = argv_log

        def bench(self, until_in_s, set_ago_s=0.0):
            now = time.time()
            state = tmp_path / ".llm-router"
            state.mkdir(exist_ok=True)
            (state / "provider_reset.json").write_text(json.dumps(
                {"providers": {"codex": {"until": now + until_in_s, "set_at": now - set_ago_s, "reason": "usage limit"}}}))

        def run(self, prompt, **env):
            stub = write_codex_stub(tmp_path / "bin", argv_log=argv_log, answer="STUB-CODEX-ANSWER-w11")
            env.setdefault("CODEX_PATH", str(stub))
            payload = agent_payload(prompt, cwd=str(project))
            return run_hook(AGENT_ROUTE, payload, hook_env(tmp_path, **env), cwd=tmp_path)

    return Ctx()


PROMPT = "analyze why the pager drops the last row and explain the root cause"


def test_a_benched_codex_leaves_the_decision_alone_and_records_the_local_verdict(ctx):
    ctx.bench(3 * HOUR)
    proc = ctx.run(PROMPT)
    decision = decision_of(proc)
    assert stub_calls(ctx.log) == []  # Codex was not started
    assert decision is None or "STUB-CODEX-ANSWER" not in json.dumps(decision)  # and nothing was bound
    rows = ledger_rows(ctx.home)
    assert [(r["lever"], r["outcome"]) for r in rows] == [("agent_route_codex", "benched")]  # the North Star ledger is unchanged
    log = ctx.home / ".llm-router" / local.LOG_FILE
    local_rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(local_rows) == 1 and local_rows[0]["outcome"] == "local_ineligible"
    assert local_rows[0]["declined_by"] == "benched" and "no_file" in local_rows[0]["blockers"]
    assert PROMPT not in json.dumps(local_rows)  # the verdict row carries rule names and counts, never the prompt


def test_a_delegated_task_writes_no_local_row(ctx):
    proc = ctx.run(PROMPT)
    decision = decision_of(proc)
    assert decision and "STUB-CODEX-ANSWER-w11" in json.dumps(decision)
    assert [r["lever"] for r in ledger_rows(ctx.home)] == ["agent_route_codex"]
    assert not (ctx.home / ".llm-router" / local.LOG_FILE).exists()


def test_end_to_end_a_long_bench_is_probed_by_the_real_task_and_lifted(ctx):
    ctx.bench(48 * HOUR)
    proc = ctx.run(PROMPT)
    decision = decision_of(proc)
    assert decision and "STUB-CODEX-ANSWER-w11" in json.dumps(decision)  # bound exactly like any Codex answer
    assert len(stub_calls(ctx.log)) == 1
    delegated = [r for r in ledger_rows(ctx.home) if r["outcome"] == "delegated"]
    assert len(delegated) == 1 and delegated[0]["canary"] is True
    state = json.loads((ctx.home / ".llm-router" / "provider_reset.json").read_text())
    assert "codex" not in state["providers"]
    meter_log = [json.loads(x) for x in (ctx.home / ".llm-router" / "codex_meter.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in meter_log] == ["canary_claimed", "bench_cleared"]
