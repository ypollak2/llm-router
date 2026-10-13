"""P1.1 door 5: hooks/agent-route.py DIRECT sub-agent runs build context via build_pack.

The door name is "agent": it is the sub-agent member of context_pack.DOORS
("agent_route" is not a member and build_pack raises on it).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import llm_router.context_pack as cp
import llm_router.hooks.chain_builder as cb
import llm_router.hooks.direct_executor as de

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "agent-route.py"


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "lr"))


def _hook():
    spec = importlib.util.spec_from_file_location("agent_route_hook_p11", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def rig(monkeypatch):
    seen = {"packs": [], "exec": []}
    spec = de.ModelSpec("ollama", "fake")

    def _exec(prompt, chain, *a, **k):
        seen["exec"].append((prompt, k))
        return de.DirectResult(text="routed", model=spec, latency_ms=1,
                               input_tokens=1, output_tokens=1)

    monkeypatch.setattr(cb, "build_chain", lambda *a, **k: [spec])
    monkeypatch.setattr(cb, "get_current_pressure", lambda: ("green", 0.0))
    monkeypatch.setattr(cb, "needs_claude_tools", lambda *a, **k: False)
    monkeypatch.setattr(de, "execute_chain", _exec)
    monkeypatch.setattr(de, "execute_agent", _exec)
    real = cp.build_pack

    def _spy(*a, **k):
        seen["packs"].append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(cp, "build_pack", _spy)
    return seen


def test_direct_run_builds_pack_once_with_agent_door(monkeypatch, rig):
    monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "on")
    assert _hook()._try_direct_subagent("implement foo", "code", "simple", "s1") == "routed"
    assert len(rig["packs"]) == 1
    assert rig["packs"][0][1]["door"] == "agent"
    assert len(rig["exec"]) == 1


def test_direct_off_builds_no_pack(monkeypatch, rig):
    monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "off")
    assert _hook()._try_direct_subagent("implement foo", "code", "simple", "s1") is None
    monkeypatch.delenv("LLM_ROUTER_SUBAGENT_DIRECT")
    assert _hook()._try_direct_subagent("implement foo", "code", "simple", "s1") is None
    assert rig["packs"] == [] and rig["exec"] == []


def test_request_untruncated(monkeypatch, rig):
    monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "on")
    big = "implement " + ("x" * 200_000) + " END"
    assert _hook()._try_direct_subagent(big, "code", "simple", "s1") == "routed"
    assert rig["exec"][0][0] == big


def test_transcript_conversation_reaches_the_model(monkeypatch, rig, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "on")
    t = tmp_path / "t.jsonl"
    t.write_text(
        '{"type":"user","message":{"role":"user","content":"earlier question"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":"earlier answer"}}\n')
    _hook()._try_direct_subagent("implement foo", "code", "simple", "s1",
                                 transcript_path=str(t))
    prompt, kw = rig["exec"][0]
    assert prompt == "implement foo"
    blob = repr(kw.get("history")) + repr(kw.get("context"))
    assert "earlier question" in blob and "earlier answer" in blob


def test_pack_error_fails_open(monkeypatch, rig):
    monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "on")

    def _boom(*a, **k):
        raise RuntimeError("pack down")

    monkeypatch.setattr(cp, "build_pack", _boom)
    assert _hook()._try_direct_subagent("implement foo", "code", "simple", "s1") == "routed"
    prompt, kw = rig["exec"][0]
    assert prompt == "implement foo"
    assert not kw.get("history") and not kw.get("context")
