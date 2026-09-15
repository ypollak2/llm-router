"""A continuation prompt must reach the context-rescue ladder, not bypass it.

Regression for 2026-09-14. `method in ("context-inherit", "code-context-inherit")`
set `_direct_enabled = False` outright, on a v2.6.1 premise that "the direct hook
is stateless". By 13.3 the hook relayed conversation history, built session
context and retrieved OKF documents — but all three rescues sit behind
`if _direct_enabled`, so a continuation like "keep going into W3" never reached
the machinery built to resolve exactly that kind of prompt. It was the single
largest reason no draft was produced: 7 of 25 real prompts.

The fix must not become "wave continuations through". A continuation IS
context-dependent; it now enters the same gate as any other context-dependent
prompt, and when nothing resolves the reference that gate still closes.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "src/llm_router/hooks/auto-route.py"
SOURCE = HOOK.read_text()


def test_continuation_no_longer_hard_disables_direct_execution():
    stanza = re.search(
        r"_inherits_context = method in \([^)]*\)\n    if _inherits_context and not zero_claude:\n(.*?)\n\n",
        SOURCE, re.S,
    )
    assert stanza, "the context-inherit branch is not in the expected shape"
    assert "_direct_enabled = False" not in stanza.group(1), (
        "a continuation is hard-disabled again — the OKF / session / tool-loop "
        "rescues below sit behind `if _direct_enabled` and will never run"
    )


def test_continuation_enters_the_context_dependent_gate():
    gate = re.search(
        r"if _direct_enabled and not zero_claude and \(\n\s*_is_context_dependent\(prompt\) or _inherits_context\n\s*\):",
        SOURCE,
    )
    assert gate, (
        "continuations no longer enter the context-dependent gate; they would be "
        "answered blind, which is the fabrication this gate exists to prevent"
    )


def test_the_gate_still_closes_when_nothing_resolves_the_reference():
    tail = SOURCE.split("TOOL LOOP RESCUE", 1)[1]
    closing = tail.split("else:", 1)[1][:400]
    assert "_direct_enabled = False" in closing, (
        "the rescue ladder no longer has a closing branch — an unresolvable "
        "continuation would be drafted blind"
    )


@pytest.mark.parametrize("arm", ["OKF RESCUE", "SESSION RESCUE", "TOOL LOOP RESCUE"])
def test_all_three_rescue_arms_are_reachable_for_continuations(arm):
    gate_at = SOURCE.index("_is_context_dependent(prompt) or _inherits_context")
    assert SOURCE.index(arm) > gate_at, f"{arm} sits before the gate that admits continuations"
