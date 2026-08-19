"""CF-4 / G1: bounded_operational proven on the REAL agent harness, not a fake.

Scenario 4 in test_bounded_operational.py used a fake writing agent. This file drives
the FULL production path — `llm_delegate(bounded=True)` → real `ReActAgent` → real
`default_tool_executor` (real filesystem, real sandbox) → real `cmd_check` against the
actual file on disk → real ledger row. Only the model's TOKENS are scripted (the
`OllamaClient` seam), which is unavoidable offline; every other layer is real.

Plus:
  * the two §18 CF-4 safety items proven on the REAL executor (destructive command
    blocked; write outside the repo rejected), and
  * an opt-in live-Ollama test (skipped unless LLM_ROUTER_LIVE_OLLAMA=1 and Ollama is
    reachable) that drives the loop with a genuine local model.
"""
from __future__ import annotations

import json
import os
import urllib.request

import pytest

from llm_router.agentic.react import ChatTurn, ReActAgent, ToolCall, default_tool_executor
from llm_router.routing_quality import load_records

_VERIFY_CMD = [
    "python3", "-c",
    "import sys; sys.exit(0 if open('README.md').read().endswith(chr(10)*2) else 1)",
]


class _ScriptedClient:
    """A fake OllamaClient: step 1 emits a real write_file tool call, step 2 finishes.
    The tool call is executed by the REAL executor, so the file is really written."""
    def __init__(self, path="README.md", content="# Title\n\n"):
        self.calls = 0
        self.path = path
        self.content = content

    def __call__(self, messages, tools) -> ChatTurn:
        self.calls += 1
        if self.calls == 1:
            return ChatTurn(content="", tool_calls=[
                ToolCall("write_file", {"path": self.path, "content": self.content})])
        return ChatTurn(content="done", tool_calls=[])


@pytest.mark.asyncio
async def test_real_harness_bounded_edit_end_to_end(tmp_path, monkeypatch, temp_db):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Title\n")
    ledger = tmp_path / "rq.jsonl"
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(ledger))
    monkeypatch.setenv("LLM_ROUTER_BOUNDED_OPERATIONAL", "1")

    import llm_router.tools.agentic as tool

    def _planner_factory():
        def pm(_goal):
            # objective cmd_check that inspects the REAL file — not a self-reported diff
            return [{"id": "M1", "description": "add a blank line to README.md",
                     "acceptance": {"type": "cmd", "command": _VERIFY_CMD, "cwd": str(repo)}}]
        return pm

    # REAL ReActAgent with the REAL executor bound to the repo; only tokens are scripted.
    real_agent = ReActAgent(tier=0, cwd=str(repo), client=_ScriptedClient())
    monkeypatch.setattr(tool, "planner_factory", _planner_factory)
    monkeypatch.setattr(tool, "adapters_factory", lambda: {0: real_agent})

    out = json.loads(await tool.llm_delegate("Add a blank line to README.md", bounded=True))

    # the REAL file on disk was mutated by the real executor
    assert (repo / "README.md").read_text() == "# Title\n\n"
    assert out["outcome"] == "complete" and out["ok"] is True
    assert out["route_kind"] == "bounded_operational"
    # exactly one verified bounded_operational ledger row
    rows = [r for r in load_records(str(ledger)) if not r.get("_invalid")]
    assert len(rows) == 1
    r = rows[0]
    assert r["route_kind"] == "bounded_operational"
    assert r["verification_attempted"] is True and r["verification_passed"] is True


def test_real_executor_blocks_destructive_command(tmp_path):
    """§18 CF-4: a destructive bash command is blocked by the REAL executor's
    _bash_block_reason denylist — proven on the production factory, not a mock."""
    execute = default_tool_executor(cwd=str(tmp_path))
    result = execute("bash", {"command": "rm -rf /"})
    assert "blocked" in result.lower()
    assert "destructive" in result.lower()


def test_real_executor_rejects_write_outside_repo(tmp_path):
    """§18 CF-4: a write that escapes the repo root is rejected by the REAL executor."""
    repo = tmp_path / "repo"
    repo.mkdir()
    execute = default_tool_executor(cwd=str(repo))
    result = execute("write_file", {"path": "../escape.txt", "content": "x"})
    assert "escapes" in result.lower() or "error" in result.lower()
    assert not (tmp_path / "escape.txt").exists()  # nothing written outside the repo


# ── opt-in live-model coverage (skipped in CI hard-gate) ─────────────────────

def _ollama_reachable() -> bool:
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 — unreachable / dead port → skip
        return False


@pytest.mark.skipif(
    os.environ.get("LLM_ROUTER_LIVE_OLLAMA", "").strip().lower() not in ("1", "true", "yes", "on")
    or not _ollama_reachable(),
    reason="live Ollama not enabled (set LLM_ROUTER_LIVE_OLLAMA=1 with a reachable Ollama)",
)
def test_live_ollama_bounded_edit(tmp_path):
    """Genuine live-model proof: a real local Ollama model drives the real ReAct loop
    to edit a file, verified objectively. Runs only when explicitly enabled — the CI
    hard gate points OLLAMA_BASE_URL at a dead port, so this skips there."""
    from llm_router.agentic.acceptance import cmd_check
    from llm_router.agentic.ledger import Milestone
    from llm_router.agentic.service import run_delegation

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "NOTE.md").write_text("hello\n")
    agent = ReActAgent(tier=0, cwd=str(repo))  # real client → real Ollama
    accept = cmd_check(
        ["python3", "-c", "import sys; sys.exit(0 if 'DONE' in open('NOTE.md').read() else 1)"],
        cwd=str(repo))
    ms = [Milestone("M1", "append the word DONE on a new line in NOTE.md", accept)]
    out = run_delegation("append DONE to NOTE.md", ms, {0: agent},
                         baseline_cost_per_milestone=0.2, budget_cap_usd=0.05)
    # a genuine local model must have actually written the file to pass the objective check
    assert out["outcome"] == "complete"
    assert "DONE" in (repo / "NOTE.md").read_text()
