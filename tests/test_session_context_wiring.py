"""Session Context Accumulator — hook draft-path wiring (auto-route.py).

Unit-tests (in-process, not subprocess) the three new integration points added
to auto-route.py's Phase 1 direct-execution block:

  1. `user_prompt` is recorded to the session store before the draft model runs.
  2. `build_session_context(..., target_provider="local")` is called and its
     result is threaded into execute_chain/execute_agent as `context=`.
  3. `routed_qa` is recorded on a successful direct result.
  4. Fail-open: if session_store raises anywhere, routing still completes.
  5. Empty-context correctness: build_session_context() returning "" doesn't
     break the call (context=None/"" is passed through, not fabricated).

Runs main() in-process (importlib-loaded module) with chain_builder,
direct_executor, and session_store monkeypatched to fakes — no real network
calls, no real Ollama/API calls, no real ~/.llm-router writes. Path.home() is
monkeypatched to tmp_path as defense-in-depth alongside directly neutralizing
every fire-and-forget logger (model_tracking, savings_logger) so this test
can never touch the real user's home directory even if some module already
cached a Path.home()-derived constant earlier in the pytest session.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"

# A general-knowledge query, deliberately NOT context-dependent (no determiner
# + code/file noun like "the bug" / "this file") so `_is_context_dependent()`
# returns False and `_direct_enabled` stays True. Confirmed via
# GOLDEN_ROUTE_CASES in test_auto_route_hook.py: task_type="query",
# complexity="simple", tool="llm_query".
SAFE_PROMPT = "What does os.path.join do?"


def _load():
    cached = sys.modules.get("auto_route_session_ctx")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_session_ctx", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_session_ctx"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ar(monkeypatch, tmp_path):
    module = _load()
    # Defense-in-depth: neutralize every path this run could use to touch the
    # real ~/.llm-router, regardless of when each constant was first computed.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(module, "_ROUTER_DIR", tmp_path / ".llm-router", raising=False)
    monkeypatch.setattr(module, "log_routing_decision", lambda **kw: None, raising=False)

    import llm_router.hooks.savings_logger as savings_logger
    monkeypatch.setattr(savings_logger, "log_direct_savings", lambda **kw: None)
    monkeypatch.setattr(savings_logger, "log_direct_to_db", lambda **kw: None)

    monkeypatch.setenv("LLM_ROUTER_DISABLE_LLM_CLASSIFIERS", "1")
    monkeypatch.setenv("LLM_ROUTER_DIRECT_EXECUTION", "1")
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "suggest")
    monkeypatch.setenv("LLM_ROUTER_ZERO_CLAUDE", "off")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    return module


class _Recorder:
    """Captures every session_store call made during a run() invocation."""

    def __init__(self):
        self.record_event_calls: list[dict] = []
        self.build_session_context_calls: list[dict] = []
        self.build_session_context_return = "PRIOR SESSION CONTEXT\n---\n"
        self.record_event_raises = False
        self.build_session_context_raises = False

    def record_event(self, session_id, kind, content, **kwargs):
        if self.record_event_raises:
            raise RuntimeError("boom: record_event")
        self.record_event_calls.append(
            {"session_id": session_id, "kind": kind, "content": content, **kwargs}
        )

    def build_session_context(self, session_id, **kwargs):
        if self.build_session_context_raises:
            raise RuntimeError("boom: build_session_context")
        self.build_session_context_calls.append({"session_id": session_id, **kwargs})
        return self.build_session_context_return


def _patch_direct_execution(ar, monkeypatch, *, execute_chain_fn, needs_tools=False):
    import llm_router.hooks.chain_builder as chain_builder
    import llm_router.hooks.direct_executor as direct_executor

    fake_model = direct_executor.ModelSpec(provider="ollama", model="fake-model")

    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 10.0))
    monkeypatch.setattr(chain_builder, "build_chain", lambda complexity, zone, task_type: [fake_model])
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda prompt, task_type: needs_tools)
    monkeypatch.setattr(direct_executor, "execute_chain", execute_chain_fn)
    return fake_model


def _run(ar, monkeypatch, prompt: str, session_id: str = "sess-abc123"):
    payload = json.dumps({"prompt": prompt, "session_id": session_id})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    with pytest.raises(SystemExit) as exc_info:
        ar.main()
    assert exc_info.value.code == 0
    return out.getvalue()


# ── 1+2+3: happy path — record, build+thread context, record result ─────────

def test_records_user_prompt_before_draft(ar, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(ar, "session_store", rec, raising=False)
    import llm_router.session_store as real_session_store
    monkeypatch.setattr(real_session_store, "record_event", rec.record_event)
    monkeypatch.setattr(real_session_store, "build_session_context", rec.build_session_context)

    import llm_router.hooks.direct_executor as direct_executor

    def fake_execute_chain(prompt, chain, task_type, timeout=4, history=None, context=None):
        return direct_executor.DirectResult(
            text="os.path.join joins path components.",
            model=chain[0],
            latency_ms=42,
            input_tokens=3,
            output_tokens=7,
        )

    _patch_direct_execution(ar, monkeypatch, execute_chain_fn=fake_execute_chain)

    _run(ar, monkeypatch, SAFE_PROMPT, session_id="sess-abc123")

    user_prompt_events = [c for c in rec.record_event_calls if c["kind"] == "user_prompt"]
    assert len(user_prompt_events) == 1
    assert user_prompt_events[0]["session_id"] == "sess-abc123"
    assert user_prompt_events[0]["content"] == SAFE_PROMPT
    assert user_prompt_events[0]["role"] == "user"
    assert user_prompt_events[0]["task_type"] == "query"


def test_builds_and_threads_session_context_into_execute_chain(ar, monkeypatch):
    rec = _Recorder()
    import llm_router.session_store as real_session_store
    monkeypatch.setattr(real_session_store, "record_event", rec.record_event)
    monkeypatch.setattr(real_session_store, "build_session_context", rec.build_session_context)

    import llm_router.hooks.direct_executor as direct_executor
    captured = {}

    def fake_execute_chain(prompt, chain, task_type, timeout=4, history=None, context=None):
        captured["context"] = context
        captured["prompt"] = prompt
        return direct_executor.DirectResult(
            text="answer", model=chain[0], latency_ms=1, input_tokens=1, output_tokens=1,
        )

    _patch_direct_execution(ar, monkeypatch, execute_chain_fn=fake_execute_chain)

    _run(ar, monkeypatch, SAFE_PROMPT, session_id="sess-xyz")

    assert len(rec.build_session_context_calls) == 1
    call = rec.build_session_context_calls[0]
    assert call["session_id"] == "sess-xyz"
    assert call["target_provider"] == "local"
    assert call["task_type"] == "query"
    assert call["query"] == SAFE_PROMPT
    # The exact string built by build_session_context must be what execute_chain saw.
    assert captured["context"] == rec.build_session_context_return


def test_records_routed_qa_on_success(ar, monkeypatch):
    rec = _Recorder()
    import llm_router.session_store as real_session_store
    monkeypatch.setattr(real_session_store, "record_event", rec.record_event)
    monkeypatch.setattr(real_session_store, "build_session_context", rec.build_session_context)

    import llm_router.hooks.direct_executor as direct_executor

    def fake_execute_chain(prompt, chain, task_type, timeout=4, history=None, context=None):
        return direct_executor.DirectResult(
            text="the answer text", model=chain[0], latency_ms=5, input_tokens=2, output_tokens=4,
        )

    _patch_direct_execution(ar, monkeypatch, execute_chain_fn=fake_execute_chain)

    _run(ar, monkeypatch, SAFE_PROMPT, session_id="sess-qa")

    routed = [c for c in rec.record_event_calls if c["kind"] == "routed_qa"]
    assert len(routed) == 1
    assert routed[0]["session_id"] == "sess-qa"
    assert routed[0]["content"] == "the answer text"
    assert routed[0]["role"] == "assistant"
    assert routed[0]["task_type"] == "query"
    assert routed[0]["model"] == "ollama/fake-model"


# ── 4: fail-open — session_store raising never blocks routing ───────────────

def test_fail_open_when_record_event_raises(ar, monkeypatch):
    rec = _Recorder()
    rec.record_event_raises = True
    import llm_router.session_store as real_session_store
    monkeypatch.setattr(real_session_store, "record_event", rec.record_event)
    monkeypatch.setattr(real_session_store, "build_session_context", rec.build_session_context)

    import llm_router.hooks.direct_executor as direct_executor

    def fake_execute_chain(prompt, chain, task_type, timeout=4, history=None, context=None):
        return direct_executor.DirectResult(
            text="answer despite store failure", model=chain[0], latency_ms=1,
            input_tokens=1, output_tokens=1,
        )

    _patch_direct_execution(ar, monkeypatch, execute_chain_fn=fake_execute_chain)

    stdout_text = _run(ar, monkeypatch, SAFE_PROMPT, session_id="sess-fail1")

    # record_event was attempted (and raised) but routing still produced output.
    assert stdout_text.strip()
    output = json.loads(stdout_text)
    assert "hookSpecificOutput" in output or output.get("decision") in ("block", "approve")


def test_fail_open_when_build_session_context_raises(ar, monkeypatch):
    rec = _Recorder()
    rec.build_session_context_raises = True
    import llm_router.session_store as real_session_store
    monkeypatch.setattr(real_session_store, "record_event", rec.record_event)
    monkeypatch.setattr(real_session_store, "build_session_context", rec.build_session_context)

    import llm_router.hooks.direct_executor as direct_executor
    captured = {}

    def fake_execute_chain(prompt, chain, task_type, timeout=4, history=None, context=None):
        captured["context"] = context
        return direct_executor.DirectResult(
            text="answer despite context-build failure", model=chain[0], latency_ms=1,
            input_tokens=1, output_tokens=1,
        )

    _patch_direct_execution(ar, monkeypatch, execute_chain_fn=fake_execute_chain)

    stdout_text = _run(ar, monkeypatch, SAFE_PROMPT, session_id="sess-fail2")

    assert stdout_text.strip()
    # build_session_context raised → _session_ctx falls back to None, and the
    # draft call still ran (with context=None), rather than the whole hook
    # blowing up.
    assert captured["context"] is None
    routed = [c for c in rec.record_event_calls if c["kind"] == "routed_qa"]
    assert len(routed) == 1  # the routed_qa record_event still fires normally


# ── 5: empty-context correctness ─────────────────────────────────────────────

def test_empty_context_is_passed_through_not_fabricated(ar, monkeypatch):
    rec = _Recorder()
    rec.build_session_context_return = ""  # no prior events this session
    import llm_router.session_store as real_session_store
    monkeypatch.setattr(real_session_store, "record_event", rec.record_event)
    monkeypatch.setattr(real_session_store, "build_session_context", rec.build_session_context)

    import llm_router.hooks.direct_executor as direct_executor
    captured = {}

    def fake_execute_chain(prompt, chain, task_type, timeout=4, history=None, context=None):
        captured["context"] = context
        return direct_executor.DirectResult(
            text="answer", model=chain[0], latency_ms=1, input_tokens=1, output_tokens=1,
        )

    _patch_direct_execution(ar, monkeypatch, execute_chain_fn=fake_execute_chain)

    _run(ar, monkeypatch, SAFE_PROMPT, session_id="sess-empty")

    assert len(rec.build_session_context_calls) == 1
    # The empty string build_session_context legitimately returned is exactly
    # what gets threaded through — nothing invents placeholder context.
    assert captured["context"] == ""


def test_no_session_id_skips_all_session_store_calls(ar, monkeypatch):
    rec = _Recorder()
    import llm_router.session_store as real_session_store
    monkeypatch.setattr(real_session_store, "record_event", rec.record_event)
    monkeypatch.setattr(real_session_store, "build_session_context", rec.build_session_context)

    import llm_router.hooks.direct_executor as direct_executor
    captured = {}

    def fake_execute_chain(prompt, chain, task_type, timeout=4, history=None, context=None):
        captured["context"] = context
        return direct_executor.DirectResult(
            text="answer", model=chain[0], latency_ms=1, input_tokens=1, output_tokens=1,
        )

    _patch_direct_execution(ar, monkeypatch, execute_chain_fn=fake_execute_chain)

    _run(ar, monkeypatch, SAFE_PROMPT, session_id="")

    assert rec.record_event_calls == []
    assert rec.build_session_context_calls == []
    assert captured["context"] is None
