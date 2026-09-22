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

import re

import pathlib

TESTS = pathlib.Path(__file__).resolve().parent

#: Count at the time of writing. LOWER THIS. Raising it needs a reason in the
#: commit message.
#: 62 when R13 started; 1 after the conversion pass. LOWER THIS, never raise
#: it — the one that remains is deliberate and is named in
#: `REMAINING_BY_DESIGN` below.
MAX_SOURCE_TEXT_ASSERTIONS = 1


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
            # Bare identifiers only. `" in src"` also matched prompt strings
            # like "Make the filter case-insensitive in src/a.py", so the
            # ratchet was counting fixtures as violations — a detector that
            # over-counts invites raising the ceiling for the wrong reason.
            if re.search(r"\bin\s+(src|code|source)\b(?!/)", s) or \
                    "in inspect.getsource" in s:
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


def test_the_detector_fires_on_a_known_positive(tmp_path):
    """Anti-vacuity, done the right way round.

    This used to assert the repo still CONTAINED more than ten source-text
    assertions — so the ratchet could not be vacuous. It was a reasonable
    check when the population was 62 and a wrong one the moment the class was
    nearly closed: it required the codebase to keep the disease in order to
    prove the thermometer worked. At 2 remaining, success broke the test.

    The detector is now proved against a SYNTHETIC positive instead. The
    repo's population is free to reach zero, which is the goal.
    """
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import inspect\n"
        "def test_x():\n"
        "    src = inspect.getsource(object)\n"
        '    assert "foo" in src\n'
        '    assert "bar" in inspect.getsource(object)\n'
        "def test_not_a_violation():\n"
        '    assert "fix the bug in src/a.py" == "fix the bug in src/a.py"\n'
    )
    hits = []
    for i, line in enumerate(probe.read_text().split("\n"), 1):
        t = line.strip()
        if not t.startswith("assert"):
            continue
        if re.search(r"\bin\s+(src|code|source)\b(?!/)", t) or \
                "in inspect.getsource" in t:
            hits.append(i)
    assert hits == [4, 5], (
        f"the detector matched lines {hits}; it must flag the two real "
        "source-text assertions and NOT the prompt string containing "
        "'in src/a.py'"
    )


#: The assertions deliberately left as text, each with the reason. A row here
#: is a decision, not a backlog item.
REMAINING_BY_DESIGN = {
    "test_s03_route_server_auth_parity.py": (
        "checks a specific DOCSTRING sentence is gone. `_ast_assert."
        "string_constants` excludes docstrings by design — routing this "
        "through the helper would make it pass whether the stale sentence "
        "survived or not, which is strictly weaker than the text check."
    ),
    # REMOVED: telemetry/test_m02_benchmarks_declare_themselves.py.
    #
    # I excused it here with the reason "the marker is a comment by
    # construction, so there is no AST node to assert on". That was WRONG —
    # the declaration is a live `os.environ.setdefault("LLM_ROUTER_SYNTHETIC",
    # "1")` call, which is exactly the kind of thing an AST assertion is for,
    # and it has since been converted (and red-checked three ways, including
    # the case where the call is changed to a hard assignment rather than a
    # default).
    #
    # Kept as a comment rather than deleted: an excuse written from a guess
    # rather than from reading the file is the same defect as a source-text
    # assertion — it looks like a decision and is not one.
}


def test_every_remaining_source_text_assertion_is_deliberate():
    """The two survivors are named, or the count is wrong somewhere."""
    found = _source_text_assertions()
    unexplained = [
        h for h in found
        if h.rsplit(":", 1)[0] not in REMAINING_BY_DESIGN
    ]
    assert not unexplained, (
        f"source-text assertion(s) with no recorded reason: {unexplained}\n\n"
        "Convert it, or add its file to REMAINING_BY_DESIGN with why an AST "
        "assertion would be weaker."
    )
    for f, reason in REMAINING_BY_DESIGN.items():
        assert len(reason) > 60, f"{f}: the reason does not explain itself"


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
