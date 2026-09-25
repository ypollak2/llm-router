"""I4: every local draft may open files — read-only, inside the hook's deadline.

Before this, a Q&A draft went through execute_chain: text in, text out. The
model answered questions about a repo it could not look at, and only prompts the
tool classifier flagged reached the agent loop (which can also write). The
draft path now runs the same loop with the READ tools only, and falls back to
the text chain when the loop produces nothing in time.

Writes stay where they were: the read-only loop is not offered write_file,
edit_file or run_command, and refuses them if a model emits one anyway.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from llm_router.hooks import agent_loop

READ_TOOLS = {"read_file", "list_files", "search_files"}


class _R:
    def __init__(self, payload): self._p = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self._p).encode()


def _scripted(messages, sent):
    seq = list(messages)

    def _urlopen(req, *a, **k):
        sent.append(json.loads(req.data))
        return _R({"message": seq.pop(0) if seq else messages[-1]})
    return _urlopen


def _names(payload):
    return {t["function"]["name"] for t in payload["tools"]}


def test_the_read_only_loop_is_offered_only_read_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_CONSTRAINED_TOOLS", "on")
    sent: list = []
    done = {"content": json.dumps({"tool": "finish", "arguments": {"answer": "ok"}})}
    monkeypatch.setattr(agent_loop.urllib.request, "urlopen", _scripted([done], sent))

    agent_loop.run_agent_loop("q", "m", tmp_path)
    assert "write_file" in _names(sent[0]), "premise: the default loop can write"

    sent.clear()
    agent_loop.run_agent_loop("q", "m", tmp_path, read_only=True)
    assert _names(sent[0]) == READ_TOOLS
    enum = set(sent[0]["format"]["properties"]["tool"]["enum"])
    assert enum == READ_TOOLS | {agent_loop.FINISH_TOOL}, \
        "the grammar must not let the model name a write tool either"


def test_the_read_only_loop_refuses_a_write_it_was_not_offered(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "apply")   # writes WOULD land
    sent: list = []
    write = {"content": "", "tool_calls": [{"function": {
        "name": "write_file", "arguments": {"path": "pwned.txt", "content": "x"}}}]}
    done = {"content": "done"}
    monkeypatch.setattr(agent_loop.urllib.request, "urlopen",
                        _scripted([write, done], sent))

    agent_loop.run_agent_loop("q", "m", tmp_path, read_only=True)
    assert not (tmp_path / "pwned.txt").exists()
    tool_msgs = [m["content"] for m in sent[-1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "read-only" in tool_msgs[0], tool_msgs


def test_a_read_only_draft_may_answer_without_opening_a_file(monkeypatch, tmp_path):
    """A draft for "what does os.path.join do?" has nothing to read. The write
    loop treats a tool-less answer as a no-op; the draft loop must not."""
    answer = {"content": "It joins path components."}
    monkeypatch.setattr(agent_loop.urllib.request, "urlopen", _scripted([answer], []))
    assert agent_loop.run_agent_loop("q", "m", tmp_path) is None, "premise"
    assert agent_loop.run_agent_loop("q", "m", tmp_path, read_only=True) \
        == "It joins path components."


def test_the_loop_seeds_retrieval_from_the_session(monkeypatch, tmp_path):
    from llm_router import context_injection
    seen = {}

    def fake_inject(prompt, **kw):
        seen.update(kw)
        return prompt
    monkeypatch.setattr(context_injection, "inject", fake_inject)
    monkeypatch.setattr(context_injection, "enabled", lambda: True)
    monkeypatch.setattr(agent_loop.urllib.request, "urlopen",
                        _scripted([{"content": "a"}], []))
    agent_loop.run_agent_loop("q", "m", tmp_path, read_only=True, session_id="s-1")
    assert seen.get("session_id") == "s-1"


# ── the hook: the Q&A branch drafts through the read-only loop ───────────────

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
SAFE_PROMPT = "What does os.path.join do?"   # not context-dependent: drafts directly


def _load():
    cached = sys.modules.get("auto_route_i4")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_i4", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_i4"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ar(monkeypatch, tmp_path):
    module = _load()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(module, "_router_dir", lambda: tmp_path / ".llm-router", raising=False)
    monkeypatch.setattr(module, "log_routing_decision", lambda **kw: None, raising=False)
    import llm_router.hooks.savings_logger as savings_logger
    monkeypatch.setattr(savings_logger, "log_direct_savings", lambda **kw: None)
    monkeypatch.setattr(savings_logger, "log_direct_to_db", lambda **kw: None)
    for k, v in {"LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1", "LLM_ROUTER_DIRECT_EXECUTION": "1",
                 "LLM_ROUTER_ENFORCE": "suggest", "LLM_ROUTER_ZERO_CLAUDE": "off",
                 "LLM_ROUTER_LOCAL_AGENT_LOOP": "on"}.items():
        monkeypatch.setenv(k, v)
    return module


def _wire(monkeypatch, *, agent_result):
    import llm_router.hooks.chain_builder as chain_builder
    import llm_router.hooks.direct_executor as de
    model = de.ModelSpec(provider="ollama", model="fake-model")
    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 10.0))
    monkeypatch.setattr(chain_builder, "build_chain", lambda c, z, t: [model])
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda p, t: False)
    calls = {"agent": [], "chain": []}

    def fake_agent(prompt, chain, **kw):
        calls["agent"].append(kw)
        return de.DirectResult(text=agent_result, model=model, latency_ms=1) if agent_result else None

    def fake_chain(prompt, chain, task_type, **kw):
        calls["chain"].append(kw)
        return de.DirectResult(text="from the chain", model=model, latency_ms=1)
    monkeypatch.setattr(de, "execute_agent", fake_agent)
    monkeypatch.setattr(de, "execute_chain", fake_chain)
    return calls


def _run(ar, monkeypatch, cwd):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": SAFE_PROMPT, "session_id": "sess-i4a1b2", "cwd": str(cwd)})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit):
        ar.main()


def test_a_qa_draft_runs_the_read_only_loop(ar, monkeypatch, tmp_path):
    calls = _wire(monkeypatch, agent_result="os.path.join joins path components into one path.")
    _run(ar, monkeypatch, tmp_path)
    assert len(calls["agent"]) == 1, calls
    kw = calls["agent"][0]
    assert kw.get("read_only") is True
    assert kw.get("session_id") == "sess-i4a1b2"
    assert kw.get("project_root") == str(tmp_path)
    assert calls["chain"] == [], "the loop answered; the chain must not also run"


def test_the_text_chain_catches_a_loop_that_produced_nothing(ar, monkeypatch, tmp_path):
    calls = _wire(monkeypatch, agent_result=None)
    _run(ar, monkeypatch, tmp_path)
    assert len(calls["agent"]) == 1 and len(calls["chain"]) == 1, calls


def test_loop_off_means_text_chain_only(ar, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_AGENT_LOOP", "off")
    calls = _wire(monkeypatch, agent_result="x" * 40)
    _run(ar, monkeypatch, tmp_path)
    assert calls["agent"] == [] and len(calls["chain"]) == 1, calls


# ── I4c: a QUESTION flagged tool-shaped still drafts read-only ───────────────
# Live 2026-09-24: "What is the default value of _REVERT_DEFAULT in
# hooks/draft_usage.py?" was classified query/simple but needs_tools=True (it
# names a file), so it went to the write-capable loop — which must call a tool,
# got no session or cwd, wandered 4 calls at ~10s each and hit the 55s deadline.

def _wire_tools(monkeypatch, *, needs_tools):
    calls = _wire(monkeypatch, agent_result="The default value is 50, set in draft_usage.py.")
    import llm_router.hooks.chain_builder as chain_builder
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda p, t: needs_tools)
    return calls


def test_a_tool_shaped_question_drafts_read_only(ar, monkeypatch, tmp_path):
    calls = _wire_tools(monkeypatch, needs_tools=True)
    _run(ar, monkeypatch, tmp_path)   # SAFE_PROMPT classifies as query
    assert len(calls["agent"]) == 1, calls
    kw = calls["agent"][0]
    assert kw.get("read_only") is True
    assert kw.get("session_id") == "sess-i4a1b2"
    assert kw.get("project_root") == str(tmp_path)


def test_a_tool_shaped_code_task_keeps_the_write_loop(ar, monkeypatch, tmp_path):
    # U (2026-09-25): code tasks are not drafted by default; the write loop is
    # still what runs when drafting is widened to everything.
    monkeypatch.setenv("LLM_ROUTER_DRAFT_TASKS", "all")
    calls = _wire_tools(monkeypatch, needs_tools=True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "prompt": "Refactor the parse_config function in config.py to use a dataclass",
        "session_id": "sess-i4a1b2", "cwd": str(tmp_path)})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit):
        ar.main()
    assert calls["agent"], "premise: the code task reached the agent loop"
    kw = calls["agent"][0]
    assert not kw.get("read_only")
    assert kw.get("session_id") == "sess-i4a1b2"
    assert kw.get("project_root") == str(tmp_path)
