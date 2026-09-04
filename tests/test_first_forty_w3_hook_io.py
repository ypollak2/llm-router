"""The host-agnostic hook seam must refuse to guess (task 17).

Two failure modes this boundary exists to make impossible:

  * reading an invented payload key, which yields None forever and fails
    silently;
  * dropping a decision the host cannot express, which runs the ORIGINAL tool
    call while the hook believes it rewrote it.

Both are caught by raising rather than degrading.
"""

from __future__ import annotations

import pytest

from llm_router.hosts.events import Event
from llm_router.hosts.hook_io import (
    HookDecision,
    UnverifiedHost,
    normalize,
    render,
)


# ── normalize ─────────────────────────────────────────────────────────────────


def test_claude_code_prompt_payload_normalizes():
    req = normalize(
        "claude-code",
        Event.PROMPT_SUBMIT,
        {"prompt": "why is the statusline blank", "session_id": "abc123"},
    )
    assert req.prompt == "why is the statusline blank"
    assert req.session_id == "abc123"
    assert req.host == "claude-code"


def test_claude_code_tool_payload_normalizes():
    req = normalize(
        "claude-code",
        Event.PRE_TOOL,
        {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    assert req.tool_name == "Bash"
    assert req.tool_input == {"command": "ls"}


@pytest.mark.parametrize("host", ["cursor"])
def test_unported_host_refuses_to_normalize_a_prompt(host):
    """Reading a documented-but-unverified key is how a port fails silently."""
    with pytest.raises(UnverifiedHost) as exc:
        normalize(host, Event.PROMPT_SUBMIT, {"prompt": "hi", "session_id": "s"})
    assert "prompt_key" in exc.value.missing
    assert "Do not fill them in" in str(exc.value)


def test_codex_prompt_is_ported_but_its_tool_events_are_not():
    """Codex's UserPromptSubmit keys were captured from a real run (2026-09-04,
    tests/fixtures/codex_user_prompt_submit.json); its PreToolUse keys were not."""
    import json
    import pathlib
    payload = json.loads((pathlib.Path(__file__).parent / "fixtures" / "codex_user_prompt_submit.json").read_text())
    req = normalize("codex", Event.PROMPT_SUBMIT, payload)
    assert req.prompt == "Reply with the single word: pong"
    assert req.session_id.startswith("01a06e3d")
    with pytest.raises(UnverifiedHost) as exc:
        normalize("codex", Event.PRE_TOOL, {"tool_name": "Bash", "tool_input": {}})
    assert "tool_name_key" in exc.value.missing


def test_unknown_host_is_a_hard_error():
    with pytest.raises(KeyError, match="unknown host"):
        normalize("emacs", Event.PROMPT_SUBMIT, {})


def test_events_only_require_the_fields_they_read():
    """A session hook does not read tool_input, so it must not be gated on it.

    Otherwise an unported host cannot carry even the hooks it could support.
    """
    # gemini-cli has verified prompt/session keys but no tool keys at all.
    req = normalize("gemini-cli", Event.SESSION_END, {"session_id": "s"})
    assert req.session_id == "s"


# ── render ────────────────────────────────────────────────────────────────────


def test_block_renders_in_the_hosts_own_shape():
    out = render("claude-code", HookDecision(block=True, reason="route first"))
    assert out == {"decision": "block", "reason": "route first"}


def test_context_is_nested_where_the_host_expects_it():
    """Claude Code reads hookSpecificOutput.additionalContext, not a flat key."""
    out = render("claude-code", HookDecision(context="⚡ ROUTE: query/simple"))
    assert out == {"hookSpecificOutput": {"additionalContext": "⚡ ROUTE: query/simple"}}


def test_rewrite_is_refused_where_it_cannot_be_honoured():
    """Silently dropping a rewrite runs the original call — worse than failing.

    Codex PreToolUse supports updatedInput; Claude Code's does not.
    """
    decision = HookDecision(updated_input={"command": "ls -la"})
    with pytest.raises(ValueError, match="cannot rewrite tool input"):
        render("claude-code", decision)


def test_rewrite_renders_on_a_host_that_supports_it():
    out = render("codex", HookDecision(updated_input={"command": "ls -la"}))
    assert out["updatedInput"] == {"command": "ls -la"}


def test_block_on_a_host_with_unverified_shape_raises():
    """Cursor can block, but the exact JSON has not been captured."""
    with pytest.raises(UnverifiedHost):
        render("cursor", HookDecision(block=True, reason="route first"))


def test_block_and_context_compose():
    out = render("claude-code", HookDecision(block=True, reason="r", context="c"))
    assert out["decision"] == "block"
    assert out["hookSpecificOutput"]["additionalContext"] == "c"


# ── conformance with the hooks actually shipping ──────────────────────────────


def test_seam_matches_the_live_auto_route_block_shape():
    """The seam is only useful if it renders what the real hook already writes.

    auto-route.py emits its block with a literal json.dump of
    {"decision": "block", "reason": ...}. If the seam disagreed, migrating the
    hook onto it would silently change behaviour on the one host that works.
    """
    import re

    from llm_router import install_hooks as ih

    src = (ih._PACKAGE_DIR / "hooks" / "auto-route.py").read_text()
    assert re.search(r'\{\s*"decision"\s*:\s*"block"\s*,\s*"reason"\s*:', src), (
        "auto-route.py no longer emits the block shape the event map records; "
        "llm_router.hosts.events.CLAUDE_CODE.block_shape is now wrong"
    )

    rendered = render("claude-code", HookDecision(block=True, reason="x"))
    assert set(rendered) == {"decision", "reason"}
    assert rendered["decision"] == "block"


def test_seam_matches_the_live_context_key():
    """auto-route.py normalises context onto `additionalContext` for every host.

    The event map records that same key, so the two must not drift.
    """
    from llm_router import install_hooks as ih

    src = (ih._PACKAGE_DIR / "hooks" / "auto-route.py").read_text()
    assert "additionalContext" in src

    rendered = render("claude-code", HookDecision(context="c"))
    assert "additionalContext" in rendered["hookSpecificOutput"]


def test_live_hook_payload_keys_match_the_map():
    """The keys auto-route.py reads are the keys the map claims it reads."""
    from llm_router import install_hooks as ih
    from llm_router.hosts.events import CLAUDE_CODE

    src = (ih._PACKAGE_DIR / "hooks" / "auto-route.py").read_text()
    for key in (CLAUDE_CODE.prompt_key, CLAUDE_CODE.session_key):
        assert f'"{key}"' in src, (
            f"the map claims Claude Code payloads carry {key!r}, but the live "
            "hook never reads it"
        )
