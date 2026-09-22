"""H-08 — the gate admitted work the harness cannot run.

`scripts/groundtruth/` contains **zero** `git checkout`, `git apply` or worktree
call sites. `run_matrix.call_model()` sends the prompt as a single-turn
completion and grades the raw text.

Meanwhile the eligibility gate was tuned to admit exactly the tasks that need a
repository: capture the repo state and the task became eligible. So the envelope
captured everything required to replay, nothing consumed it, and the pool filled
with candidates that could never be labelled.

The failure mode is worse than "no labels". The funnel reports *why* tasks are
not accumulating, and admitting an ungradable task makes that report say the
pipeline is healthy while it produces nothing. A gate that admits work the runner
cannot execute is how a pool fills with candidates that will never be labelled.

Option B from the remediation plan: narrow the gate now, implement replay later.
`replay_available()` is tied to the **capability** — it looks for a runner
function — rather than to a hand-maintained flag, so when replay is implemented
these tasks become eligible again automatically and the gate cannot drift out of
date.

`R_NO_REPLAYER` is deliberately a separate reason from `R_ENVELOPE_INCOMPLETE`.
The latter says "we failed to capture the state" — something an operator can
act on. The former says "we captured it and there is no runner", which they
cannot. Reporting the second as the first would send someone to fix capture.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GT = ROOT / "scripts" / "groundtruth"


def _load(name: str):
    key = f"_gt_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, GT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def E():
    sys.path.insert(0, str(ROOT / "scripts"))
    return _load("eligibility")


REPO_TASK = "fix the retry logic in src/llm_router/router.py so the timeout is honoured"
PLAIN_TASK = "what is the capital of Portugal? answer with just the city name"


def test_there_is_still_no_replayer(E):
    """Pins the premise. If this flips, the narrowing below must be revisited."""
    assert E.replay_available() is False, (
        "a replayer now exists — remove the R_NO_REPLAYER narrowing and let "
        "repo-state tasks through"
    )


def _code_only(path: pathlib.Path) -> str:
    """Source with comments and string literals removed.

    Grepping raw text finds the words in docstrings — including the ones in this
    very file explaining the finding — so the scan has to look at code.
    """
    import tokenize

    out = []
    with open(path, "rb") as fh:
        try:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                out.append(tok.string)
        except tokenize.TokenError:
            return ""
    return " ".join(out)


def test_no_checkout_or_apply_call_sites_exist():
    """The measurement behind the finding, kept checkable.

    Looks at code, not text: the tokens appear in prose throughout this module
    and in `eligibility.replay_available`'s own docstring.
    """
    sites = []
    for f in sorted(GT.glob("*.py")):
        code = _code_only(f)
        for token in ("checkout", "worktree"):
            if token in code:
                sites.append(f"{f.name}: `{token}` appears in code")
    assert not sites, (
        f"a replay call site appeared: {sites}. If replay is now implemented, "
        f"`replay_available()` should detect it and this test should be updated."
    )


def test_the_code_scan_is_not_vacuous():
    """The tokenizer must actually be reading code, or the scan proves nothing."""
    code = _code_only(GT / "eligibility.py")
    assert "def" in code and "assess" in code, "the code scan returned nothing usable"
    # and it must genuinely strip strings — the docstring tokens must be gone
    assert "single-turn completion" not in code, "docstrings are not being stripped"


def test_a_repo_task_with_captured_state_is_not_admitted(E):
    """The exact case that filled the pool: capture succeeded, grading cannot."""
    a = E.assess(REPO_TASK, has_repo_state=True)
    assert a.ground_truth_candidate is False
    assert E.R_NO_REPLAYER in a.ineligibility_reasons, (
        f"admitted an ungradable task, or gave the wrong reason: "
        f"{a.ineligibility_reasons}"
    )


def test_missing_capture_is_reported_as_capture_not_as_replay(E):
    """The two reasons must not be conflated — they send an operator elsewhere."""
    a = E.assess(REPO_TASK, has_repo_state=False)
    assert E.R_ENVELOPE_INCOMPLETE in a.ineligibility_reasons
    assert E.R_NO_REPLAYER not in a.ineligibility_reasons


def test_gradable_tasks_are_still_admitted(E):
    """Anti-over-correction. Narrowing the gate to nothing is not a fix.

    A single-turn question with a checkable answer is precisely what the current
    harness CAN grade, and it must still get through.
    """
    a = E.assess(PLAIN_TASK)
    assert a.ground_truth_candidate is True, (
        f"a task the harness can actually run was rejected: {a.ineligibility_reasons}"
    )


def test_the_gate_flips_back_automatically_when_replay_lands(E, monkeypatch):
    """Tied to the capability, not to a flag someone has to remember to clear."""
    monkeypatch.setattr(E, "replay_available", lambda: True)
    a = E.assess(REPO_TASK, has_repo_state=True)
    assert a.ground_truth_candidate is True, (
        f"repo-state tasks stay rejected even with a replayer present: "
        f"{a.ineligibility_reasons}"
    )


def test_replay_detection_looks_for_a_real_runner(E):
    """Anti-vacuity: `replay_available` must be capable of returning True.

    A function that always returns False would make every assertion above pass
    while proving nothing about the mechanism.
    """
    import types

    fake = types.ModuleType("groundtruth.run_matrix")
    fake.replay_in_worktree = lambda *a, **k: None
    monkey = sys.modules.get("groundtruth.run_matrix")
    sys.modules["groundtruth.run_matrix"] = fake
    sys.modules.setdefault("groundtruth", types.ModuleType("groundtruth"))
    sys.modules["groundtruth"].run_matrix = fake
    try:
        assert E.replay_available() is True, (
            "replay_available() cannot detect a runner even when one is present"
        )
    finally:
        if monkey is not None:
            sys.modules["groundtruth.run_matrix"] = monkey
        else:
            sys.modules.pop("groundtruth.run_matrix", None)
