"""SECURITY.md must describe that enforcement hooks block core tools.

WHY THIS EXISTS
===============

This document said nothing about hook blocking, while `PreToolUse` enforcement
blocks `Bash`/`Read`/`Edit`/`Write` routinely in `smart` and `hard` modes. Not an
edge case — it is the mechanism those modes are built on.

Silence is a milder failure than the downstream copy's, which asserted the
opposite with a ✅ ("Hooks cannot block core tools — would create deadlock"), but
it is still a failure: a user learns the behaviour by hitting it, from a document
whose job is to set expectations.

The distinction the text must preserve:

    hooks cannot block core tools   FALSE — everyday, intended behaviour
    hooks cannot lock you out       TRUE  — escape valve + auto-pivot

These sound alike and are not. This test asserts BOTH halves: that the behaviour
is disclosed, and that the guarantee is stated — because disclosing the blocking
without the releases would read as "you may get stuck", which is equally untrue
and considerably more alarming.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SECURITY = Path(__file__).resolve().parents[1] / "SECURITY.md"


def _doc() -> str:
    return _SECURITY.read_text(encoding="utf-8").lower()


def test_security_md_exists():
    """Guards the guard: a missing file makes every check below vacuous."""
    assert _SECURITY.is_file(), f"{_SECURITY} not found — checks below test nothing"


def test_blocking_is_disclosed():
    doc = _doc()
    assert "block" in doc and any(
        p in doc for p in ("can block", "can** block", "blocks core tools")
    ), (
        "SECURITY.md does not disclose that enforcement hooks block core tools. "
        "Users meet this behaviour in normal use; a security document that omits "
        "it leaves them to discover it by being blocked."
    )


@pytest.mark.parametrize(
    "release", ["no permanent lockout", "auto-pivot", "llm_"]
)
def test_the_escape_routes_are_stated(release: str):
    """Disclosing the block without the releases is its own kind of wrong."""
    assert release in _doc(), (
        f"SECURITY.md discloses that hooks block core tools but does not mention "
        f"{release!r}. Without the escape routes the text reads as 'you may get "
        f"stuck', which is untrue and more alarming than the omission it replaced."
    )


def test_the_false_absolute_is_not_reintroduced():
    """The downstream copy of this document asserted it with a ✅. Guard against
    it arriving here in a future sync."""
    # Matches the claim ASSERTED, not the claim QUOTED. The first version of
    # this test searched the whole document and tripped on this file's own
    # explanation of why the claim is false — the second time in one session that
    # a textual check confused a mention for a use (see the SEC-002 layer-2 test
    # in the downstream repo). Assertions here are checkmarked bullets; prose
    # discussing them is not.
    asserted = [
        line for line in _doc().splitlines()
        if "cannot block core tools" in line and line.lstrip().startswith(("- ✅", "* ✅", "- [x]"))
    ]
    assert not asserted, (
        "SECURITY.md now claims hooks CANNOT block core tools. That is false — "
        "enforcement blocks Bash/Read/Edit/Write by design. The true, narrower "
        "claim is that there is no permanent lockout."
    )
