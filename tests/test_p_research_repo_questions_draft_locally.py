"""P: a repo question classified "research" still gets a local draft.

build_chain returns [] for task_type "research" (web research belongs to
Perplexity), so a research-tagged prompt never reached a local model. Replaying
186 real prompts from 2026-09-14..24 through the hook: 18 (9.7%) ended there,
and none of them was a web question — "is llm-router really works locally
here?", "check if agenticgraphs accepts an injected runner", audit briefs. The
only one that needed the web asked for current OpenRouter prices.

Now research goes to the web-only chain only when the prompt carries a web
signal; otherwise the DRAFT chain is built as a question. The routing hint
Claude sees is unchanged.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
REPO_Q = "Can you take the last 10 days prompts and check if they are being routed?"
WEB_Q = "What is the latest price of Claude Opus on OpenRouter today?"


def _load():
    cached = sys.modules.get("auto_route_p")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_p", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_p"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("text,web", [
    (WEB_Q, True), ("search the web for the newest release of uv", True),
    ("what changed in python 3.14 released in 2025?", True), ("see https://example.com/x", True),
    (REPO_Q, False), ("is llm-router really works locally here?", False),
    ("How could there be only 4 routings today?", False),
    ("open audit/CHECKPOINT_2026-09-24.md", False),
    ("check if agenticgraphs accepts an injected runner", False),
])
def test_web_signal(text, web):
    assert _load()._needs_web(text) is web


def _run(monkeypatch, tmp_path, prompt):
    ar = _load()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ar, "log_routing_decision", lambda **kw: None, raising=False)
    for k, v in {"LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1", "LLM_ROUTER_DIRECT_EXECUTION": "1",
                 "LLM_ROUTER_ENFORCE": "suggest", "LLM_ROUTER_ZERO_CLAUDE": "off"}.items():
        monkeypatch.setenv(k, v)
    import llm_router.hooks.chain_builder as cb
    import llm_router.hooks.direct_executor as de
    built = []
    model = de.ModelSpec(provider="ollama", model="fake-model")
    monkeypatch.setattr(cb, "get_current_pressure", lambda: ("green", 10.0))
    monkeypatch.setattr(cb, "build_chain",
                        lambda c, z, t: built.append(t) or ([] if t == "research" else [model]))
    monkeypatch.setattr(cb, "needs_claude_tools", lambda p, t: False)
    drafted = []
    monkeypatch.setattr(de, "execute_chain", lambda *a, **k: drafted.append(1))
    monkeypatch.setattr(de, "execute_agent", lambda *a, **k: drafted.append(1))
    log: list[str] = []
    monkeypatch.setattr(ar, "_debug_log", log.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": prompt, "session_id": "sess-p7q8r9"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        ar.main()
    except SystemExit:
        pass
    return built, drafted, log


def test_a_repo_question_tagged_research_is_drafted(monkeypatch, tmp_path):
    built, drafted, log = _run(monkeypatch, tmp_path, REPO_Q)
    assert any("task=research" in line for line in log), "premise: classified research"
    assert built == ["query"] and drafted


def test_a_web_question_still_goes_to_web_research(monkeypatch, tmp_path):
    built, drafted, log = _run(monkeypatch, tmp_path, WEB_Q)
    assert any("task=research" in line for line in log), "premise: classified research"
    assert built == ["research"] and not drafted
