"""New tests may not assert on source TEXT — R13.

On 2026-09-22 the audit defeated this file's predecessors: several tests whose
docstrings claimed to "assert the CALL SITE, not the definition" instead
checked whether a string appeared anywhere in a 5,300-line module. Breaking the
real call site while leaving the phrase in a comment let **23 tests pass** on
code where the bandit was again rewarded for answers the router had rejected.

The earlier red-check reverted whole files. That removes the string too, so the
test went red and looked sound. **A whole-file revert cannot distinguish a
call-site assertion from a substring scan** — which is why this repo's
remediation rule now requires the NARROWEST mutation that reintroduces the bug.

This test bounds the population. `tests/_ast_assert.py` is the replacement:
comments and strings are not in the AST, so prose cannot satisfy an assertion.

It is a RATCHET, not a ban. 64 such assertions existed when it was written;
many are genuinely structural (e.g. "this migration is in the migration list")
and have no behavioural equivalent. Lower the number; do not raise it.
"""

from __future__ import annotations

import pathlib

TESTS = pathlib.Path(__file__).resolve().parent

#: Count at the time of writing. LOWER THIS. Raising it needs a reason in the
#: commit message.
MAX_SOURCE_TEXT_ASSERTIONS = 62


def _source_text_assertions() -> list[str]:
    """`assert ... in <source-ish>` lines across the test tree."""
    hits: list[str] = []
    for f in sorted(TESTS.rglob("test_*.py")):
        text = f.read_text(encoding="utf-8")
        if "getsource" not in text:
            continue
        for i, line in enumerate(text.split("\n"), 1):
            s = line.strip()
            if not s.startswith("assert"):
                continue
            if any(tok in s for tok in (" in src", " in code", " in source",
                                        "in inspect.getsource")):
                hits.append(f"{f.relative_to(TESTS)}:{i}")
    return hits


def test_source_text_assertions_have_not_multiplied():
    found = _source_text_assertions()
    assert len(found) <= MAX_SOURCE_TEXT_ASSERTIONS, (
        f"{len(found)} source-text assertions, up from "
        f"{MAX_SOURCE_TEXT_ASSERTIONS}.\n\n"
        "A substring check over a module is satisfied by a comment. Use\n"
        "tests/_ast_assert.py (assert_calls / assert_guarded_by /\n"
        "assert_passes_kwarg) or write a behavioural test.\n\n"
        f"newest: {found[-6:]}"
    )


def test_the_scan_still_finds_the_population():
    """Anti-vacuity: a scan that matches nothing would let the count drop to 0
    and then permit anything."""
    found = _source_text_assertions()
    assert len(found) > 10, (
        f"the scan found only {len(found)} — it has stopped matching the test "
        "tree and the ratchet is no longer protecting anything"
    )


def test_the_ast_helper_rejects_a_commented_out_call(tmp_path):
    """The property that makes the replacement worth using.

    This is the exact evasion that defeated the old assertions: the phrase
    survives as a comment while the executing code does the opposite.

    Written to a real module because `inspect.getsource` cannot read an
    `exec`-defined function — and a helper that silently no-ops on
    unreadable source would be worse than the substring check it replaces.
    """
    import importlib.util
    import sys

    import pytest

    sys.path.insert(0, str(TESTS))
    from _ast_assert import assert_guarded_by

    mod_path = tmp_path / "fixture_mod.py"
    mod_path.write_text(
        "def broken(response):\n"
        "    # False if getattr(response, 'quality_degraded', False)\n"
        "    return True\n"
        "\n"
        "def fixed(response):\n"
        "    return False if getattr(response, 'quality_degraded', False) else True\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("fixture_mod", mod_path)
    m = importlib.util.module_from_spec(spec)
    sys.modules["fixture_mod"] = m
    spec.loader.exec_module(m)

    # the evasion must NOT satisfy it
    with pytest.raises(AssertionError):
        assert_guarded_by(m.broken, "quality_degraded")

    # the real thing must satisfy it
    assert_guarded_by(m.fixed, "quality_degraded")

    del sys.modules["fixture_mod"]
