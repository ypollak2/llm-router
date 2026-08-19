"""session-start.py's Session Context Accumulator wiring.

Covers the block added to main() (see module docstring reference in
session-start.py) that, on every session start:

  1. Reads Claude Code's real session_id from the hook's own stdin JSON
     payload (distinct from SESSION_ID_FILE's fresh-per-session UUID, which
     four other consumers depend on and is untouched by this work).
  2. Writes it to the durable pointer file via session_store.write_pointer()
     so later hooks (context-capture.py, auto-route.py) can resolve the same
     session_id without needing an env var.
  3. Calls session_store.cleanup_old_sessions() unconditionally, regardless
     of whether a real session_id was present.
  4. Wraps all of the above in one try/except Exception: pass — a failure
     anywhere in this block (import error, write_pointer raising,
     cleanup_old_sessions raising) must never block session start; the rest
     of main() (banner, JSON output) must still run to completion.

Run in-process via importlib, matching tests/test_session_start_pxpipe.py's
_load_hook_module() helper (which snapshots/restores os.environ around
exec_module, since the hook's _load_dotenv() mutates os.environ at import
time). main() itself does a lot of unrelated work (Ollama/pxpipe
autostart, real OAuth usage refresh, background subprocess warmers) that
must never fire in a test — all of those functions are monkeypatched to
inert no-ops here, isolating the assertions to the pointer/cleanup wiring.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "session-start.py"


def _load_hook_module():
    """Import session-start.py fresh, with HOME already pointed at tmp_path.

    HOME must be set BEFORE exec_module runs: STATE_DIR (and everything
    derived from it) is a module-level constant computed via
    os.path.expanduser("~/.llm-router") at import time, so it only picks up a
    sandboxed HOME if that env var is set prior to loading, not after.
    """
    spec = importlib.util.spec_from_file_location("session_start_hook_ctx", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


@pytest.fixture()
def hook(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    mod = _load_hook_module()

    # Neutralize every side-effecting piece of main() unrelated to the
    # pointer/cleanup wiring under test: subprocess spawns, real network/OAuth
    # calls, and background detached processes must never fire in a test.
    monkeypatch.setattr(mod, "_ensure_ollama_running", lambda: "")
    monkeypatch.setattr(mod, "_ensure_pxpipe_running", lambda: "")
    monkeypatch.setattr(mod, "_sync_pxpipe_anthropic_base_url", lambda: "")
    monkeypatch.setattr(mod, "_refresh_claude_usage", lambda: "")
    monkeypatch.setattr(mod, "_format_learned_memory", lambda: "")
    monkeypatch.setattr(mod, "_weekly_digest", lambda: "")
    monkeypatch.setattr(mod, "_latency_hint", lambda: "")
    monkeypatch.setattr(mod, "_preflight_check", lambda: "")
    monkeypatch.setattr(mod, "_maybe_refresh_benchmarks_bg", lambda: None)
    monkeypatch.setattr(mod, "_warm_ollama_bg", lambda: None)
    monkeypatch.setattr(mod, "_maybe_update_pull_routing_rules", lambda: None)
    return mod


def _run_main(mod, monkeypatch, payload) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    mod.main()  # must complete normally (no sys.exit in this hook's main())
    return stdout.getvalue()


# ── happy path: real session_id present ──────────────────────────────────────

def test_write_pointer_called_with_real_session_id(hook, monkeypatch):
    calls = []
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(real_session_store, "write_pointer", lambda sid: calls.append(sid))
    monkeypatch.setattr(real_session_store, "cleanup_old_sessions", lambda: None)

    out = _run_main(hook, monkeypatch, {"session_id": "cc-session-abc123"})

    assert calls == ["cc-session-abc123"]
    # Session start must still complete and emit its normal hook JSON.
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_cleanup_old_sessions_called_unconditionally_with_session_id(hook, monkeypatch):
    cleanup_calls = []
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(real_session_store, "write_pointer", lambda sid: None)
    monkeypatch.setattr(real_session_store, "cleanup_old_sessions", lambda: cleanup_calls.append(1))

    _run_main(hook, monkeypatch, {"session_id": "cc-session-xyz"})

    assert cleanup_calls == [1]


# ── no real session_id in the hook payload ───────────────────────────────────

def test_no_session_id_skips_write_pointer_but_still_cleans_up(hook, monkeypatch):
    write_calls = []
    cleanup_calls = []
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(real_session_store, "write_pointer", lambda sid: write_calls.append(sid))
    monkeypatch.setattr(real_session_store, "cleanup_old_sessions", lambda: cleanup_calls.append(1))

    _run_main(hook, monkeypatch, {})  # no "session_id" key at all

    assert write_calls == []
    assert cleanup_calls == [1]


def test_non_dict_hook_input_skips_write_pointer_but_still_cleans_up(hook, monkeypatch):
    write_calls = []
    cleanup_calls = []
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(real_session_store, "write_pointer", lambda sid: write_calls.append(sid))
    monkeypatch.setattr(real_session_store, "cleanup_old_sessions", lambda: cleanup_calls.append(1))

    _run_main(hook, monkeypatch, json.dumps(["not", "a", "dict"]))

    assert write_calls == []
    assert cleanup_calls == [1]


def test_malformed_json_stdin_skips_write_pointer_but_still_cleans_up(hook, monkeypatch):
    """json.load(sys.stdin) fails -> _hook_input = {} -> not truthy session_id,
    but the block is still entered and cleanup_old_sessions still fires."""
    write_calls = []
    cleanup_calls = []
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(real_session_store, "write_pointer", lambda sid: write_calls.append(sid))
    monkeypatch.setattr(real_session_store, "cleanup_old_sessions", lambda: cleanup_calls.append(1))

    _run_main(hook, monkeypatch, "{not valid json")

    assert write_calls == []
    assert cleanup_calls == [1]


# ── fail-open ─────────────────────────────────────────────────────────────────

def test_fail_open_when_write_pointer_raises(hook, monkeypatch):
    import llm_router.session_store as real_session_store

    def _raise(sid):
        raise RuntimeError("boom")

    monkeypatch.setattr(real_session_store, "write_pointer", _raise)
    monkeypatch.setattr(real_session_store, "cleanup_old_sessions", lambda: None)

    out = _run_main(hook, monkeypatch, {"session_id": "cc-session-will-fail"})

    # Session start must still complete and emit its normal hook JSON despite
    # write_pointer raising.
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_fail_open_when_cleanup_old_sessions_raises(hook, monkeypatch):
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(real_session_store, "write_pointer", lambda sid: None)

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(real_session_store, "cleanup_old_sessions", _raise)

    out = _run_main(hook, monkeypatch, {"session_id": "cc-session-cleanup-fails"})

    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_fail_open_when_write_pointer_raises_import_error(hook, monkeypatch):
    """Same fail-open contract, exercised with an ImportError-shaped failure
    (representative of the `from llm_router import session_store` import line
    itself failing, e.g. a packaging/circular-import problem) rather than a
    plain RuntimeError — the bare `except Exception` must catch this too."""
    import llm_router.session_store as real_session_store

    def _raise(sid):
        raise ImportError("simulated import failure")

    monkeypatch.setattr(real_session_store, "write_pointer", _raise)
    monkeypatch.setattr(real_session_store, "cleanup_old_sessions", lambda: None)

    out = _run_main(hook, monkeypatch, {"session_id": "cc-session-import-fail"})

    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
