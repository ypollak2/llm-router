"""P0.13 (R-LOC-7, NFR-SAFE): ``llm_act`` writes are confined to the project root.

The bug at da31df7: ``tools/agentic._default_adapters`` built ``ReActAgent(tier=0)``
with no cwd, so ``default_tool_executor`` fell back to ``Path.cwd()`` of the MCP
server process (in the field, ``$HOME``) and ``write_file`` wrote there. Codex
(tier 1) got no ``-C`` either and ran ``workspace-write`` in the same directory.

Every test here drives the real ``llm_act`` door with the real default adapters,
the real tool executor and the real filesystem. Only the model tokens are faked
(a scripted Ollama client) and Codex is replaced by a recorder — no live model.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from llm_router.agentic.engine import AgentRunResult
from llm_router.agentic.react import ChatTurn, ToolCall, default_tool_executor

DONE = "P013_CONFINE_DONE"


class _FakeCodex:
    """Stands in for CodexAdapter: records how it was built, never runs codex."""

    built: list[dict[str, Any]] = []

    def __init__(self, tier: int, **kw: Any) -> None:
        self.tier = tier
        _FakeCodex.built.append(dict(kw))

    def run(self, milestone, frozen_context, budget_left):
        return AgentRunResult({"output": "", "tier": self.tier}, 0.0)


def _scripted(calls: list[ToolCall], results: list[str]):
    """All tool calls in one turn (max_steps is 8), then the canary answer."""
    pending = list(calls)

    def client(messages, tools):
        tail = []
        for m in reversed(messages):
            if m.get("role") != "tool":
                break
            tail.append(str(m.get("content", "")))
        results.extend(reversed(tail))
        if pending:
            batch = pending[:]
            pending.clear()
            return ChatTurn(tool_calls=batch)
        return ChatTurn(content=DONE)

    return client


@pytest.fixture
def world(tmp_path, monkeypatch, temp_db):
    """MCP process cwd != project root; no root source unless a test adds one."""
    for var in ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID", "LLM_ROUTER_PROJECT_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(tmp_path / "rq.jsonl"))
    base = tmp_path.resolve()
    mcp_cwd, project, outside = base / "mcp_cwd", base / "project", base / "outside"
    for d in (mcp_cwd, project, outside):
        d.mkdir()
    monkeypatch.chdir(mcp_cwd)

    import llm_router.agentic.react as react
    import llm_router.tools.agentic as tool

    def _planner_factory():
        def pm(_goal):
            return [{"id": "M1", "description": "write the file",
                     "acceptance": {"type": "canary", "marker": DONE}}]
        return pm

    monkeypatch.setattr(tool, "planner_factory", _planner_factory)
    monkeypatch.setattr(tool, "adapters_factory", None)  # the REAL default adapters
    monkeypatch.setattr(tool, "CodexAdapter", _FakeCodex)
    monkeypatch.setattr(react, "_default_local_model", lambda: "fake-local")
    _FakeCodex.built = []
    results: list[str] = []
    state = SimpleNamespace(base=base, mcp_cwd=mcp_cwd, project=project,
                            outside=outside, results=results, calls=[])

    def _client_factory(model, base_url=None):
        return _scripted(state.calls, results)

    monkeypatch.setattr(react, "_default_ollama_client", _client_factory)
    return state


async def _act(state, calls: list[ToolCall], **kw) -> dict[str, Any]:
    from llm_router.tools.consolidated import llm_act

    state.calls[:] = calls
    return json.loads(await llm_act("write a marker file", **kw))


def _write(path: str, content: str = "x") -> ToolCall:
    return ToolCall("write_file", {"path": path, "content": content})


def _files_outside(state) -> list[Path]:
    """Every file under the test tree that is not inside the project root."""
    return [p for p in state.base.rglob("*")
            if p.is_file() and not p.is_relative_to(state.project)
            and p.name.startswith("x")]


async def test_write_lands_in_project_root_not_mcp_cwd(world, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.project))
    out = await _act(world, [_write("marker.txt", "hello")])
    assert (world.project / "marker.txt").read_text() == "hello"
    assert not (world.mcp_cwd / "marker.txt").exists()
    assert out["read_only"] is False
    assert out["project_root"] == str(world.project)


async def test_ten_outside_paths_refused_10_of_10(world, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.project))
    (world.project / "link").symlink_to(world.outside, target_is_directory=True)
    (world.base / "project-evil").mkdir()
    targets = [
        "../x01.txt",
        "../../x02.txt",
        "sub/../../x03.txt",
        str(world.outside / "x04.txt"),
        str(world.mcp_cwd / "x05.txt"),          # absolute, into the MCP process cwd
        "../mcp_cwd/x06.txt",
        str(world.base / "x07.txt"),
        "link/x08.txt",                           # symlink inside root pointing outside
        str(world.base / "project-evil" / "x09.txt"),  # shared-prefix sibling
        "./../outside/x10.txt",
    ]
    await _act(world, [_write(t) for t in targets])
    refused = [r for r in world.results if r.startswith("tool error")]
    assert len(world.results) == 10
    assert len(refused) == 10, world.results
    assert _files_outside(world) == []
    assert list(world.project.rglob("x*.txt")) == []


async def test_no_root_is_read_only(world):
    (world.mcp_cwd / "readme.txt").write_text("visible")
    out = await _act(world, [
        _write("x_marker.txt"),
        ToolCall("bash", {"command": "echo hi > x_bash.txt"}),
        ToolCall("read_file", {"path": "readme.txt"}),
    ])
    assert not (world.mcp_cwd / "x_marker.txt").exists()
    assert not (world.mcp_cwd / "x_bash.txt").exists()
    assert out["read_only"] is True and out["project_root"] is None
    assert world.results[0].startswith("tool error") and "read-only" in world.results[0]
    assert world.results[1].startswith("tool error") and "read-only" in world.results[1]
    assert world.results[2] == "visible"  # reads still work


async def test_mcp_client_roots_beat_claude_project_dir(world, monkeypatch, tmp_path):
    from llm_router import mcp_roots

    mcp_roots.clear_cache()
    other = world.base / "other"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))

    class _Session:
        async def list_roots(self):
            return SimpleNamespace(roots=[SimpleNamespace(uri=world.project.as_uri())])

    ctx = SimpleNamespace(session=_Session())
    out = await _act(world, [_write("marker.txt")], ctx=ctx)
    mcp_roots.clear_cache()
    assert (world.project / "marker.txt").exists()
    assert not (other / "marker.txt").exists()
    assert not (world.mcp_cwd / "marker.txt").exists()
    assert out["project_root"] == str(world.project)


async def test_codex_tier_is_confined_too(world, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.project))
    await _act(world, [])
    assert _FakeCodex.built[-1].get("cwd") == str(world.project)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    await _act(world, [])
    assert _FakeCodex.built[-1].get("sandbox_mode") == "read-only"


def test_executor_without_cwd_is_read_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "r.txt").write_text("ok")
    ex = default_tool_executor()
    assert ex("write_file", {"path": "w.txt", "content": "x"}).startswith("tool error")
    assert ex("bash", {"command": "touch b.txt"}).startswith("tool error")
    assert not (tmp_path / "w.txt").exists() and not (tmp_path / "b.txt").exists()
    assert ex("read_file", {"path": "r.txt"}) == "ok"


@pytest.mark.xfail(strict=True, reason=(
    "Known gap, owned by P2.9 (OS sandbox): a bash command can still redirect "
    "output outside the project root. P0.13 confines the file tools only."))
def test_bash_redirect_outside_root_is_refused(tmp_path):
    root, out = tmp_path / "root", tmp_path / "out"
    root.mkdir()
    out.mkdir()
    default_tool_executor(cwd=str(root))("bash", {"command": "echo x > ../out/y.txt"})
    assert not (out / "y.txt").exists()
