"""BIND W1 / F3: void-delegation detector and per-session breaker (``llm_router.codex_handoff``).

A delegated Codex handoff is a PreToolUse[Agent] ``deny``. When the host ignores it (docs/BUGS.md BIND-1: 317 of
317 in the window 2026-10-04..2026-11-03), the Agent call launches anyway and PostToolUse[Agent] fires for
the same ``tool_use_id``; a call whose deny is honoured never reaches PostToolUse. That asymmetry is the
detector: a delegated ``tool_use_id`` seen in PostToolUse[Agent] is void. Three voids in one session open a
breaker, so the router stops spending Codex time on a host that discards the result.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from llm_router import codex_handoff as ch
from tests._bind_support import HOOKS_SRC, hook_env, ledger_rows

SID = "void-session"


@pytest.fixture
def home(tmp_path, monkeypatch):
    state = tmp_path / ".llm-router"
    state.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    return tmp_path


def test_launch_of_a_delivered_call_is_void_and_counted(home):
    h = ch.prompt_fingerprint("analyze the pager")
    ch.note_delivered(SID, "toolu_1", h)
    assert ch.voids_in_session(SID) == 0
    assert ch.note_agent_launched(SID, "toolu_1") == 1
    assert ch.voids_in_session(SID) == 1


def test_a_call_that_was_never_delivered_is_not_void(home):
    ch.note_delivered(SID, "toolu_1", ch.prompt_fingerprint("a"))
    assert ch.note_agent_launched(SID, "toolu_other") is None  # an ordinary Agent call
    assert ch.voids_in_session(SID) == 0


def test_a_void_is_counted_once(home):
    ch.note_delivered(SID, "toolu_1", ch.prompt_fingerprint("a"))
    assert ch.note_agent_launched(SID, "toolu_1") == 1
    assert ch.note_agent_launched(SID, "toolu_1") is None  # PostToolUse fired twice for one id


def test_breaker_opens_after_three_voids_and_only_for_that_session(home):
    assert not ch.breaker_open(SID)
    for n in range(1, 4):
        ch.note_delivered(SID, f"toolu_{n}", ch.prompt_fingerprint(f"p{n}"))
        assert ch.note_agent_launched(SID, f"toolu_{n}") == n
        assert ch.breaker_open(SID) is (n >= ch.VOID_LIMIT)
    assert ch.VOID_LIMIT == 3
    assert not ch.breaker_open("another-session")


def test_an_honoured_deny_leaves_the_prompt_marked_delivered(home):
    """The deny worked (no PostToolUse), so the same prompt coming back means the model found the result
    insufficient: it must not cost another Codex run."""
    h = ch.prompt_fingerprint("review the diff")
    ch.note_delivered(SID, "toolu_1", h)
    assert ch.was_delivered(SID, h)
    assert not ch.was_delivered(SID, ch.prompt_fingerprint("a different prompt"))
    assert not ch.was_delivered("another-session", h)


def test_delivered_entries_expire(home):
    h = ch.prompt_fingerprint("review the diff")
    ch.note_delivered(SID, "toolu_1", h, now=1_000.0)
    assert ch.was_delivered(SID, h, now=1_000.0 + ch.DELIVERED_TTL_S - 1)
    assert not ch.was_delivered(SID, h, now=1_000.0 + ch.DELIVERED_TTL_S + 1)


def test_state_shares_the_depth_file_without_clobbering_it(home):
    """The breaker state lives in the per-session depth file (SessionEnd already removes it), next to the
    slots and the nesting registry the agent hooks keep there."""
    depth = home / ".llm-router" / f"agent_depth_{SID}.json"
    depth.write_text(json.dumps({"slots": [["toolu_live", 9e9]], "depth": 1, "pending": [], "agents": {"a1": 2}}))
    ch.note_delivered(SID, "toolu_1", ch.prompt_fingerprint("p"))
    ch.note_agent_launched(SID, "toolu_1")
    data = json.loads(depth.read_text())
    assert data["slots"] == [["toolu_live", 9e9]] and data["agents"] == {"a1": 2}
    assert data["codex_handoff"]["void"] == 1
    assert oct(depth.stat().st_mode & 0o777) == "0o600"


def test_unreadable_state_fails_open_and_is_recorded(home, monkeypatch):
    depth = home / ".llm-router" / f"agent_depth_{SID}.json"
    depth.write_text("{not json")
    recorded = []
    monkeypatch.setattr("llm_router.failopen.record", lambda code, exc=None, *, detail="": recorded.append(code))
    assert ch.voids_in_session(SID) == 0 and not ch.breaker_open(SID)
    ch.note_delivered(SID, "toolu_1", ch.prompt_fingerprint("p"))  # must not raise
    assert ch.note_agent_launched(SID, "toolu_1") in (1, None)


def test_record_void_row_is_a_ledger_row_carrying_the_tool_use_id(home):
    ch.record_void(SID, "toolu_1", 2)
    rows = ledger_rows(home)
    assert len(rows) == 1
    row = rows[0]
    assert (row["lever"], row["outcome"], row["tool_use_id"], row["session_id"]) == (
        "agent_route_codex", "delegation_void", "toolu_1", SID)
    assert row["void_count"] == 2 and row["reason_code"] == "agent_launched_after_deny"


# ------------------------------------------------------------------ through the real PostToolUse hook


def _post_agent(home, tool_use_id, session=SID):
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_use_id": tool_use_id,
               "session_id": session, "tool_input": {"prompt": "x"},
               "tool_response": {"status": "async_launched"}}
    return subprocess.run(
        [sys.executable, str(HOOKS_SRC / "agent-depth-release.py")], input=json.dumps(payload),
        capture_output=True, text=True, cwd=str(home),
        env=hook_env(home, CLAUDE_CODE_SESSION_ID=session))


def test_post_tool_use_hook_marks_the_delegated_call_void(home):
    ch.note_delivered(SID, "toolu_void", ch.prompt_fingerprint("p"))
    proc = _post_agent(home, "toolu_void")
    assert proc.returncode == 0
    assert proc.stdout == "", proc.stdout  # this hook never speaks on stdout
    assert ch.voids_in_session(SID) == 1
    rows = [r for r in ledger_rows(home) if r["outcome"] == "delegation_void"]
    assert len(rows) == 1 and rows[0]["tool_use_id"] == "toolu_void"
    assert "void" in proc.stderr.lower()


def test_post_tool_use_hook_ignores_an_ordinary_agent_call(home):
    ch.note_delivered(SID, "toolu_void", ch.prompt_fingerprint("p"))
    proc = _post_agent(home, "toolu_ordinary")
    assert proc.returncode == 0 and proc.stdout == ""
    assert ch.voids_in_session(SID) == 0
    assert [r for r in ledger_rows(home) if r["outcome"] == "delegation_void"] == []


def test_post_tool_use_hook_with_no_state_does_nothing(home):
    proc = _post_agent(home, "toolu_x", session="never-delegated")
    assert proc.returncode == 0 and proc.stdout == ""
    assert not (home / ".llm-router" / "agent_depth_never-delegated.json").exists()
    assert not os.path.exists(home / ".llm-router" / "north_star_units.jsonl")
