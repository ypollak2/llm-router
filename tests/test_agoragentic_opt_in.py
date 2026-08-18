"""The marketplace/wallet tools must not register unless the operator opts in.

WHY THIS EXISTS (SEC-003)
=========================

Four tools — ``agoragentic_task``/``_browse``/``_wallet``/``_status`` — were
registered unconditionally at server startup, alongside every core routing tool.
They differ from every other destination in this package on both axes that
matter:

  * every other routing target is a KNOWN, NAMED vendor the operator configured
    themselves (pulled a model, added a key). These match a task to a
    dynamically-selected, unvetted counterparty at request time;
  * every other target costs either nothing (local) or the operator's own
    metered account. These settle real USDC on Base L2 automatically.

That is task outsourcing to a paid third party, not model routing, and it was
arriving switched on for every install.

THE FINDING IS THE GAP, NOT THE FIX
===================================

Chuzom already had this gate — ``src/chuzom/tools/agoragentic.py``, SEC-003, off
unless ``CHUZOM_AGORAGENTIC=on``, with the reasoning written out. The mitigation
existed, was correct, and never reached this repository.

That makes it the pattern from Chuzom's doc 32 §5 one level up: not "a helper
exists and a sibling call site skipped it", but "a mitigation exists and the
DOWNSTREAM REPOSITORY never received it". Anywhere two repositories share a
lineage, a security fix landing in one is not a security fix landing in both,
and nothing in either repo's CI notices.

So the durable question after this test passes is not "is agoragentic gated"
but "which OTHER SEC-* mitigations are unported".
"""

from __future__ import annotations

import pytest

from llm_router.tools import agoragentic


class _RecordingMCP:
    """Minimal stand-in that records what `register()` would expose."""

    def __init__(self) -> None:
        self.registered: list[str] = []

    def tool(self, *_args, **_kwargs):
        def _decorator(fn):
            self.registered.append(fn.__name__)
            return fn

        return _decorator


def _register_with(monkeypatch, value: str | None) -> list[str]:
    if value is None:
        monkeypatch.delenv(agoragentic._AGORAGENTIC_ENV, raising=False)
    else:
        monkeypatch.setenv(agoragentic._AGORAGENTIC_ENV, value)
    mcp = _RecordingMCP()
    agoragentic.register(mcp)
    return mcp.registered


def test_unset_environment_registers_nothing(monkeypatch):
    """The default install must carry no wallet/marketplace surface at all."""
    assert _register_with(monkeypatch, None) == [], (
        "agoragentic tools registered without an opt-in — every install would "
        "expose USDC settlement and dispatch to unvetted providers"
    )


@pytest.mark.parametrize("value", ["", "0", "off", "false", "no", "maybe", "ON_BUT_NOT_REALLY"])
def test_non_affirmative_values_register_nothing(monkeypatch, value: str):
    """Anything that is not an explicit yes is a no.

    Parametrised over the near-misses rather than one negative case, because a
    gate that accepts `"off"` as truthy is the classic way this kind of check
    fails open.
    """
    assert _register_with(monkeypatch, value) == [], (
        f"{value!r} was treated as an opt-in — the gate must fail closed"
    )


@pytest.mark.parametrize("value", ["1", "on", "true", "yes", "ON", "True", " yes "])
def test_affirmative_values_do_register(monkeypatch, value: str):
    """The opt-in must actually work, or this is a removal rather than a gate.

    Without this, `register()` could unconditionally `return` and every test
    above would still pass — the feature would be silently dead instead of
    gated.
    """
    registered = _register_with(monkeypatch, value)
    assert registered, f"{value!r} should enable the tools but registered none"
    assert any(name.startswith("agoragentic_") for name in registered)


def test_all_four_tools_are_behind_the_same_gate(monkeypatch):
    """A partial gate is the shape this class of defect leaves behind."""
    registered = _register_with(monkeypatch, "on")
    expected = {
        "agoragentic_task",
        "agoragentic_browse",
        "agoragentic_wallet",
        "agoragentic_status",
    }
    missing = expected - set(registered)
    assert not missing, f"tools not registered under the opt-in: {sorted(missing)}"
