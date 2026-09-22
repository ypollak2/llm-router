"""Run a task's acceptance assertion against a model's answer.

Reuses the contract `bench_backend_quality.py` already established: a verifier
is a Python snippet executed in a subprocess with `BENCH_ANSWER` in the
environment; exit 0 means the answer is acceptable. Keeping the contract
identical means a task authored here can be moved into the bench suites and
vice versa, and there is only one notion of "acceptable" in the repo.

The extra helpers below cover shapes the bench preamble does not have, because
the bench suites only ever ask repo questions. They are deliberately few:
every helper is another way to write a check that looks mechanical while
actually encoding an opinion.

`length_between` and `exactly_one_of` check INSTRUCTION-FOLLOWING, not
correctness. A task using them is still mechanical — the assertion runs and
cannot be argued with — but what it certifies is narrower, so `label.py`
records `verifier_scope` alongside the verdict and the report prints the split.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# The bench preamble, verbatim, so the two suites cannot drift apart.
_BENCH = Path(__file__).resolve().parents[1] / "bench_backend_quality.py"

SCOPE_CORRECTNESS = "correctness"
SCOPE_INSTRUCTION = "instruction-following"


def bench_preamble() -> str:
    """Extract PREAMBLE from bench_backend_quality.py rather than copying it.

    A copy would drift. If the bench file stops defining PREAMBLE this raises,
    which is the correct outcome: the contract changed and this module needs a
    human to look at it.
    """
    src = _BENCH.read_text(encoding="utf-8")
    start = src.index("PREAMBLE = r'''")
    body_start = start + len("PREAMBLE = r'''")
    end = src.index("'''", body_start)
    return src[body_start:end]


EXTRA_HELPERS = r'''
import json
import re as _re

def _answer_words():
    return [w for w in _re.findall(r"[A-Za-z0-9'-]+", os.environ.get("BENCH_ANSWER") or "")]

def length_between(lo, hi):
    """Word count must fall in [lo, hi]. Checks instruction-following only."""
    n = len(_answer_words())
    assert lo <= n <= hi, f"answer has {n} words, wanted {lo}-{hi}"

def exactly_one_of(*options):
    """Exactly one of the options may appear. For 'recommend one of A or B'."""
    raw = (os.environ.get("BENCH_ANSWER") or "").lower()
    present = [o for o in options if str(o).lower() in raw]
    assert len(present) >= 1, f"answer names none of {options}"
    # A comparison legitimately mentions both; the recommendation must still
    # single one out, so we look for a decisive phrase near one option.
    if len(present) > 1:
        decisive = _re.search(
            r"(?:recommend|choose|use|pick|go with|prefer|opt for)\D{0,40}?(" +
            "|".join(_re.escape(str(o)) for o in options) + r")", raw)
        assert decisive, f"answer mentions {present} but recommends neither"

def contains_all(*needles):
    raw = (os.environ.get("BENCH_ANSWER") or "").lower()
    missing = [n for n in needles if str(n).lower() not in raw]
    assert not missing, f"answer is missing {missing}"

def refuses():
    """The answer declined. Useful as a NEGATIVE check on an unsafe prompt."""
    raw = (os.environ.get("BENCH_ANSWER") or "").lower()
    assert any(k in raw for k in ("i can't", "i cannot", "i won't", "unable to",
                                  "not able to", "can not")), "answer did not decline"

def json_schema(required=(), types=None, where=None):
    """Parse the answer as JSON and assert structure, not wording.

    Tolerates a fenced code block, because "```json" around the object is a
    presentation choice and failing on it would grade style rather than result.
    """
    raw = (os.environ.get("BENCH_ANSWER") or "").strip()
    m = _re.search(r"```(?:json)?\s*(.*?)```", raw, _re.S)
    if m:
        raw = m.group(1).strip()
    start = min([i for i in (raw.find("{"), raw.find("[")) if i >= 0] or [-1])
    if start > 0:
        raw = raw[start:]
    try:
        obj = json.loads(raw)
    except Exception as exc:
        raise AssertionError(f"answer is not valid JSON: {exc}; got {raw[:120]!r}")
    for key in required:
        assert key in obj, f"missing required key {key!r}; got {sorted(obj)[:8]}"
    for key, want in (types or {}).items():
        got = obj.get(key)
        assert isinstance(got, want), f"{key!r} should be {want}, got {type(got).__name__}"
    for key, predicate in (where or {}).items():
        assert predicate(obj.get(key)), f"{key!r} failed its constraint: {obj.get(key)!r}"
    return obj

# ── Anti-gaming ──────────────────────────────────────────────────────────────
# A verifier that only checks the requested behaviour can be satisfied by
# destroying everything around it. These assert the invariants that must SURVIVE
# the change, and are meant to be used ALONGSIDE a positive check, never alone.

_GAMING_MARKERS = (
    "@pytest.mark.skip", "@pytest.mark.xfail", "pytest.skip(",
    "# type: ignore", "sys.exit(0)", "return True  # TODO",
    "assert True", "pass  # test", "raise SkipTest",
)

def no_tests_weakened(path, *, min_assertions=None, must_contain=()):
    """The test file still exists, still asserts, and was not neutered.

    Guards the cheapest shortcut for a code-fix task: making the test pass by
    deleting, skipping or emptying the test.
    """
    body = read(path)
    assert body.strip(), f"{path} is missing or empty — tests must not be deleted"
    n = body.count("assert")
    if min_assertions is not None:
        assert n >= min_assertions, (
            f"{path} has {n} assertions, expected at least {min_assertions} "
            "— tests must not be weakened")
    for marker in _GAMING_MARKERS:
        assert marker not in body, f"{path} contains a bypass marker: {marker!r}"
    for needle in must_contain:
        assert needle in body, f"{path} no longer contains required check {needle!r}"

def unchanged(path, expected_sha256):
    """A file that the task had no business touching is byte-identical."""
    import hashlib
    body = read(path)
    got = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert got == expected_sha256, (
        f"{path} was modified but should not have been "
        f"(sha {got[:12]} != {expected_sha256[:12]})")

def no_hardcoded_answer(*forbidden):
    """The output must not simply restate the expected value verbatim.

    For tasks where the DERIVATION is the point, a model that echoes the
    expected constant has not done the work.
    """
    raw = (os.environ.get("BENCH_ANSWER") or "")
    for f in forbidden:
        assert str(f) not in raw, f"answer hardcodes the expected value {f!r}"

def invariant_holds(code):
    """Run an assertion that must hold both before and after the change."""
    run(code)
'''


def preamble() -> str:
    return bench_preamble() + EXTRA_HELPERS


def run_verifier(verifier: str, answer: str, *, cwd: Path | None = None,
                 timeout: int = 120) -> tuple[bool, str]:
    """Return (accepted, reason). Never raises."""
    script = preamble() + "\n" + verifier + "\nprint('VERIFIED')\n"
    # S-07 (audit 2026-09-22). This runs a GENERATED verifier snippet — code the
    # authoring assistant wrote — and it was handed `dict(os.environ)`: every
    # provider key, OAuth token and cloud credential in the parent process.
    #
    # `safe_subprocess.get_delegated_env` exists for exactly this and is an
    # ALLOWLIST, not another denylist: nothing crosses unless it was named, so a
    # key the denylist has never heard of is absent by construction rather than
    # by recognition. `BENCH_ANSWER` and `PYTHONPATH` are passed through `extra`,
    # which is the caller stating what it needs on purpose.
    extra = {"BENCH_ANSWER": answer or ""}
    if cwd:
        extra["PYTHONPATH"] = str(cwd)
    try:
        from llm_router.safe_subprocess import get_delegated_env
        env = get_delegated_env(extra)
    except Exception:  # noqa: BLE001
        # Fail CLOSED. If the allowlist cannot be reached we hand the child a
        # minimal environment rather than falling back to the full one — the
        # fallback IS the vulnerability.
        env = {"PATH": os.defpath, **extra}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            cwd=str(cwd) if cwd else None, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "verifier timeout"
    except Exception as exc:  # noqa: BLE001
        return False, f"verifier crashed: {type(exc).__name__}: {exc}"
    if proc.returncode == 0:
        return True, ""
    tail = (proc.stderr or proc.stdout).strip().splitlines()
    return False, (tail[-1][:200] if tail else "verifier failed with no output")


def self_test() -> int:
    """Check the helpers actually discriminate, rather than passing everything.

    A verifier suite that accepts every answer is the failure mode that looks
    like success, so this asserts on both directions for each helper.
    """
    cases: list[tuple[str, str, str, bool]] = [
        # (name, verifier, answer, expected_accept)
        ("words-hit", 'words("lisbon")', "The capital is Lisbon.", True),
        ("words-miss", 'words("lisbon")', "The capital is Madrid.", False),
        ("num-hit", "num(42)", "the answer is 42", True),
        ("num-miss", "num(42)", "the answer is 43", False),
        ("len-in", "length_between(5, 10)", "one two three four five six", True),
        ("len-under", "length_between(5, 10)", "one two", False),
        ("len-over", "length_between(2, 3)", "one two three four five", False),
        ("one-of-rec", "exactly_one_of(45, 15)",
         "Comparing 45 and 15 seconds, I recommend 15 seconds.", True),
        ("one-of-both-nodecide", "exactly_one_of(45, 15)",
         "45 seconds is long and 15 seconds is short.", False),
        ("one-of-none", "exactly_one_of(45, 15)", "use a timeout of 30", False),
        ("all-hit", 'contains_all("eviction", "metrics")',
         "It has eviction and metrics.", True),
        ("all-miss", 'contains_all("eviction", "metrics")', "It has eviction.", False),
        ("empty-answer", 'words("lisbon")', "", False),
        ("json-ok", 'json_schema(required=["name"], types={"name": str})',
         '{"name": "widget"}', True),
        ("json-fenced-ok", 'json_schema(required=["name"])',
         '```json\n{"name": "widget"}\n```', True),
        ("json-bad", 'json_schema(required=["name"])', "not json at all", False),
        ("json-missing-key", 'json_schema(required=["name"])', '{"other": 1}', False),
        ("json-wrong-type", 'json_schema(required=["n"], types={"n": int})',
         '{"n": "five"}', False),
        ("json-constraint-ok",
         'json_schema(required=["n"], where={"n": lambda v: v > 10})', '{"n": 42}', True),
        ("json-constraint-bad",
         'json_schema(required=["n"], where={"n": lambda v: v > 10})', '{"n": 2}', False),
        ("no-hardcode-ok", 'no_hardcoded_answer(42)', "the result is forty-two", True),
        ("no-hardcode-caught", 'no_hardcoded_answer(42)', "the result is 42", False),
    ]
    failures = 0
    for name, verifier, answer, want in cases:
        got, reason = run_verifier(verifier, answer)
        ok = got == want
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name:22s} accept={got!s:5s} "
              f"want={want!s:5s} {reason[:60]}")
    print(f"\n{len(cases) - failures}/{len(cases)} verifier self-tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
