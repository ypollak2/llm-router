"""A pool-authored ACTIVE verifier must be able to grade a frozen task — T-06.

A candidate that passed eligibility → envelope → pool → propose → MUTATION
VALIDATION → HUMAN SIGN-OFF → ACTIVE could never contribute a label, because
the two halves of the subsystem use different identities:

    pool candidates      gtc-<content-hash>     (accumulate._task_id)
    frozen dataset tasks gt-<seq>               (author_tasks.py)

`run_matrix`'s only bridge was `reg.active_for(t.task_id)`, and the namespaces
never intersect. So the rigorous half — mutation testing, human approval — was
decorative, and every label that exists came from the older manual path. No test
exercised `--use-registry` end to end; it appeared only in a code comment.

Both ids derive from the same place, so the bridge needs no mapping table:
`gtc-{exact_key(prompt)}` is recomputable from any frozen task's own prompt.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from groundtruth import dataset as ds                      # noqa: E402
from groundtruth import verifier_registry as vr            # noqa: E402
from groundtruth.extract_corpus import exact_key           # noqa: E402
from groundtruth.run_matrix import (                       # noqa: E402
    _content_task_id, _registry_any_for, _registry_record_for,
)
from groundtruth.verifiers import run_verifier             # noqa: E402

PROMPT = "What is the capital of Portugal? Answer with just the city name."
SNIPPET = 'words("lisbon")'


@pytest.fixture
def frozen_task():
    """A task exactly as `freeze.py` writes it: sequential id, no verifier."""
    return ds.Task(task_id="gt-0001", prompt=PROMPT, kind=ds.QA,
                   verifier_kind=ds.SUBJECTIVE, verifier=None,
                   ambiguity_reason="no verifier authored yet")


@pytest.fixture
def registry_with_active_verifier(tmp_path):
    """A verifier as the POOL authors it: content-hash id, driven to ACTIVE."""
    reg = vr.Registry(tmp_path / "verifiers.jsonl")
    reg.save(vr.VerifierRecord(
        task_id=f"gtc-{exact_key(PROMPT)}",
        status=vr.ACTIVE,
        proposal={"verifier_snippet": SNIPPET,
                  "verification_class": "FACTUAL",
                  "verification_strategy": "schema"},
    ))
    return reg


# ── the finding ──────────────────────────────────────────────────────────────

def test_the_two_namespaces_really_do_not_intersect(frozen_task):
    """Anti-vacuity. If they ever did, every test below would pass for free."""
    assert frozen_task.task_id.startswith("gt-")
    assert _content_task_id(PROMPT).startswith("gtc-")
    assert frozen_task.task_id != _content_task_id(PROMPT)


def test_the_old_lookup_finds_nothing(frozen_task, registry_with_active_verifier):
    """The defect, stated as a test: this is what `run_matrix` used to do."""
    assert registry_with_active_verifier.active_for(frozen_task.task_id) is None


def test_the_bridged_lookup_finds_the_active_verifier(
    frozen_task, registry_with_active_verifier
):
    rec = _registry_record_for(registry_with_active_verifier, frozen_task)
    assert rec is not None, (
        "a verifier that passed mutation validation and human sign-off is still "
        "unreachable from the task it was authored for"
    )
    assert rec.status == vr.ACTIVE
    assert rec.proposal["verifier_snippet"] == SNIPPET


# ── end to end: it must actually GRADE ───────────────────────────────────────

def test_the_adopted_verifier_grades_a_correct_answer(
    frozen_task, registry_with_active_verifier
):
    """The gate: ACTIVE verifier → frozen task → a verdict on a real answer."""
    rec = _registry_record_for(registry_with_active_verifier, frozen_task)
    frozen_task.verifier = rec.proposal["verifier_snippet"]
    frozen_task.verifier_kind = ds.MECHANICAL
    frozen_task.verification_type = rec.proposal["verification_class"]

    accepted, reason = run_verifier(frozen_task.verifier, "Lisbon")
    assert accepted, f"the adopted verifier rejected a correct answer: {reason}"


def test_the_adopted_verifier_rejects_a_wrong_answer(
    frozen_task, registry_with_active_verifier
):
    """A verifier that accepts everything is not a verifier.

    The other half of the gate — without this, "it graded end to end" is
    satisfied by a snippet that returns True unconditionally.
    """
    rec = _registry_record_for(registry_with_active_verifier, frozen_task)
    accepted, _ = run_verifier(rec.proposal["verifier_snippet"], "Madrid")
    assert not accepted, "the adopted verifier accepted a wrong answer"


# ── the lifecycle gate still holds ───────────────────────────────────────────

@pytest.mark.parametrize("status", [vr.PROPOSED, vr.VALIDATED, vr.APPROVED])
def test_a_non_active_verifier_is_still_not_adopted(tmp_path, frozen_task, status):
    """Bridging the namespaces must not bridge the approval gate.

    A PROPOSED or even APPROVED verifier is a draft; grading with it would let a
    generated assertion define truth, which is the one thing the authoring flow
    exists to prevent.
    """
    reg = vr.Registry(tmp_path / "verifiers.jsonl")
    reg.save(vr.VerifierRecord(
        task_id=f"gtc-{exact_key(PROMPT)}", status=status,
        proposal={"verifier_snippet": SNIPPET, "verification_class": "FACTUAL"},
    ))
    assert _registry_record_for(reg, frozen_task) is None, (
        f"a {status} verifier was adopted — the human sign-off gate is bypassed"
    )
    # ...but it must still be COUNTED, so the run can say so.
    assert _registry_any_for(reg, frozen_task) is not None


# ── the refusal, stated out loud ─────────────────────────────────────────────

def _code_only(module) -> str:
    """Module source with comments and string literals removed.

    A scan over raw source matches the prose describing the defect as readily as
    the defect. That has now tripped four source-assertions in this audit —
    including this file's own docstring, which quotes the very expression the
    test forbids.
    """
    import inspect
    import io
    import tokenize

    src = inspect.getsource(module)
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except tokenize.TokenError:
        return src
    return " ".join(out)


def test_a_proposed_files_verifier_is_refused_not_silently_skipped():
    """pytest/mutation strategies populate `proposed_files`, never a snippet.

    Executing generated files against the repo is a decision about trust, not
    missing plumbing. It stays refused — but loudly, because a candidate that
    survived mutation validation and human approval and then vanished with no
    message is how this subsystem became decorative in the first place.
    """
    from groundtruth import run_matrix

    # R13: `_code_only` strips comments, which was a partial defence — but it
    # is still a substring scan over text. Both halves are AST now: the counter
    # is a NAME the code uses, and the message is a STRING VALUE.
    import ast
    import inspect
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _ast_assert import string_constants

    tree = ast.parse(inspect.getsource(run_matrix))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert any("unbridgeable" in n for n in names), "the refusal counter is gone"
    assert any("REFUSED" in s for s in string_constants(tree)), (
        "the refusal message is not a string the code emits"
    )


def test_run_matrix_uses_the_bridge():
    """Rule B: the call site, not the helper."""
    from groundtruth import run_matrix

    import ast
    import inspect

    tree = ast.parse(inspect.getsource(run_matrix))
    calls = {ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "_registry_record_for(reg, t)" in calls, (
        f"run_matrix still looks up by the frozen task id alone; calls: "
        f"{sorted(c for c in calls if 'registry' in c or 'active_for' in c)}"
    )
    assert "reg.active_for(t.task_id)" not in calls, (
        "the un-bridged lookup is still called"
    )
