"""J: the capability probe must be able to write inside its own temp dir.

Measured 2026-09-24: the registry marked all four local models incapable,
including qwen3-coder:30b, which drives the agent loop correctly in live use.
The probe passes only if the model CREATES fib.py, and agent writes default to
`propose` (diff computed, nothing written) — so no model could ever pass:
    LLM_ROUTER_AGENT_WRITES=apply  -> probe_model('qwen3-coder:30b') True
    default                        -> False
The probe's writes land in a fresh mkdtemp it deletes afterwards, so applying
them there does not relax the guard anywhere else.
"""
from __future__ import annotations

import os

from llm_router import agentic_registry as reg
from llm_router.hooks.agent_loop import execute_tool

FIB = "def fib(n):\n    return n if n < 2 else fib(n-1) + fib(n-2)\nprint(fib(10))\n"


def _model_that_writes_fib(prompt, model, project_root, timeout_per_call=60, **kw):
    execute_tool("write_file", {"path": "fib.py", "content": FIB}, project_root)
    return "done"


def test_a_capable_model_passes_under_the_default_write_mode(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_AGENT_WRITES", raising=False)
    monkeypatch.setattr(reg, "run_agent_loop", _model_that_writes_fib)
    assert reg.probe_model("m", timeout=30) is True


def test_the_probe_leaves_the_write_mode_as_it_found_it(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "propose")
    monkeypatch.setattr(reg, "run_agent_loop", _model_that_writes_fib)
    reg.probe_model("m", timeout=30)
    assert os.environ["LLM_ROUTER_AGENT_WRITES"] == "propose"
    monkeypatch.delenv("LLM_ROUTER_AGENT_WRITES")
    reg.probe_model("m", timeout=30)
    assert "LLM_ROUTER_AGENT_WRITES" not in os.environ
