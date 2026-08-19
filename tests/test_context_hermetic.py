"""#21 — every test that builds context must isolate the live ~/.llm-router.

`tests/test_context.py` defines a `reset_session_buffer` fixture that does two
things: resets the global session buffer, and repoints HOME at a temp dir. Its own
docstring states the hazard:

    "Without isolating HOME the test read the developer's LIVE session
     accumulator (populated by the active llm_router hooks) and non-deterministically
     saw context where the test expects none."

The fixture is a plain ``@pytest.fixture`` — NOT autouse — so each test has to
request it. Three members of ``TestBuildContextMessages`` did;
``test_combined_context_order`` and ``test_respects_token_budget`` did not.

WHAT THAT COST
--------------
``test_combined_context_order`` failed one full-suite run
(``content.index("Additional context")`` → ValueError) and passed the next, on
the same tree, with a clean-HEAD control passing in between. Resolving it as
flaky rather than a real regression took three suite runs, a control worktree and
two instrumented probes. Measured leak: the live HOME yields **2854** characters
of context where an isolated HOME yields **159** — 18x.

A false red is not free. Had it been trusted, it would have sent someone hunting
a regression in the calibration change that did not exist.

WHY A SOURCE-LEVEL TEST
-----------------------
Making the fixture ``autouse=True`` would fix today's two tests silently and
leave the next author free to add a third non-hermetic one in a different class.
This asserts the property that actually matters — every test in that class opts
into isolation — so the NEXT one fails here instead of failing someone's suite at
random.

The repo already has G4, a "test-hygiene ratchet (no new can't-fail tests)". It
catches tests that cannot fail. It does not catch tests that can fail
*spuriously*, which is the other half of the same problem.
"""

from __future__ import annotations

import ast
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "test_context.py"

#: The class whose members read the durable accumulator via
#: build_context_messages() and therefore need HOME isolated.
CLASS_NAME = "TestBuildContextMessages"
FIXTURE = "reset_session_buffer"


def _class_node(tree: ast.Module) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == CLASS_NAME:
            return node
    raise AssertionError(
        f"{CLASS_NAME} not found in {TARGET.name} — this guard has silently "
        "stopped guarding anything (renamed class?)"
    )


def _tests_in_class(cls: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name.startswith("test_")
    ]


def test_the_guard_finds_the_class_and_its_tests():
    """Guards the guard. If the parse returns nothing, every assertion below
    passes vacuously — the failure mode that let a broken probe report
    '0/6 reproductions' while measuring nothing."""
    tests = _tests_in_class(_class_node(ast.parse(TARGET.read_text())))
    assert len(tests) >= 5, f"only found {len(tests)} tests in {CLASS_NAME}"


def test_every_context_building_test_isolates_home():
    """The criterion: each test in the class requests the isolation fixture."""
    tests = _tests_in_class(_class_node(ast.parse(TARGET.read_text())))
    missing = [
        t.name for t in tests
        if FIXTURE not in {a.arg for a in t.args.args}
    ]
    assert not missing, (
        f"these tests read the LIVE ~/.llm-router accumulator because they do not "
        f"request `{FIXTURE}`, so they pass or fail depending on machine state:\n"
        + "\n".join(f"  {CLASS_NAME}::{n}" for n in missing)
    )


def test_the_fixture_still_isolates_home():
    """The fixture's VALUE is the HOME repoint, not its name. If someone drops
    the monkeypatch and keeps the buffer reset, every test above still 'requests
    the fixture' while the hazard returns — a guard passing on a name rather
    than on behaviour."""
    src = TARGET.read_text()
    start = src.index(f"def {FIXTURE}")
    body = src[start:start + 1200]
    assert 'setenv("HOME"' in body, (
        f"{FIXTURE} no longer repoints HOME; the tests requesting it are no "
        "longer hermetic even though this file's other assertions still pass"
    )
