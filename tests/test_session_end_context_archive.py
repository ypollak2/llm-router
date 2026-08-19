"""session-end.py's Session Context Accumulator wiring.

Covers the block added to main() that, on every session end, resolves this
session's id (real session_id from the hook's stdin payload, else env vars,
else the pointer file written by session-start.py — the full precedence
chain lives in session_store.resolve_session_id()) and archives (deletes)
its durable JSONL event store via session_store.archive_session(), since
the session is over and there is nothing left to inject context into.

The whole block is one `try/except Exception: pass` — a failure anywhere
(import error, resolve_session_id raising, archive_session raising) must
never block the rest of session-end's summary/dashboard output.

Run in-process via importlib. Unlike session-start.py's equivalent test,
main() here does NOT need its downstream sections (dashboard rendering, DB
queries, savings panel, etc.) mocked out — they are each already
independently wrapped in their own fail-open try/except blocks and behave
correctly against an empty, sandboxed ~/.llm-router (no usage.db present),
confirmed by manual probe before writing this file. HOME is sandboxed to
tmp_path before the module is loaded (STATE_DIR is a module-level constant
computed via os.path.expanduser("~/.llm-router") at import time, same
constraint as session-start.py).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "session-end.py"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("session_end_hook_ctx", HOOK_PATH)
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
    return _load_hook_module()


def _run_main(mod, monkeypatch, payload) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    mod.main()  # must complete normally — session-end's main() has no sys.exit
    return stdout.getvalue()


# ── happy path: session_id resolves ──────────────────────────────────────────

def test_archive_session_called_with_resolved_session_id(hook, monkeypatch):
    resolve_calls = []
    archive_calls = []
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(
        real_session_store,
        "resolve_session_id",
        lambda explicit=None: (resolve_calls.append(explicit), "resolved-sess-1")[1],
    )
    monkeypatch.setattr(real_session_store, "archive_session", lambda sid: archive_calls.append(sid))

    out = _run_main(hook, monkeypatch, {"session_id": "cc-session-real"})

    # resolve_session_id must be given the real session_id straight from the
    # hook's stdin payload as its explicit-override argument.
    assert resolve_calls == ["cc-session-real"]
    assert archive_calls == ["resolved-sess-1"]
    assert out  # session-end still produced its normal summary output


def test_no_explicit_session_id_still_passes_none_through_resolution_chain(hook, monkeypatch):
    """No "session_id" key in the hook payload -> explicit=None is passed to
    resolve_session_id(), which is then responsible for falling back to env
    vars / the pointer file itself. This test only confirms the wiring calls
    resolve_session_id(None), not resolve_session_id's own fallback logic
    (covered separately at the session_store unit-test level)."""
    resolve_calls = []
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(
        real_session_store,
        "resolve_session_id",
        lambda explicit=None: resolve_calls.append(explicit) or None,
    )
    archive_calls = []
    monkeypatch.setattr(real_session_store, "archive_session", lambda sid: archive_calls.append(sid))

    _run_main(hook, monkeypatch, {})

    assert resolve_calls == [None]
    assert archive_calls == []  # resolve_session_id returned falsy -> no archive


def test_falsy_resolved_session_id_skips_archive(hook, monkeypatch):
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(real_session_store, "resolve_session_id", lambda explicit=None: None)
    archive_calls = []
    monkeypatch.setattr(real_session_store, "archive_session", lambda sid: archive_calls.append(sid))

    _run_main(hook, monkeypatch, {"session_id": "whatever"})

    assert archive_calls == []


def test_non_dict_hook_input_passes_none_as_explicit(hook, monkeypatch):
    resolve_calls = []
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(
        real_session_store,
        "resolve_session_id",
        lambda explicit=None: resolve_calls.append(explicit) or None,
    )
    monkeypatch.setattr(real_session_store, "archive_session", lambda sid: None)

    _run_main(hook, monkeypatch, json.dumps(["not", "a", "dict"]))

    assert resolve_calls == [None]


def test_malformed_json_stdin_passes_none_as_explicit(hook, monkeypatch):
    resolve_calls = []
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(
        real_session_store,
        "resolve_session_id",
        lambda explicit=None: resolve_calls.append(explicit) or None,
    )
    monkeypatch.setattr(real_session_store, "archive_session", lambda sid: None)

    _run_main(hook, monkeypatch, "{not valid json")

    assert resolve_calls == [None]


# ── fail-open ─────────────────────────────────────────────────────────────────

def test_fail_open_when_resolve_session_id_raises(hook, monkeypatch):
    import llm_router.session_store as real_session_store

    def _raise(explicit=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(real_session_store, "resolve_session_id", _raise)

    # Must still complete and produce the normal session-end summary despite
    # resolve_session_id raising.
    out = _run_main(hook, monkeypatch, {"session_id": "cc-session-resolve-fails"})
    assert out


def test_fail_open_when_archive_session_raises(hook, monkeypatch):
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(real_session_store, "resolve_session_id", lambda explicit=None: "sess-1")

    def _raise(sid):
        raise RuntimeError("boom")

    monkeypatch.setattr(real_session_store, "archive_session", _raise)

    out = _run_main(hook, monkeypatch, {"session_id": "cc-session-archive-fails"})
    assert out
