"""M-01 — the envelope's two answers to "can we replay this?" disagreed.

`completeness()` — which the module's own docstring calls "the conservative
half of the design" — tested `repo.diff_sha256`, the HASH. `reconstructable`
tests `repo.diff`, the PATCH. So for a dirty tree whose patch was never stored,
or was truncated:

    completeness()          -> (True, [])      "nothing missing"
    repo.reconstructable    -> False           "cannot rebuild this tree"

on the same object, at the same moment.

That is precisely the failure `reconstructable`'s own docstring records and was
written to prevent: *"an earlier version kept only `diff_sha256`, which proves
you have the right tree and cannot produce it."* It came back in the function
that decides whether a candidate is admissible at all.

It was masked by `accumulate.py:95`, which independently used `reconstructable`
for `has_repo_state` — so the pipeline behaved correctly by luck rather than by
design. `accumulate.py`'s check is kept, because it is the right predicate for
that parameter; what changed is that both now resolve through **one** definition
instead of one silently covering for the other.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def E():
    sys.path.insert(0, str(ROOT / "scripts"))
    key = "_gt_envelope"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        key, ROOT / "scripts" / "groundtruth" / "envelope.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def _repo(E, **kw):
    import inspect

    base = dict(commit="abc123", dirty=True, diff="", diff_sha256="deadbeef")
    base.update(kw)
    allowed = set(inspect.signature(E.RepoState).parameters)
    return E.RepoState(**{k: v for k, v in base.items() if k in allowed})


CASES = {
    "clean tree": dict(dirty=False),
    "dirty, patch stored": dict(diff="--- a\n+++ b\n"),
    "dirty, hash only": dict(diff="", diff_sha256="deadbeef"),
    "dirty, patch truncated": dict(diff="--- a", diff_truncated=True),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_two_answers_agree(E, name):
    """One definition. Whatever the answer is, it must be the same answer."""
    repo = _repo(E, **CASES[name])
    env = E.ReplayEnvelope(prompt="do the thing", required_state=["repo"], repo=repo)
    complete, missing = env.completeness()
    assert complete == repo.reconstructable, (
        f"{name}: completeness()={complete} but reconstructable={repo.reconstructable} "
        f"(missing={missing})"
    )


def test_a_hash_without_a_patch_is_not_complete(E):
    """The specific regression. A hash recognises a tree; it cannot produce one."""
    env = E.ReplayEnvelope(
        prompt="fix the retry logic",
        required_state=["repo"],
        repo=_repo(E, diff="", diff_sha256="deadbeef"),
    )
    complete, missing = env.completeness()
    assert complete is False
    assert any("diff" in m for m in missing), missing


def test_a_truncated_patch_says_so_distinctly(E):
    """"Too large to store" and "never captured" are different operator problems."""
    env = E.ReplayEnvelope(
        prompt="fix the retry logic",
        required_state=["repo"],
        repo=_repo(E, diff="--- a", diff_truncated=True),
    )
    _, missing = env.completeness()
    assert "repo-dirty-diff-truncated" in missing, missing


def test_a_stored_patch_is_complete(E):
    """Anti-over-correction: the common case must still pass.

    Developers usually have dirty trees. `reconstructable`'s docstring records
    that rejecting them all once "rejected most real coding traffic".
    """
    env = E.ReplayEnvelope(
        prompt="fix the retry logic",
        required_state=["repo"],
        repo=_repo(E, diff="--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n"),
    )
    complete, missing = env.completeness()
    assert complete is True, missing


def _executable_source(path: pathlib.Path) -> str:
    """Source with comments and string literals removed.

    A raw substring check would match the explanation of the bug in the comment
    that documents the fix — the assertion has to look at code.
    """
    import tokenize

    parts = []
    with open(path, "rb") as fh:
        try:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                parts.append(tok.string)
        except tokenize.TokenError:
            return ""
    return " ".join(parts)


def test_completeness_delegates_rather_than_reimplements(E):
    """Pin the mechanism: a second copy of the rule is how they drifted."""
    code = _executable_source(ROOT / "scripts" / "groundtruth" / "envelope.py")
    assert "reconstructable" in code, (
        "envelope.py no longer references reconstructable at all"
    )
    body = code.split("def completeness (")[1].split(" def ")[0] if "def completeness (" in code \
        else code.split("completeness")[1][:600]
    assert "reconstructable" in body, (
        "completeness() no longer defers to reconstructable; the two answers can "
        "drift apart again"
    )
    assert "diff_sha256" not in body, (
        "completeness() is testing the hash again — that is the original defect"
    )


def test_the_source_scan_strips_comments(E):
    """Anti-vacuity for the scan above: it must not be reading prose."""
    code = _executable_source(ROOT / "scripts" / "groundtruth" / "envelope.py")
    assert "def" in code and "completeness" in code, "scan returned nothing usable"
    assert "load-bearing" not in code, "docstrings are not being stripped"


def test_this_suite_covers_the_disagreeing_case(E):
    """Anti-vacuity: at least one case must be one where they USED to differ.

    If every fixture were a clean tree, the parametrised agreement test would
    pass with the old code too.
    """
    repo = _repo(E, diff="", diff_sha256="deadbeef")
    assert repo.dirty is True and not repo.diff, "the known-positive fixture is wrong"
    assert repo.reconstructable is False
