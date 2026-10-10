"""CONTEXT-RESUME-1 (docs/bugs/CONTEXT-RESUME-1.md): the context store survives `claude --resume`.

Every `claude -p --resume` is its own SessionStart..SessionEnd lifecycle under the SAME session
id. SessionEnd used to delete the store, so a resumed session started empty and the line count
per session id never grew past one process's worth (live 2026-10-10: 4 and 7 lines at archive
after 10 resumed turns). Now SessionEnd moves the store to an archive and SessionStart
`source=resume` restores it. Runs the real hook mains against a sandboxed HOME.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

from llm_router import session_store as ss

_HOOKS = Path(__file__).parent.parent / "src" / "llm_router" / "hooks"
SID = "resume-sess-0001"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, _HOOKS / file)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


@pytest.fixture()
def hooks(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SESSION_CONTEXT", raising=False)
    start = _load("ss_hook_resume", "session-start.py")
    end = _load("se_hook_resume", "session-end.py")
    for n in ("_ensure_ollama_running", "_ensure_pxpipe_running", "_sync_pxpipe_anthropic_base_url",
              "_refresh_claude_usage", "_format_learned_memory", "_weekly_digest", "_latency_hint",
              "_preflight_check"):
        monkeypatch.setattr(start, n, lambda: "")
    for n in ("_maybe_refresh_benchmarks_bg", "_warm_ollama_bg", "_maybe_update_pull_routing_rules"):
        monkeypatch.setattr(start, n, lambda: None)
    monkeypatch.setattr(end, "_spawn_background_stop_work", lambda: None, raising=False)
    return start, end


def _run(mod, monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    mod.main()


def _lines() -> int:
    p = ss._session_path(SID)
    return len(p.read_text().splitlines()) if p.exists() else 0


def _turns(n: int, tag: str) -> None:
    for i in range(n):
        ss.record_event(SID, "user_prompt", f"{tag} prompt number {i}")


def test_line_count_non_decreasing_across_resumes(hooks, monkeypatch):
    start, end = hooks
    counts = []
    for proc in range(3):
        source = "startup" if proc == 0 else "resume"
        _run(start, monkeypatch, {"session_id": SID, "source": source, "hook_event_name": "SessionStart"})
        counts.append(_lines())  # what the resumed process starts with
        _turns(2, f"proc{proc}")
        _run(end, monkeypatch, {"session_id": SID, "hook_event_name": "Stop"})
        assert _lines() == counts[-1] + 2, "Stop must not archive"
        _run(end, monkeypatch, {"session_id": SID, "hook_event_name": "SessionEnd", "reason": "other"})
        # SessionEnd archives: live store gone, archive holds everything so far.
        assert not ss._session_path(SID).exists()
        assert len(ss._archive_path(SID).read_text().splitlines()) == 2 * (proc + 1)
    assert counts == [0, 2, 4]
    _run(start, monkeypatch, {"session_id": SID, "source": "resume", "hook_event_name": "SessionStart"})
    assert _lines() == 6
    assert not ss._archive_path(SID).exists(), "restore consumes the archive"
    texts = [e["content"] for e in map(json.loads, ss._session_path(SID).read_text().splitlines())]
    assert texts == [f"proc{p} prompt number {i}" for p in range(3) for i in range(2)]


@pytest.mark.parametrize("source", ["startup", "clear", "compact", None])
def test_only_resume_restores(hooks, monkeypatch, source):
    start, end = hooks
    _turns(2, "old")
    _run(end, monkeypatch, {"session_id": SID, "hook_event_name": "SessionEnd"})
    payload = {"session_id": SID, "hook_event_name": "SessionStart"}
    if source:
        payload["source"] = source
    _run(start, monkeypatch, payload)
    assert _lines() == 0
    assert ss._archive_path(SID).exists()


def test_restore_puts_archive_before_events_already_recorded():
    _turns(2, "old")
    ss.archive_session(SID)
    ss.record_event(SID, "user_prompt", "new-before-restore")
    assert ss.restore_session(SID) is True
    contents = [json.loads(x)["content"] for x in ss._session_path(SID).read_text().splitlines()]
    assert contents[-1] == "new-before-restore" and len(contents) == 3


def test_second_archive_merges_instead_of_clobbering():
    _turns(2, "a")
    ss.archive_session(SID)
    _turns(1, "b")
    ss.archive_session(SID)
    assert len(ss._archive_path(SID).read_text().splitlines()) == 3


def test_unresumed_archive_is_swept_after_ttl(tmp_path):
    _turns(1, "a")
    ss.archive_session(SID)
    arch = ss._archive_path(SID)
    ss.cleanup_old_sessions()
    assert arch.exists()
    old = time.time() - 8 * 86400
    os.utime(arch, (old, old))
    ss.cleanup_old_sessions()
    assert not arch.exists()


def test_kill_switch_off_retains_nothing(monkeypatch):
    _turns(1, "a")
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
    ss.archive_session(SID)
    assert not ss._session_path(SID).exists()
    assert not ss._archive_path(SID).exists()


def test_restore_without_archive_is_a_noop():
    assert ss.restore_session(SID) is False
    assert ss.restore_session(None) is False
