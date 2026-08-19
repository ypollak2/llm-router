"""PostToolUse hook — src/llm_router/hooks/context-capture.py.

Tests the write side of the Session Context Accumulator: every PostToolUse
event should durably record into the session store as `tool_call`, subject to
noise filtering, self-poisoning prevention, and a minimum content-length
threshold — and must always fail open (exit 0, never raise) regardless of
what goes wrong downstream.

Run in-process via importlib (matching tests/test_free_tier_drafts.py's
pattern) so private helpers (`_is_noisy_tool`, `_get_session_id`, `main`) are
directly testable and monkeypatchable without subprocess overhead.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "context-capture.py"


def _load():
    cached = sys.modules.get("context_capture_hook")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("context_capture_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["context_capture_hook"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cc(monkeypatch, tmp_path):
    module = _load()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return module


def _run(cc, monkeypatch, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    with pytest.raises(SystemExit) as exc_info:
        cc.main()
    assert exc_info.value.code == 0


# ── noisy-tool filtering ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__llm_router__llm_query",
        "mcp__llm_router__llm_code",
        "TodoWrite",
        "TodoRead",
        "BashOutput",
        "KillShell",
        "",
    ],
)
def test_noisy_tools_are_skipped(cc, monkeypatch, tool_name):
    calls = []
    monkeypatch.setattr(cc, "_get_session_id", lambda: "sess-1")

    class _Fail:
        def record_event(self, *a, **kw):
            calls.append((a, kw))
            raise AssertionError("record_event must not be called for noisy tools")

    import llm_router.session_store as real_session_store
    monkeypatch.setattr(real_session_store, "record_event", _Fail().record_event)

    _run(
        cc,
        monkeypatch,
        {
            "tool_name": tool_name,
            "tool_input": {"x": "y" * 40},
            "tool_response": "z" * 40,
        },
    )
    assert calls == []


def test_screenshot_and_computer_tools_are_skipped_case_insensitive(cc):
    assert cc._is_noisy_tool("computer") is True
    assert cc._is_noisy_tool("Screenshot") is True
    assert cc._is_noisy_tool("browser_zoom") is True
    assert cc._is_noisy_tool("Read") is False


# ── self-poisoning guard ─────────────────────────────────────────────────────

def test_sentinel_open_in_result_is_skipped(cc, monkeypatch):
    import llm_router.session_store as real_session_store
    from llm_router.session_store import SENTINEL_OPEN

    recorded = []
    monkeypatch.setattr(real_session_store, "record_event", lambda *a, **kw: recorded.append((a, kw)))

    _run(
        cc,
        monkeypatch,
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x"},
            "tool_response": f"{SENTINEL_OPEN} some prior context that would self-poison " + "x" * 40,
        },
    )
    assert recorded == []


def test_sentinel_open_in_tool_input_is_skipped(cc, monkeypatch):
    import llm_router.session_store as real_session_store
    from llm_router.session_store import SENTINEL_OPEN

    recorded = []
    monkeypatch.setattr(real_session_store, "record_event", lambda *a, **kw: recorded.append((a, kw)))

    _run(
        cc,
        monkeypatch,
        {
            "tool_name": "Write",
            "tool_input": {"content": f"{SENTINEL_OPEN} injected " + "x" * 40},
            "tool_response": "ok " * 20,
        },
    )
    assert recorded == []


# ── minimum content-length threshold ─────────────────────────────────────────

def test_short_content_is_skipped(cc, monkeypatch):
    import llm_router.session_store as real_session_store
    recorded = []
    monkeypatch.setattr(real_session_store, "record_event", lambda *a, **kw: recorded.append((a, kw)))
    monkeypatch.setattr(cc, "_get_session_id", lambda: "sess-1")

    _run(cc, monkeypatch, {"tool_name": "Read", "tool_input": {}, "tool_response": "ok"})
    assert recorded == []


# ── successful record_event call ─────────────────────────────────────────────

def test_successful_record_event_call_shape(cc, monkeypatch):
    import llm_router.session_store as real_session_store
    recorded = []
    monkeypatch.setattr(real_session_store, "record_event", lambda *a, **kw: recorded.append((a, kw)))
    monkeypatch.setattr(cc, "_get_session_id", lambda: "sess-42")

    _run(
        cc,
        monkeypatch,
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "/Users/yaliandrona/Projects/LLM Router/README.md"},
            "tool_response": "This is the README content, long enough to pass the threshold check.",
        },
    )

    assert len(recorded) == 1
    args, kwargs = recorded[0]
    session_id, kind, content = args[0], args[1], args[2]
    assert session_id == "sess-42"
    assert kind == "tool_call"
    assert "Read(" in content
    assert "README content" in content
    assert kwargs.get("role") == "tool"
    assert kwargs.get("tool") == "Read"


def test_no_session_id_skips_record_event(cc, monkeypatch):
    import llm_router.session_store as real_session_store
    recorded = []
    monkeypatch.setattr(real_session_store, "record_event", lambda *a, **kw: recorded.append((a, kw)))
    monkeypatch.setattr(cc, "_get_session_id", lambda: None)

    _run(
        cc,
        monkeypatch,
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x"},
            "tool_response": "long enough content to pass the min-length threshold check here",
        },
    )
    assert recorded == []


# ── fail-open behavior ───────────────────────────────────────────────────────

def test_fail_open_when_record_event_raises(cc, monkeypatch):
    import llm_router.session_store as real_session_store
    monkeypatch.setattr(cc, "_get_session_id", lambda: "sess-1")

    def _raise(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(real_session_store, "record_event", _raise)

    # Must still exit 0 despite record_event raising.
    _run(
        cc,
        monkeypatch,
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x"},
            "tool_response": "long enough content to pass the min-length threshold check here",
        },
    )


def test_fail_open_on_malformed_json_stdin(cc, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not valid json"))
    with pytest.raises(SystemExit) as exc_info:
        cc.main()
    assert exc_info.value.code == 0


def test_fail_open_on_empty_stdin(cc, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as exc_info:
        cc.main()
    assert exc_info.value.code == 0


def test_fail_open_on_non_dict_json_stdin(cc, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(["not", "a", "dict"])))
    with pytest.raises(SystemExit) as exc_info:
        cc.main()
    assert exc_info.value.code == 0


def test_get_session_id_fails_open_when_resolve_session_id_raises(cc, monkeypatch):
    # _get_session_id() itself (not main()) wraps session_store.resolve_session_id()
    # in try/except — confirm that internal fail-open contract directly.
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    import llm_router.session_store as real_session_store

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(real_session_store, "resolve_session_id", _raise)
    assert cc._get_session_id() is None
