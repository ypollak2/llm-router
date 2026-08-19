"""F5 regression: the agent-depth RELEASE hook must be installed.

The PreToolUse[Agent] circuit breaker (agent-route.py) increments a per-session
depth counter before each real subagent spawn. agent-depth-release.py
(PostToolUse[Agent]) decrements it when the subagent finishes. If the release
hook is never REGISTERED by the installer, depth becomes a lifetime total and
after 3 spawns every further Agent call is permanently blocked (observed live).
These tests pin the increment↔release pairing in both host hook-def lists.
"""
from __future__ import annotations

from llm_router.install_hooks import _CLAW_CODE_HOOK_DEFS, _HOOK_DEFS

_RELEASE = "agent-depth-release.py"


def _find(defs, src):
    return [d for d in defs if d[0] == src]


def _pre_agent(defs):
    return [d for d in defs if d[2] == "PreToolUse" and d[3] == "Agent"]


def _post_agent(defs):
    return [d for d in defs if d[2] == "PostToolUse" and d[3] == "Agent"]


def test_release_hook_registered_in_main_defs():
    assert _find(_HOOK_DEFS, _RELEASE), (
        f"{_RELEASE} not in _HOOK_DEFS -> depth counter never decrements (F5)"
    )


def test_release_hook_is_post_tool_use_agent():
    match = _find(_HOOK_DEFS, _RELEASE)
    assert match, f"{_RELEASE} missing from _HOOK_DEFS"
    _src, dst, event, matcher = match[0]
    assert event == "PostToolUse" and matcher == "Agent"
    assert dst == "llm_router-agent-depth-release.py"


def test_increment_and_release_are_paired_main():
    # A PreToolUse[Agent] incrementer with no PostToolUse[Agent] releaser is the
    # exact lifetime-counter bug (F5).
    assert _pre_agent(_HOOK_DEFS), "no PreToolUse[Agent] increment hook"
    assert any(d[0] == _RELEASE for d in _post_agent(_HOOK_DEFS)), (
        "increment without a matching release hook -> F5 lifetime-counter bug"
    )


def test_increment_and_release_are_paired_claw_code():
    # claw-code also uses the agent-route.py incrementer, so it needs the release.
    assert _pre_agent(_CLAW_CODE_HOOK_DEFS), "no PreToolUse[Agent] hook in claw-code defs"
    assert any(d[0] == _RELEASE for d in _post_agent(_CLAW_CODE_HOOK_DEFS)), (
        "claw-code increments depth but never releases it -> F5"
    )
