"""Regression: RED1-5-03 — explicit LLM_ROUTER_RENDER_MODE=echo must never be
force-blocked, even when LLM_ROUTER_ZERO_CLAUDE is also set.

`_resolve_auto_render_mode(render_mode, zero_claude)` already applies the
zero_claude gate (auto → block only in zero-Claude; explicit echo/block honored
unchanged). The call site then derived turn-blocking as
`not (mode == "echo" and not zero_claude)`, which re-applied zero_claude on top
of the resolved mode and force-blocked an operator's explicit `echo` whenever
zero_claude was on — bypassing Claude with an unverified draft in exactly the
advisory-only config they opted into. Blocking must derive purely from the
resolved mode: `_turn_blocked = mode != "echo"`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"


def _load():
    spec = importlib.util.spec_from_file_location("llm_router_auto_route_red1503", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _turn_blocked(mod, render_mode: str, zero_claude: bool) -> bool:
    """Mirror the production derivation at the call site (post-fix)."""
    resolved = mod._resolve_auto_render_mode(render_mode, zero_claude)
    return resolved != "echo"


@pytest.mark.parametrize(
    "render_mode,zero_claude,expected_blocked",
    [
        ("auto", False, False),   # advisory echo when Claude present
        ("auto", True, True),     # zero-Claude → block (the CHZ-DRAFT-01 case)
        ("echo", False, False),   # explicit echo honored
        ("echo", True, False),    # RED1-5-03: explicit echo honored EVEN in zero-Claude
        ("block", False, True),   # explicit block honored
        ("block", True, True),    # explicit block honored
    ],
)
def test_turn_blocked_matrix(render_mode, zero_claude, expected_blocked):
    mod = _load()
    assert _turn_blocked(mod, render_mode, zero_claude) is expected_blocked


def test_explicit_echo_not_blocked_under_zero_claude():
    """The specific RED1-5-03 cell: the operator wants advisory-only display AND
    is protecting Claude quota — the routed draft must NOT replace the turn."""
    mod = _load()
    assert mod._resolve_auto_render_mode("echo", True) == "echo"
    assert _turn_blocked(mod, "echo", True) is False, (
        "RED1-5-03: explicit LLM_ROUTER_RENDER_MODE=echo force-blocked under zero_claude"
    )


@pytest.mark.parametrize("bad", ["eco", "", "  ", "off", "warn", "disabled", "ECHOO", "blck"])
def test_unrecognized_render_mode_fails_safe_to_echo(bad):
    """RED1-6-02: any unrecognized LLM_ROUTER_RENDER_MODE must fail SAFE to echo
    (advisory), never escalate to turn-blocking (which would replace the user's
    turn with an unverified draft on a config typo)."""
    mod = _load()
    assert mod._resolve_auto_render_mode(bad, False) == "echo"
    assert mod._resolve_auto_render_mode(bad, True) == "echo"
    assert _turn_blocked(mod, bad, False) is False, f"{bad!r} escalated to block"
    assert _turn_blocked(mod, bad, True) is False, f"{bad!r} escalated to block under zero_claude"


def test_case_and_whitespace_normalized():
    mod = _load()
    assert mod._resolve_auto_render_mode(" ECHO ", False) == "echo"
    assert mod._resolve_auto_render_mode("Block", False) == "block"
    assert mod._resolve_auto_render_mode(" AUTO ", True) == "block"
