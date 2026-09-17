"""The grounding benchmark's scorer must not accept a wrong directory.

`scores()` was `q["answer"] in text or q["basename"] in text`, and its docstring
defended the second half deliberately: naming the file is the knowledge being
tested, the directory is a formatting preference the question did not ask for.

That argument is reasonable and the implementation of it is not. `okf.py` is a
substring of `wrong_directory/okf.py`, so an answer that names a directory —
and names the wrong one — scores correct. The lenient rule was meant to forgive
an answer that omits the directory; it also forgives an answer that gets it
wrong, which is the opposite of forgiveness.

Both readings survive here, separately, because they measure different things
and conflating them is what produced a number nobody can interpret:

    strict   the answer names exactly the gold repo-relative path
    lenient  strict, OR the answer names the right file and no directory at all

An answer naming the wrong directory is false under both. It is not an answer
that omitted the directory; it is an answer that asserted one.

Reporting both with their n is the point. `a535f22`'s published "+64.0%" was
measured under the old rule and cannot be compared with a strict figure.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "bench_grounding",
    Path(__file__).resolve().parent.parent / "scripts" / "bench_grounding.py",
)
bench_grounding = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bench_grounding)

GOLD = {"answer": "src/llm_router/okf.py", "basename": "okf.py"}


@pytest.mark.parametrize("answer,strict,lenient,why", [
    ("src/llm_router/okf.py", True, True,
     "the exact gold path"),
    ("The file is `src/llm_router/okf.py`.", True, True,
     "backticks and a full stop are formatting, not a different answer"),
    ("./src/llm_router/okf.py", True, True,
     "a leading ./ is the same path"),
    ("wrong_directory/okf.py", False, False,
     "THE BUG: the old rule scored this correct because okf.py is a substring"),
    ("src/llm_router/cache/okf.py", False, False,
     "a plausible-looking wrong directory is still wrong"),
    ("okf.py", False, True,
     "names the right file and asserts no directory — what lenient is for"),
    ("`okf.py`", False, True,
     "same, quoted"),
    ("src/llm_router/okf.py or src/llm_router/router.py", False, False,
     "two candidates is not an answer; the old rule took the hit and moved on"),
    ("src/llm_router/router.py", False, False,
     "a different real file in the right directory"),
    ("I don't know.", False, False,
     "abstention is not correct, but it is not a wrong directory either"),
    ("", False, False,
     "empty"),
    ("<error: TimeoutError>", False, False,
     "a transport failure must not score as knowledge"),
])
def test_scoring_rules(answer, strict, lenient, why):
    got_strict, got_lenient = bench_grounding.score(answer, GOLD)
    assert got_strict is strict, f"strict: {why} — {answer!r}"
    assert got_lenient is lenient, f"lenient: {why} — {answer!r}"


def test_strict_implies_lenient():
    """Lenient is a superset. A rule where they cross is a reporting trap."""
    for answer in ("src/llm_router/okf.py", "okf.py", "wrong/okf.py", "",
                   "src/a.py src/b.py", "The answer is src/llm_router/okf.py"):
        strict, lenient = bench_grounding.score(answer, GOLD)
        assert not (strict and not lenient), answer


def test_durations_use_a_monotonic_clock():
    """macOS Maintenance Sleep advances time.time() and not time.monotonic().

    One benchmark task in this repo was recorded at 918.6s of which 902s was the
    laptop asleep. A wall-clock duration here is not a measurement.
    """
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "bench_grounding.py").read_text(encoding="utf-8")
    offenders = [
        f"line {i}" for i, ln in enumerate(src.splitlines(), 1)
        if "time.time()" in ln and not ln.lstrip().startswith("#")
    ]
    assert not offenders, (
        "bench_grounding times a model call with the wall clock: " + ", ".join(offenders)
    )
