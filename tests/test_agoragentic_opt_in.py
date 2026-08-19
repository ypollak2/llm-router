"""SEC-003 regression: agoragentic_* MCP tools must be opt-in.

The four ``agoragentic_*`` tools (``agoragentic_task``, ``agoragentic_browse``,
``agoragentic_wallet``, ``agoragentic_status``) talk to an external marketplace
API and ``agoragentic_task`` settles USDC on the Base L2 blockchain — i.e.
it can spend real money. Pre-fix they were registered unconditionally,
even when ``LLM_ROUTER_SLIM=routing`` was set.

After SEC-003, the four tools are gated behind ``LLM_ROUTER_AGORAGENTIC=on``.
Without the opt-in, ``register()`` exposes ZERO ``agoragentic_*`` tools
to MCP clients — eliminating the accidental wallet surface.

See: Docs/audit/HIGH_PRIORITY_WORK_PLAN.md F-SEC-003
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from llm_router.tools import agoragentic


def _fake_mcp() -> MagicMock:
    """Minimal MCP stand-in that records every @mcp.tool() registration."""
    mcp = MagicMock()
    mcp.registered = []

    def tool_factory():
        def decorator(func):
            mcp.registered.append(func.__name__)
            return func
        return decorator

    mcp.tool = tool_factory
    return mcp


def test_agoragentic_tools_not_registered_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-003: LLM_ROUTER_AGORAGENTIC unset → zero agoragentic_* tools registered."""
    monkeypatch.delenv("LLM_ROUTER_AGORAGENTIC", raising=False)
    mcp = _fake_mcp()
    agoragentic.register(mcp)
    assert mcp.registered == [], (
        "Agoragentic tools must NOT be registered without LLM_ROUTER_AGORAGENTIC=on. "
        f"Got: {mcp.registered}"
    )


@pytest.mark.parametrize("value", ["on", "1", "true", "TRUE", "Yes"])
def test_agoragentic_tools_registered_when_opted_in(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """SEC-003: with opt-in, all four agoragentic_* tools register."""
    monkeypatch.setenv("LLM_ROUTER_AGORAGENTIC", value)
    mcp = _fake_mcp()
    agoragentic.register(mcp)
    assert set(mcp.registered) == {
        "agoragentic_task",
        "agoragentic_browse",
        "agoragentic_wallet",
        "agoragentic_status",
    }, f"Expected all four agoragentic_* tools registered; got: {mcp.registered}"


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "", "  "])
def test_agoragentic_falsy_env_values_treated_as_opt_out(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """SEC-003: non-affirmative env values must NOT opt in.

    Including the empty string and whitespace — a malformed shell config
    that sets ``export LLM_ROUTER_AGORAGENTIC=`` should not silently open the
    wallet surface.
    """
    monkeypatch.setenv("LLM_ROUTER_AGORAGENTIC", value)
    mcp = _fake_mcp()
    agoragentic.register(mcp)
    assert mcp.registered == [], (
        f"LLM_ROUTER_AGORAGENTIC={value!r} must be treated as opt-out, "
        f"got registrations: {mcp.registered}"
    )


def test_agoragentic_enabled_helper_matches_expected_truth_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-003: the internal helper that gates the registration is exposed
    for tests so we can assert the truth table directly without booting an
    MCP server."""
    truth_table = {
        "on": True,
        "1": True,
        "true": True,
        "TRUE": True,
        "Yes": True,
        "yes": True,
        "off": False,
        "0": False,
        "false": False,
        "no": False,
        "": False,
        "  ": False,
        "maybe": False,
    }
    for value, expected in truth_table.items():
        monkeypatch.setenv("LLM_ROUTER_AGORAGENTIC", value)
        assert agoragentic._agoragentic_enabled() is expected, (
            f"LLM_ROUTER_AGORAGENTIC={value!r} → expected {expected}, "
            f"got {agoragentic._agoragentic_enabled()!r}"
        )
    monkeypatch.delenv("LLM_ROUTER_AGORAGENTIC", raising=False)
    assert agoragentic._agoragentic_enabled() is False
