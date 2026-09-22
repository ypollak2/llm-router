"""Two suite defects of one shape: a test that breaks the tests after it.

Both were found in the same session, both were order-dependent, and both were
first diagnosed as pollution coming from somewhere else. Neither involved the
product at all.

**P-05** — `LogRecord.message` is a formatting side effect, not an attribute.

`logging.LogRecord` has `msg` and `args`. It does NOT have `message`; that name
is assigned by `Formatter.format()` as a side effect, so `record.message` works
only when some handler has already formatted that record.

Under the default handler set it usually has. After
`tests/test_built_artifact_is_complete.py` runs, it has not — and five
assertions across `test_calibration.py` and `security/test_agentic_injection.py`
failed with `AttributeError: 'LogRecord' object has no attribute 'message'`.

The failure was order-dependent, so it looked like the suite was polluting
logging configuration. It was parked as P-05 across two sessions, bisected to
the build test, and confirmed "pre-existing" at 5f90f24 — all of which was
true and none of which was the cause. The product was never involved. The
correct accessor is `getMessage()`, which formats `msg % args` on demand and
depends on nothing.

The lesson worth keeping is not about logging: an order-dependent failure
invites a search for state, and the search can succeed at finding state that is
genuinely shared while the actual defect is a local API misuse in the test.
Bisecting to the neighbour that exposes a bug is not the same as finding it.

**The structlog mock** — `tests/commands/test_routing.py` did
`sys.modules["structlog"] = MagicMock()` at import time and never restored it.
Import-time code runs at COLLECTION, and `sys.modules` is process-global, so
every test collected after that file ran with a MagicMock in place of structlog
for the remainder of the process. `structlog.testing.capture_logs()` returned a
MagicMock instead of a list and `test_exhaustion_floor.py` failed with
`expected exhaustion_floor_returned event, got: <MagicMock ...>`.

It had presumably been latent for a long time; it surfaced when a change to how
the suite is split moved the two files into the same process in the wrong
order. The mock was never needed — structlog is a real dependency of this
suite.

Both lints below are AST-based, so a phrase in this file's own prose cannot
satisfy either of them.
"""

from __future__ import annotations

import ast
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent


def test_no_test_reads_message_off_a_log_record():
    """AST, so the phrase appearing in this file's own docstring cannot satisfy it."""
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr == "message"):
                continue
            base = ast.unparse(node.value)
            # Bare loop/comprehension variables over caplog.records, plus any
            # expression that names caplog or a records list.
            #
            # Matching on a PREFIX was the first attempt and it fired on
            # `r.choices[0].message` — an OpenAI completion, nothing to do with
            # logging. A lint whose first real finding is a false positive
            # teaches people to add allowlist entries, and the allowlist then
            # becomes the blind spot. Exact names only.
            if base in {"r", "rec", "record", "log_record"} or (
                ("caplog" in base or "records" in base)
                and "choices" not in base
            ) or (
                base.startswith(("blown[", "warnings[", "errors["))
            ):
                offenders.append(f"{path.relative_to(TESTS)}:{node.lineno}: {base}.message")
    assert not offenders, (
        "`.message` read off what looks like a logging.LogRecord:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `.getMessage()`. `.message` exists only after a Formatter has "
        "run on that record, which makes the assertion depend on which handlers "
        "the preceding tests happened to leave attached."
    )


def test_the_attribute_really_is_absent(caplog):
    """The check on the check: prove the premise rather than asserting it.

    If a future Python or pytest starts populating `.message` eagerly, this
    fails and the lint above becomes cargo cult that should be deleted.
    """
    import logging

    rec = logging.LogRecord(
        name="probe", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="ratio %s", args=("5.0",), exc_info=None,
    )
    assert not hasattr(rec, "message"), (
        "LogRecord now carries `.message` on construction — this lint no longer "
        "protects anything and should be removed rather than kept as ritual"
    )
    assert rec.getMessage() == "ratio 5.0"


def _module_scope_sys_modules_assignments(tree: ast.Module) -> list[int]:
    """Line numbers of `sys.modules[...] = ...` at MODULE scope.

    Module scope is the distinction that matters. An assignment inside a test
    is at least confined to code the author controls and is usually undone;
    one at module scope runs during COLLECTION, before any test in the file has
    run, and applies to every test in the process from then on. There are 41 of
    the former in this suite and the rule deliberately does not touch them —
    a lint that fires on 42 sites gets an allowlist, and the allowlist becomes
    the blind spot.
    """
    top = {id(n) for n in tree.body}
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or id(node) not in top:
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and ast.unparse(target.value) in {"sys.modules", "modules"}
            ):
                hits.append(node.lineno)
    return hits


def test_the_sys_modules_detector_fires_on_the_real_thing():
    """Anti-vacuity: the rule below currently finds nothing, so prove it can.

    This is the exact code that was in `tests/commands/test_routing.py`.
    """
    src = (
        "import sys\n"
        "from unittest.mock import MagicMock\n"
        "sys.modules['structlog'] = MagicMock()\n"
        "def test_x():\n"
        "    sys.modules['other'] = MagicMock()\n"
    )
    hits = _module_scope_sys_modules_assignments(ast.parse(src))
    assert hits == [3], (
        f"the detector should flag line 3 (module scope) and not line 5 "
        f"(inside a test); it returned {hits}"
    )


def test_no_test_replaces_a_real_module_in_sys_modules():
    """A module swapped into `sys.modules` at import time is never put back."""
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for lineno in _module_scope_sys_modules_assignments(tree):
            offenders.append(f"{path.relative_to(TESTS)}:{lineno}")
    assert not offenders, (
        "test file(s) assigning into sys.modules at MODULE scope:\n  "
        + "\n  ".join(offenders)
        + "\n\nThat runs during collection and is never restored, so it "
        "changes what every LATER test in the process imports. Use "
        "`monkeypatch.setitem(sys.modules, ...)`, which pytest undoes, or do "
        "not mock the module at all."
    )
