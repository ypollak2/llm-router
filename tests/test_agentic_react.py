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
from llm_router.proxy.loop_guard import REASON_LOOP_GUARD


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
    # Plan 3.6: a VARYING tool call each step, so this tests ``max_steps`` itself
    # (the loop never finishes, period) independently of the loop guard, which
    # has its own dedicated tests below for a model that keeps repeating.
    def always_tool(messages, tools):
        return ChatTurn(tool_calls=[ToolCall("bash", {"command": f"echo {len(messages)}"})])

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


# ── loop guard (plan 3.6) — reuses llm_router.proxy.loop_guard ─────────────


def test_react_loop_guard_stops_on_a_repeated_tool_call():
    # Same tool, same args, every turn — the proxy's own runaway pattern
    # (loop_guard.py: 44 consecutive repeats of one Read) reproduced locally.
    calls = []

    def repeating_client(messages, tools):
        calls.append(1)
        return ChatTurn(tool_calls=[ToolCall("bash", {"command": "echo hi"})])

    agent = ReActAgent(client=repeating_client, executor=lambda n, a: "ok", max_steps=10)
    res = agent.run(Milestone("M1", "", lambda _a: AcceptanceResult(True)), [], 5.0)
    # Caught well before the step cap — not "ran out of max_steps".
    assert len(calls) < 10
    assert res.artifacts["steps"] < 10
    assert REASON_LOOP_GUARD in res.artifacts["error"]
    assert res.artifacts["output"] == ""  # same "gave up" shape as a client error
    assert res.confidence < 0.5


def test_react_loop_guard_trips_on_consecutive_cap_even_without_a_repeat():
    # A DIFFERENT tool call every turn — never repeats, but the model is still
    # not converging. The consecutive-served cap (not the repeat window) must
    # catch this; it is the other half of the proxy's guard.
    def ever_different_client(messages, tools):
        return ChatTurn(tool_calls=[ToolCall("bash", {"command": f"echo {len(messages)}"})])

    agent = ReActAgent(client=ever_different_client, executor=lambda n, a: "ok", max_steps=20)
    res = agent.run(Milestone("M1", "", lambda _a: AcceptanceResult(True)), [], 5.0)
    assert res.artifacts["steps"] < 20
    assert REASON_LOOP_GUARD in res.artifacts["error"]
    assert "consecutive_served_exceeded" in res.artifacts["error"]


def test_react_loop_guard_does_not_trip_on_varied_tool_calls():
    turns = iter([
        ChatTurn(tool_calls=[ToolCall("bash", {"command": "echo 1"})]),
        ChatTurn(tool_calls=[ToolCall("read_file", {"path": "a.py"})]),
        ChatTurn(tool_calls=[ToolCall("bash", {"command": "echo 3"})]),
        ChatTurn(content="DONE"),
    ])

    def client(messages, tools):
        return next(turns)

    agent = ReActAgent(client=client, executor=lambda n, a: "ok", max_steps=8)
    res = agent.run(Milestone("M1", "", lambda _a: AcceptanceResult(True)), [], 5.0)
    assert res.artifacts["error"] == ""
    assert res.artifacts["output"] == "DONE"
    assert res.artifacts["steps"] == 4
