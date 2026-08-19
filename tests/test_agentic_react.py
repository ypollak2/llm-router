"""A3 — local ReAct/Ollama harness, driven by a FAKE client + executor (no model, no shell)."""
from __future__ import annotations

import sys

from llm_router.agentic.acceptance import canary_check
from llm_router.agentic.engine import MGEEEngine, Outcome
from llm_router.agentic.ledger import AcceptanceResult, Milestone, TaskLedger
from llm_router.agentic.react import (
    ChatTurn,
    ReActAgent,
    ToolCall,
    default_tool_executor,
)


def _ledger(ms):
    return TaskLedger(goal="t", milestones=ms, budget_cap_usd=10.0)


def test_react_runs_tool_loop_then_finishes():
    exec_calls = []

    def fake_exec(name, args):
        exec_calls.append((name, args))
        return "ran ok"

    turns = iter([
        ChatTurn(content="", tool_calls=[ToolCall("bash", {"command": "pytest"})]),
        ChatTurn(content="DONE: PROVIDER_OLLAMA_CANARY"),
    ])

    def fake_client(messages, tools):
        return next(turns)

    agent = ReActAgent(client=fake_client, executor=fake_exec)
    res = agent.run(Milestone("M1", "do it", lambda _a: AcceptanceResult(True)), [], 5.0)
    assert exec_calls == [("bash", {"command": "pytest"})]
    assert "PROVIDER_OLLAMA_CANARY" in res.artifacts["output"]
    assert res.artifacts["actions"][0]["tool"] == "bash"
    assert res.artifacts["hit_step_cap"] is False


def test_react_is_bounded_never_loops_forever():
    def always_tool(messages, tools):
        return ChatTurn(tool_calls=[ToolCall("bash", {"command": "echo hi"})])

    agent = ReActAgent(client=always_tool, executor=lambda n, a: "ok", max_steps=4)
    res = agent.run(Milestone("M1", "", lambda _a: AcceptanceResult(True)), [], 5.0)
    assert res.artifacts["steps"] == 4 and res.artifacts["hit_step_cap"] is True
    assert res.artifacts["output"] == ""  # no fabricated finish
    assert res.confidence < 0.5


def test_react_drives_engine_with_objective_check():
    def client(messages, tools):
        return ChatTurn(content="OLLAMA_OK")

    ms = [Milestone("M1", "impl", canary_check("OLLAMA_OK"))]
    res = MGEEEngine({0: ReActAgent(client=client, executor=lambda n, a: "")}).run(_ledger(ms))
    assert res.outcome is Outcome.COMPLETE and ms[0].achieved_by == 0


def test_react_carry_forward_in_prompt():
    seen = []

    def client(messages, tools):
        seen.append(messages[-1]["content"])
        return ChatTurn(content="done")

    frozen = [{"id": "M1", "description": "scaffold", "artifacts": {}}]
    ReActAgent(client=client, executor=lambda n, a: "").run(
        Milestone("M2", "impl", lambda _a: AcceptanceResult(True)), frozen, 5.0
    )
    assert "M1" in seen[0] and "scaffold" in seen[0]


def test_default_executor_runs_and_files_no_model(tmp_path):
    ex = default_tool_executor(cwd=str(tmp_path))
    out = ex("bash", {"command": f"{sys.executable} -c 'print(42)'"})
    assert "42" in out and "[exit 0]" in out
    ex("write_file", {"path": str(tmp_path / "x.txt"), "content": "hello"})
    assert (tmp_path / "x.txt").read_text() == "hello"
    assert "hello" in ex("read_file", {"path": str(tmp_path / "x.txt")})
    assert "tool error" in ex("read_file", {"path": str(tmp_path / "missing")})


def test_default_executor_relative_path_lands_in_cwd(tmp_path):
    # A bare "marker.txt" must write INTO cwd, not the process dir (the tier-0 bug
    # live testing surfaced: 6 write_file calls, file never landed).
    ex = default_tool_executor(cwd=str(tmp_path))
    ex("write_file", {"path": "marker.txt", "content": "PHASE_B_OK"})
    assert (tmp_path / "marker.txt").read_text() == "PHASE_B_OK"


def test_default_executor_rejects_path_traversal(tmp_path):
    ex = default_tool_executor(cwd=str(tmp_path))
    out = ex("write_file", {"path": "../../escape.txt", "content": "x"})
    assert "tool error" in out and "escapes working directory" in out
    assert not (tmp_path.parent.parent / "escape.txt").exists()
