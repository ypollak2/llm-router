"""The cross-host event map must be honest about what is not yet known (task 16).

The map's job is to stop three host ports from re-deriving the same mapping and
drifting. Its second job — the one these tests mostly guard — is to make the
difference between "verified against a real payload" and "read off a docs page"
impossible to lose track of. A port that reads an invented field name gets None
forever and fails silently, which is the most expensive way to be wrong.
"""

from __future__ import annotations

import pytest

from llm_router.hosts.events import (
    CLAUDE_CODE,
    CODEX,
    CURSOR,
    HOSTS,
    Event,
    host_event_name,
    routing_ready,
    supports,
    unverified_fields,
)


def test_claude_code_matches_the_hooks_actually_shipped():
    """The Claude Code row is the one thing here verified from working code."""
    assert unverified_fields("claude-code") == [], (
        "Claude Code's payload keys are read by this repo's own live hooks, so "
        "none of them should be marked unverified"
    )
    assert CLAUDE_CODE.prompt_key == "prompt"
    assert CLAUDE_CODE.session_key == "session_id"
    assert CLAUDE_CODE.block_shape == {"decision": "block", "reason": "<text>"}


def test_claude_code_is_the_only_routing_ready_host_today():
    """Guards the claim the host support matrix will print (task 29).

    When a port lands and captures real payloads, this test changes with it —
    deliberately, so nobody can widen the claim without touching the assertion.
    """
    ready = {name for name in HOSTS if routing_ready(name)}
    assert ready == {"claude-code"}, (
        f"routing-ready hosts changed to {ready}. If a port genuinely landed, "
        "update this test in the same commit; if not, a payload key was filled "
        "in from memory rather than captured."
    )


@pytest.mark.parametrize("host", ["codex", "cursor"])
def test_unported_hosts_declare_their_unknowns(host):
    """A host we have not run yet must not pretend to know its payload."""
    assert unverified_fields(host), (
        f"{host} claims verified payload keys, but no port has captured a real "
        "payload from it. Filling these in from documentation is exactly the "
        "failure this field exists to prevent."
    )


def test_prompt_interception_exists_on_every_target_host():
    """The whole W3 thesis: the hook monopoly is over."""
    for host in ("claude-code", "codex", "cursor"):
        assert supports(host, Event.PROMPT_SUBMIT), f"{host} cannot intercept prompts"
        assert HOSTS[host].can_block_prompt, f"{host} cannot block a prompt"


def test_event_names_are_not_interchangeable():
    """Each host spells the same event differently — the reason this map exists."""
    submit = {h: host_event_name(h, Event.PROMPT_SUBMIT) for h in ("claude-code", "codex", "cursor")}
    assert submit["claude-code"] == "UserPromptSubmit"
    assert submit["codex"] == "UserPromptSubmit"
    assert submit["cursor"] == "beforeSubmitPrompt"


def test_asking_for_an_unsupported_event_raises():
    """Silently returning the canonical name would write a hooks.json the host
    ignores without complaint — the worst possible failure mode."""
    with pytest.raises(KeyError, match="no lifecycle event"):
        host_event_name("gemini-cli", Event.PRE_TOOL)


def test_codex_rewrite_capability_is_recorded():
    """Codex PreToolUse can rewrite arguments; Claude Code's cannot.

    The shared core must therefore treat rewrite as optional. Recording it here
    stops the extraction from hardcoding allow/deny.
    """
    assert CODEX.can_rewrite_tool_input is True
    assert CLAUDE_CODE.can_rewrite_tool_input is False


def test_cursor_scope_limitation_is_documented():
    """Cursor hooks are project-scoped with no plugin-root variable.

    A global install that silently works in one repo and not another is worse
    than one that says so, hence task 25.
    """
    assert CURSOR.plugin_root_var is None
    assert any("PROJECT-scoped" in n for n in CURSOR.notes)
    assert any("Cloud agents" in n for n in CURSOR.notes)
