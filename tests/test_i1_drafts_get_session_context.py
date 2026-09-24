"""I1: a local draft sees the session's recorded conversation and tool facts.

2026-09-24 (audit/CRITICAL_2026-09-24_local_models_do_no_work.md): the draft path
called `context_injection.inject(prompt)` with no `session_id` and no `root`, so
the session store — what was said AND what was actually done — was never
attached, and the semantic layer ran unscoped. Measured on this repo: +930
tokens of context without the session, +1,954 with it. The helper's own
docstring said it existed to avoid "two places to forget a scope argument".
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from llm_router.hooks import direct_executor as de
from llm_router.hooks.direct_executor import ModelSpec, execute_chain

LONG = "a sufficiently long and plain answer to the question asked here. " * 4
FACT = "ZEBRA-7741 the savings headline was changed in dashboard_data.query_window"


@pytest.fixture
def seeded_session(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_OKF", "on")
    monkeypatch.delenv("LLM_ROUTER_SESSION_CONTEXT", raising=False)
    from llm_router import session_store
    sid = "i1sess-4c2e9a"
    session_store.record_event(sid, "tool_call", FACT, role="assistant", tool="Edit")
    assert FACT in (session_store.build_session_context(sid, max_tokens=1200, query="savings") or ""), \
        "premise: the store holds the fact"
    return sid


def test_the_outbound_prompt_carries_the_session_fact(seeded_session, monkeypatch, tmp_path):
    seen = []

    def fake(prompt, model, timeout, history, system_prompt):
        seen.append(prompt + "\n" + (system_prompt or ""))
        return LONG, {}

    monkeypatch.setattr(de, "_okf_enrich", lambda *a, **k: None)
    with patch.dict(de._PROVIDER_CALLS, {"ollama": fake}), \
         patch.object(de, "available_ollama_models", return_value=None), \
         patch.object(de, "ollama_is_alive", return_value=True):
        execute_chain("what did we change about the savings headline?",
                      [ModelSpec("ollama", "qwen3.8:latest")], "query",
                      session_id=seeded_session, root=str(tmp_path))
    assert seen, "premise: the local model was called"
    assert "ZEBRA-7741" in seen[0], "the session store's fact never reached the local model"


def test_the_hook_passes_the_session_to_the_chain():
    """Structural: auto-route's execute_chain call names session_id and root."""
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks"
           / "auto-route.py").read_text()
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_execute_chain"]
    assert calls, "premise: the hook calls _execute_chain"
    for c in calls:
        kws = {k.arg for k in c.keywords}
        assert {"session_id", "root"} <= kws, f"line {c.lineno} passes {sorted(kws)}"
