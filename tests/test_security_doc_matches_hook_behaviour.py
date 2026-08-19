"""SECURITY.md's hook claims must match what the enforcement hook actually does.

WHY THIS EXISTS
===============

SECURITY.md asserted:

    ✅ Hooks cannot block core tools (Read, Edit, Bash) — would create deadlock

That was false, and reproducibly so. PreToolUse enforcement blocks Bash, Read,
Edit and Write routinely in `smart` and `hard` modes — it is the mechanism those
modes are built on, and v13 removed an older "coding session" exemption
specifically to make blocking stricter, the opposite direction from "cannot
block".

The distinction that matters, and that the doc collapsed:

    cannot block          FALSE — blocking is everyday, intended behaviour
    no permanent lockout  TRUE  — escape valve (any llm_* call) + auto-pivot

In a security document that is the difference between a guarantee and its
opposite. A reader forms the expectation "a hook will never lock me out of Bash"
and that expectation does not hold.

WHAT THIS TEST CAN AND CANNOT DO
================================

It asserts the DOCUMENT, not the hook: that SECURITY.md no longer contains the
false absolute, and that it still states the true narrower guarantee. It cannot
execute the hook, so it does not prove the escape valve works — that belongs in
the hook's own tests.

That limit is the point of writing it this way. A doc claim drifting from
behaviour is a documentation defect with a documentation fix, and the check that
catches it has to read the doc.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SECURITY = Path(__file__).resolve().parents[1] / "SECURITY.md"

#: Phrasings that assert hooks cannot block core tools. Each was, or would be, a
#: false absolute. Matched case-insensitively against the whole document.
_FALSE_ABSOLUTES = (
    "hooks cannot block core tools",
    "cannot block core tools",
    "deadlock detection prevents tool blocking",
)

#: The true, narrower guarantee. At least one must survive, or the correction
#: removed the false claim and left nothing in its place — which would be a
#: different kind of wrong.
_TRUE_GUARANTEES = (
    "no permanent lockout",
    "escape valve",
    "auto-pivot",
)


def _doc() -> str:
    return _SECURITY.read_text(encoding="utf-8").lower()


def test_security_md_exists():
    """Guards the guard: a missing file would make every check below vacuous."""
    assert _SECURITY.is_file(), f"{_SECURITY} not found — the checks below test nothing"


@pytest.mark.parametrize("claim", _FALSE_ABSOLUTES)
def test_no_false_absolute_about_blocking(claim: str):
    assert claim not in _doc(), (
        f"SECURITY.md contains {claim!r}.\n"
        f"PreToolUse enforcement blocks Bash/Read/Edit/Write routinely in smart "
        f"and hard modes — that is the mechanism, not an edge case. The true "
        f"claim is that there is no PERMANENT lockout: any llm_* call releases "
        f"the lock, and repeated attempts auto-pivot.\n"
        f"State what is guaranteed; do not restate the absolute."
    )


def test_the_true_guarantee_is_still_stated():
    """Removing the false claim without replacing it would under-inform instead."""
    doc = _doc()
    assert any(g in doc for g in _TRUE_GUARANTEES), (
        "SECURITY.md no longer claims hooks cannot block core tools — correct — "
        f"but states none of {_TRUE_GUARANTEES}. A reader now learns nothing "
        "about what actually protects them. The narrower guarantee is real and "
        "belongs in the document."
    )


def test_the_document_admits_blocking_happens():
    """The correction has to be affirmative, not merely a deletion."""
    doc = _doc()
    assert "can" in doc and "block" in doc, "sanity check on document contents"
    assert any(
        phrase in doc
        for phrase in ("can** block", "can and do block", "can block core tools")
    ), (
        "SECURITY.md does not state anywhere that hooks CAN block core tools. "
        "Silence reads as the old claim to anyone who remembers it, and as no "
        "claim at all to anyone who does not."
    )
