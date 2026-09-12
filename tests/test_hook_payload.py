"""One payload reader, because guessing the spelling fails silently.

Claude Code sends a PostToolUse payload as snake_case (`tool_name`,
`tool_input`, `tool_response`); some other hosts send camelCase (`toolName`,
`toolInputs`, `toolResult`). The hooks in this tree had split down the middle:
context-capture / library-harvest / cc-usage-track read snake_case and worked,
while bash-compress and playwright-compress read camelCase and so never fired
on Claude Code at all. `compression_stats` had no rows and no table because
nothing had ever written one.

A hook that reads the wrong key exits 0 and prints nothing, which is
indistinguishable from "there was nothing to compress" — so nothing surfaced it
for as long as it shipped.
"""
from __future__ import annotations

import pytest

from llm_router.hooks.hook_payload import (
    is_tool, tool_input, tool_name, tool_output,
)

CLAUDE_CODE = {
    "session_id": "abc", "cwd": "/tmp", "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "git status"},
    "tool_response": {"stdout": "a\nb", "stderr": "", "interrupted": False},
}
CAMEL_HOST = {
    "toolName": "mcp__plugin_x__execute_shell_command",
    "toolInputs": '{"command": "ls"}',
    "toolResult": {"content": [{"type": "text", "text": "z"}]},
}


def test_claude_code_is_read():
    assert is_tool(CLAUDE_CODE, "bash", "execute_shell_command")
    assert tool_input(CLAUDE_CODE)["command"] == "git status"
    assert tool_output(CLAUDE_CODE) == "a\nb"


def test_the_camelcase_host_is_still_read():
    """The old spelling must keep working — some host was presumably sending it."""
    assert is_tool(CAMEL_HOST, "bash", "execute_shell_command")
    assert tool_input(CAMEL_HOST)["command"] == "ls"
    assert tool_output(CAMEL_HOST) == "z"


def test_bash_matches_case_insensitively():
    """`"Bash".endswith("bash")` is False. That one capital disabled the hook."""
    assert is_tool({"tool_name": "Bash"}, "bash")
    assert is_tool({"tool_name": "bash"}, "bash")
    assert is_tool({"tool_name": "BASH"}, "bash")
    assert not is_tool({"tool_name": "Read"}, "bash")


def test_the_mcp_prefix_is_stripped():
    assert tool_name({"tool_name": "mcp__plugin_playwright__browser_snapshot"}) \
        == "browser_snapshot"


def test_a_substring_is_not_a_match():
    """Matching is equality, not endswith — `rebash` is not the shell tool."""
    assert not is_tool({"tool_name": "rebash"}, "bash")


def test_stderr_is_appended_because_a_failure_is_the_interesting_part():
    out = tool_output({"tool_response": {"stdout": "ok", "stderr": "boom"}})
    assert "ok" in out and "boom" in out


def test_stderr_alone_is_still_output():
    assert "boom" in tool_output({"tool_response": {"stdout": "", "stderr": "boom"}})


def test_stdout_wins_over_text_when_both_are_present():
    """Claude Code puts the real bytes in stdout; `text` may hold a rendering."""
    assert tool_output({"tool_response": {"stdout": "real", "text": "rendered"}}) == "real"


def test_a_bare_string_result_is_the_output():
    assert tool_output({"tool_result": "plain"}) == "plain"


@pytest.mark.parametrize("bad", [
    {}, {"tool_name": None}, {"tool_input": "not json"},
    {"tool_response": 42}, {"tool_response": {"content": "notalist"}},
])
def test_garbage_never_raises(bad):
    """These run inside a PostToolUse hook: an exception is a visible error on
    every single tool call."""
    tool_name(bad)
    is_tool(bad, "bash")
    tool_input(bad)
    tool_output(bad)


def test_a_json_string_input_is_parsed_and_a_broken_one_is_empty():
    assert tool_input({"tool_input": '{"command": "x"}'}) == {"command": "x"}
    assert tool_input({"tool_input": "{not json"}) == {}
