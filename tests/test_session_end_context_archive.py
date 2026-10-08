"""session-end.py's Session Context Accumulator wiring.

P0.1 (BUGS.md "session context archived every turn"): this script is
registered on BOTH Stop (per-turn summary) and SessionEnd. Claude Code fires
Stop at the end of EVERY turn, so archiving there deleted the session's
durable context after turn 1. Only a payload with
``hook_event_name == "SessionEnd"`` archives now; Stop never does.

Covers the block in main() that, on SessionEnd, resolves this
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
    mod = _load_hook_module()
    # P0.9: a Stop detaches a child that reads the keychain and calls the usage
    # endpoint. These tests are about archiving; they must not start it.
    monkeypatch.setattr(mod, "_spawn_background_stop_work", lambda: None, raising=False)
    return mod


def _run_main(mod, monkeypatch, payload) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    mod.main()  # must complete normally — session-end's main() has no sys.exit
    return stdout.getvalue()


# ── happy path: session_id resolves ──────────────────────────────────────────

def _spy(monkeypatch, resolved="resolved-sess-1"):
    resolve_calls: list = []
    archive_calls: list = []
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(
        real_session_store,
        "resolve_session_id",
        lambda explicit=None: (resolve_calls.append(explicit), resolved)[1],
    )
    monkeypatch.setattr(real_session_store, "archive_session", lambda sid: archive_calls.append(sid))
    return resolve_calls, archive_calls


def test_stop_never_archives(hook, monkeypatch):
    """Stop fires after every turn. It must not delete the session's context."""
    _resolve_calls, archive_calls = _spy(monkeypatch)

    out = _run_main(hook, monkeypatch, {"session_id": "cc-session-real", "hook_event_name": "Stop"})

    assert archive_calls == []
    assert out  # the per-turn summary still renders on Stop


def test_payload_without_event_name_never_archives(hook, monkeypatch):
    """Deleting data is the destructive act, so an unlabelled payload does not."""
    _resolve_calls, archive_calls = _spy(monkeypatch)

    _run_main(hook, monkeypatch, {"session_id": "cc-session-real"})

    assert archive_calls == []


def test_session_end_archives_with_resolved_session_id(hook, monkeypatch):
    resolve_calls, archive_calls = _spy(monkeypatch)

    _run_main(hook, monkeypatch, {"session_id": "cc-session-real", "hook_event_name": "SessionEnd"})

    # resolve_session_id must be given the real session_id straight from the
    # hook's stdin payload as its explicit-override argument.
    assert resolve_calls == ["cc-session-real"]
    assert archive_calls == ["resolved-sess-1"]


def test_session_file_survives_stop_with_its_events(hook, monkeypatch, tmp_path):
    """End to end on the real store: 5 turns of events, a Stop after each, and
    the JSONL is still there with every event; SessionEnd then removes it."""
    import llm_router.session_store as ss

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "lr-home"))
    sid = "p01-stop-survival"
    project = str(tmp_path / "proj")
    os.makedirs(project, exist_ok=True)
    monkeypatch.chdir(project)
    counts = []
    for turn in range(5):
        ss.record_event(sid, "user_prompt", f"turn {turn} question about module_{turn}")
        _run_main(hook, monkeypatch, {"session_id": sid, "hook_event_name": "Stop"})
        path = ss._session_path(sid)
        assert path.exists(), f"session file deleted by Stop after turn {turn}"
        counts.append(len(path.read_text().splitlines()))
    assert counts == sorted(counts) and counts[-1] >= 5, counts

    _run_main(hook, monkeypatch, {"session_id": sid, "hook_event_name": "SessionEnd"})
    assert not ss._session_path(sid).exists()


def test_installer_registers_session_end_on_both_events():
    from llm_router.install_hooks import _HOOK_DEFS

    events = {ev for src, _dst, ev, _m in _HOOK_DEFS if src == "session-end.py"}
    assert events == {"Stop", "SessionEnd"}


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

    _run_main(hook, monkeypatch, {"hook_event_name": "SessionEnd"})

    assert resolve_calls == [None]
    assert archive_calls == []  # resolve_session_id returned falsy -> no archive


def test_falsy_resolved_session_id_skips_archive(hook, monkeypatch):
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(real_session_store, "resolve_session_id", lambda explicit=None: None)
    archive_calls = []
    monkeypatch.setattr(real_session_store, "archive_session", lambda sid: archive_calls.append(sid))

    _run_main(hook, monkeypatch, {"session_id": "whatever", "hook_event_name": "SessionEnd"})

    assert archive_calls == []


def test_non_dict_hook_input_never_archives(hook, monkeypatch):
    """A non-dict payload carries no hook_event_name, so it cannot be SessionEnd."""
    _resolve_calls, archive_calls = _spy(monkeypatch)

    out = _run_main(hook, monkeypatch, json.dumps(["not", "a", "dict"]))

    assert archive_calls == []
    assert out  # treated as a Stop: the summary still renders


def test_malformed_json_stdin_never_archives(hook, monkeypatch):
    _resolve_calls, archive_calls = _spy(monkeypatch)

    out = _run_main(hook, monkeypatch, "{not valid json")

    assert archive_calls == []
    assert out


# ── fail-open ─────────────────────────────────────────────────────────────────

def test_fail_open_when_resolve_session_id_raises(hook, monkeypatch):
    import llm_router.session_store as real_session_store

    def _raise(explicit=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(real_session_store, "resolve_session_id", _raise)

    # Must still complete normally despite resolve_session_id raising.
    _run_main(hook, monkeypatch, {"session_id": "cc-session-resolve-fails", "hook_event_name": "SessionEnd"})


def test_fail_open_when_archive_session_raises(hook, monkeypatch):
    import llm_router.session_store as real_session_store

    monkeypatch.setattr(real_session_store, "resolve_session_id", lambda explicit=None: "sess-1")

    def _raise(sid):
        raise RuntimeError("boom")

    monkeypatch.setattr(real_session_store, "archive_session", _raise)

    _run_main(hook, monkeypatch, {"session_id": "cc-session-archive-fails", "hook_event_name": "SessionEnd"})
