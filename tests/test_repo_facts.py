"""Observed repository state, and the rules that keep it from becoming fiction.

N11. OKF answers "what does this repo say", the session answers "what did we say",
and neither answers "what is true right now". A continuation like "merge once CI
is green" or "check if windows was failing on main" needs the branch and the last
command's outcome; a model given only the conversation invents them. Measured
2026-09-14: 10 of 144 drafts claimed an action that never happened.

The danger is real and has a receipt in this repo: a draft's invented "63.2%
complete (5,309/8,400)" was written into session memory and returned as the next
turn's ground truth, escalating to "78.5% complete (6,600/8,400)" for a project
that does not exist. Two properties keep this different in kind:

  * every field is read from `git`, never from model output
  * every field is overwritten from a fresh read, never appended

so there is no history here to compound.
"""
from __future__ import annotations

import subprocess
import time

import pytest

from llm_router import repo_facts


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=False)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=False)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=False)
    subprocess.run(["git", "commit", "-qm", "first commit"], cwd=tmp_path, check=False)
    return tmp_path


def test_it_reports_the_branch_and_head(repo):
    f = repo_facts.collect(str(repo))
    assert f.get("branch")
    assert f.get("head") and len(f["head"]) >= 7
    assert f.get("last_commit") == "first commit"


def test_a_clean_tree_says_zero_not_nothing(repo):
    assert repo_facts.collect(str(repo))["uncommitted"] == "0", (
        "absent is not the same as clean; a model reading no field would guess"
    )


def test_it_names_the_files_that_changed(repo):
    (repo / "b.py").write_text("y = 2\n")
    (repo / "a.py").write_text("x = 99\n")
    f = repo_facts.collect(str(repo))
    assert f["uncommitted"] == "2"
    assert "a.py" in f["changed"] and "b.py" in f["changed"]


def test_paths_are_parsed_not_sliced(repo):
    """`ln[3:]` turned "src/..." into "rc/...". Porcelain prefixes vary."""
    (repo / "src").mkdir()
    (repo / "src" / "deep.py").write_text("z = 3\n")
    f = repo_facts.collect(str(repo))
    assert "src/deep.py" in f["changed"], f["changed"]


def test_a_rename_reports_the_new_name(repo):
    subprocess.run(["git", "mv", "a.py", "renamed.py"], cwd=repo, check=False)
    f = repo_facts.collect(str(repo))
    assert "renamed.py" in f["changed"], f["changed"]


def test_outside_a_repository_it_says_nothing(tmp_path):
    assert repo_facts.collect(str(tmp_path)) == {}
    assert repo_facts.render(str(tmp_path)) == ""


def test_the_block_is_labelled_as_observed(repo):
    block = repo_facts.render(str(repo))
    assert "observed just now, not model output" in block, (
        "unlabelled state reads as an instruction, and a reader of the draft "
        "cannot tell which parts were grounded"
    )


def test_it_is_small_enough_to_carry(repo):
    for i in range(30):
        (repo / f"f{i}.py").write_text("x\n")
    block = repo_facts.render(str(repo))
    assert len(block) < 900, (
        f"{len(block)} chars competes with a payload measured at ~457 tokens "
        "against a ~37s first-model budget"
    )


def test_it_is_fast_enough_to_run_on_every_call(repo):
    t0 = time.monotonic()
    repo_facts.render(str(repo))
    assert (time.monotonic() - t0) < 1.0


def test_a_broken_git_yields_nothing_rather_than_a_guess(repo, monkeypatch):
    monkeypatch.setattr(repo_facts.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert repo_facts.collect(str(repo)) == {}


def test_nothing_here_can_be_written_by_a_model():
    """The whole module's authorship rule, asserted rather than assumed."""
    src = (repo_facts.__file__)
    text = open(src).read()
    body = text.split('"""', 2)[-1]           # skip the module docstring
    assert "def set" not in body and "def record" not in body, (
        "repo_facts gained a writer. Only git may author these fields; a model-"
        "written value here is the fabrication loop with extra steps."
    )
