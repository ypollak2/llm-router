"""A repo-bound task grades against its own tree, or not at all — F31 / T-06.

`run_verifier` accepts a `cwd`. `run_matrix` never passed one.

`Task.sandbox` is documented as "fixture tree name for EDIT/repo-bound tasks";
`author_tasks.py` writes the literal `"TODO"` into it for every EDIT task it
scaffolds; and nothing in the pipeline ever resolved that name to a directory.
So a repo-bound task's verifier ran in whatever directory the operator happened
to be standing in — asserting a claim about repo state against an unrelated
tree — and the verdict was recorded as a Ground Truth label.

This is the rule eligibility already applies as `no-replayer-for-required-state`:
a task that needs repo state it cannot be given is REFUSED with a reason. A
refusal produces an AMBIGUOUS row; grading in the wrong tree produces a
confident lie.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from groundtruth import dataset as ds                       # noqa: E402
from groundtruth.run_matrix import resolve_sandbox          # noqa: E402
from groundtruth.verifiers import run_verifier              # noqa: E402


def task(sandbox, *, kind=ds.EDIT):
    return ds.Task(task_id="gt-0001", prompt="Edit the module so it exports FOO.",
                   kind=kind, verifier_kind=ds.MECHANICAL,
                   verifier="import target; assert target.FOO == 42",
                   sandbox=sandbox)


# ── refusal ──────────────────────────────────────────────────────────────────

def test_an_unauthored_sandbox_is_refused_with_a_reason(tmp_path):
    """`author_tasks.py` writes "TODO". That must never grade."""
    cwd, reason = resolve_sandbox(tmp_path, task("TODO"))
    assert cwd is None
    assert "TODO" in reason and "refusing" in reason


def test_a_missing_sandbox_tree_is_refused_with_a_reason(tmp_path):
    cwd, reason = resolve_sandbox(tmp_path, task("repo-fixture-a"))
    assert cwd is None
    assert "repo-fixture-a" in reason
    assert "not found" in reason


def test_the_refusal_names_a_path_so_it_can_be_acted_on(tmp_path):
    _cwd, reason = resolve_sandbox(tmp_path, task("repo-fixture-a"))
    assert str(tmp_path / "sandboxes" / "repo-fixture-a") in reason


# ── the happy path ───────────────────────────────────────────────────────────

def test_a_present_sandbox_resolves_to_its_tree(tmp_path):
    tree = tmp_path / "sandboxes" / "repo-fixture-a"
    tree.mkdir(parents=True)
    cwd, reason = resolve_sandbox(tmp_path, task("repo-fixture-a"))
    assert reason == ""
    assert cwd == tree


def test_a_non_repo_bound_task_needs_no_sandbox(tmp_path):
    """Anti-vacuity: the refusal must not swallow ordinary QA tasks."""
    cwd, reason = resolve_sandbox(tmp_path, task(None, kind=ds.QA))
    assert cwd is None
    assert reason == "", "a plain QA task was refused for lacking a sandbox"


# ── it actually changes the verdict ──────────────────────────────────────────

def test_the_verifier_really_runs_inside_the_resolved_tree(tmp_path):
    """The whole point of the `cwd`.

    Without it the import resolves against the operator's CWD — or not at all —
    and the task is graded on the wrong evidence.
    """
    tree = tmp_path / "sandboxes" / "repo-fixture-a"
    tree.mkdir(parents=True)
    (tree / "target.py").write_text("FOO = 42\n", encoding="utf-8")

    cwd, reason = resolve_sandbox(tmp_path, task("repo-fixture-a"))
    assert reason == ""

    accepted, why = run_verifier("import target; assert target.FOO == 42",
                                 "irrelevant", cwd=cwd)
    assert accepted, f"verifier failed inside its own sandbox: {why}"

    # And the same verifier must NOT pass without the tree — otherwise the cwd
    # is decorative and this test proves nothing.
    accepted_nowhere, _ = run_verifier("import target; assert target.FOO == 42",
                                       "irrelevant", cwd=tmp_path)
    assert not accepted_nowhere, (
        "the verifier passed outside its sandbox — it is not reading repo state at all"
    )


def test_a_wrong_tree_produces_a_different_verdict(tmp_path):
    """The failure this prevents, made concrete."""
    right = tmp_path / "sandboxes" / "right"
    wrong = tmp_path / "sandboxes" / "wrong"
    right.mkdir(parents=True)
    wrong.mkdir(parents=True)
    (right / "target.py").write_text("FOO = 42\n", encoding="utf-8")
    (wrong / "target.py").write_text("FOO = 7\n", encoding="utf-8")

    v = "import target; assert target.FOO == 42"
    assert run_verifier(v, "x", cwd=right)[0] is True
    assert run_verifier(v, "x", cwd=wrong)[0] is False


# ── the call site ────────────────────────────────────────────────────────────

def test_run_matrix_passes_the_cwd():
    """Rule B. Resolving a path nobody uses is not a fix."""
    import inspect
    from groundtruth import run_matrix

    src = inspect.getsource(run_matrix)
    assert "cwd=sandbox_cwd" in src, "run_matrix still grades in the ambient directory"
    assert "resolve_sandbox(root, task)" in src


def test_a_refused_task_is_never_sampled():
    """Refusing must also stop the model calls — otherwise it costs money to
    produce answers that cannot be graded."""
    import inspect
    from groundtruth import run_matrix

    src = inspect.getsource(run_matrix)
    assert "args.samples if not sandbox_refusal else 0" in src
