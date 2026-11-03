"""W7 text-job-bind: the job input is built from the real repository, bounded, and safe to build.

Real git, real repositories in a temp directory. What the text job is shown is what decides
whether its answer can pass the router's own checks (they look for a file the diff really
touches), so the shape of ``diff --git`` headers is part of the contract.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from llm_router import text_jobs
from llm_router.text_job_bind import gitinput
from llm_router.text_job_bind.gitinput import InputRefused, build_commit_input, build_pr_input, condense


@pytest.fixture(autouse=True)
def _git_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main", ".")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "a.py").write_text("print('a')\n")
    (r / "docs").mkdir()
    (r / "docs" / "guide.md").write_text("# guide\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "feat: first")
    return r


# ---- commit input ------------------------------------------------------------------------------------------

def test_the_staged_diff_is_the_input_and_names_every_changed_file(repo):
    (repo / "a.py").write_text("print('a')\nprint('b')\n")
    (repo / "new file.txt").write_text("hi\n")
    (repo / "unstaged.py").write_text("x\n")  # untracked: never part of a commit message
    git(repo, "add", "a.py", "new file.txt")
    inp = build_commit_input(str(repo))
    assert inp.files == 2 and "diff --git a/a.py b/a.py" in inp.text
    assert "+print('b')" in inp.text and "unstaged" not in inp.text
    assert "## Changed files (2)" in inp.text and "a.py (+1/-0)" in inp.text
    assert "feat: first" in (inp.hint or "")
    # the router's own check reads exactly these headers
    assert "a.py" in text_jobs.changed_files(inp.text)


def test_the_hint_shows_recent_subjects_without_their_pull_request_numbers(repo):
    """A model told to match the style copies a ``(#123)`` suffix and makes up a number (seen on real commits)."""
    (repo / "a.py").write_text("print('a')\nprint('b')\n")
    git(repo, "commit", "-qam", "fix: second thing (#123)")
    (repo / "a.py").write_text("print('c')\n")
    git(repo, "add", "a.py")
    hint = build_commit_input(str(repo)).hint or ""
    assert [ln for ln in hint.splitlines() if ln.startswith("- ")] == ["- fix: second thing", "- feat: first"]
    assert "never make one up" in hint


def test_the_hint_offers_the_names_of_the_largest_changes_first_and_only_plain_names(repo):
    (repo / "small_one.py").write_text("x = 1\n")
    (repo / "big-module.py").write_text("".join(f"line {i}\n" for i in range(30)))
    (repo / "notes.v2.md").write_text("a name with a dot in it\n")
    git(repo, "add", ".")
    hint = build_commit_input(str(repo)).hint or ""
    offer = [ln for ln in hint.splitlines() if ln.startswith("Name at least one changed file")]
    assert len(offer) == 1 and "for example: big-module, small_one." in offer[0]
    assert "notes" not in offer[0]


def test_at_most_three_names_are_offered(repo):
    for i in range(5):
        (repo / f"module_{i}.py").write_text("x = 1\n" * (i + 1))
    git(repo, "add", ".")
    offer = [ln for ln in (build_commit_input(str(repo)).hint or "").splitlines() if ln.startswith("Name at least one")][0]
    assert "for example: module_4, module_3, module_2." in offer


def test_a_message_that_uses_an_offered_name_passes_the_routers_own_check(repo):
    """The offer is only worth making if it satisfies the check that judges the answer."""
    (repo / "ledger_view.py").write_text("x = 1\n")
    git(repo, "add", "ledger_view.py")
    inp = build_commit_input(str(repo))
    assert "for example: ledger_view." in (inp.hint or "")
    assert text_jobs.check_commit_message(inp.text, "fix: ledger_view keeps one row per id").ok
    assert not text_jobs.check_commit_message(inp.text, "fix: keep one row per id").ok


def test_no_plain_file_name_means_no_offer_and_the_subjects_stay(repo):
    (repo / "v1.2.3.md").write_text("x\n")
    git(repo, "add", ".")
    hint = build_commit_input(str(repo)).hint or ""
    assert "Name at least one" not in hint and "- feat: first" in hint


def test_nothing_staged_is_refused_with_the_next_step(repo):
    (repo / "a.py").write_text("changed\n")  # unstaged only
    with pytest.raises(InputRefused) as e:
        build_commit_input(str(repo))
    assert e.value.code == "nothing_staged" and "git add" in e.value.message


def test_commit_all_reads_tracked_changes_against_head(repo):
    (repo / "a.py").write_text("changed\n")
    (repo / "untracked.txt").write_text("u\n")
    inp = build_commit_input(str(repo), all_tracked=True)
    assert "a.py" in inp.text and "untracked.txt" not in inp.text
    git(repo, "rm", "-q", "--cached", "docs/guide.md")  # a staged deletion is part of -a too
    assert "docs/guide.md" in build_commit_input(str(repo), all_tracked=True).text


def test_commit_all_with_no_changes_and_without_head(repo, tmp_path):
    with pytest.raises(InputRefused) as e:
        build_commit_input(str(repo), all_tracked=True)
    assert e.value.code == "nothing_staged"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q", ".")
    with pytest.raises(InputRefused) as e:
        build_commit_input(str(fresh), all_tracked=True)
    assert e.value.code == "no_head"
    (fresh / "f.txt").write_text("x\n")
    git(fresh, "add", "f.txt")  # the first commit: staged diff against the empty tree
    assert "f.txt" in build_commit_input(str(fresh)).text


def test_a_directory_that_is_not_a_repository_is_refused(tmp_path):
    with pytest.raises(InputRefused) as e:
        build_commit_input(str(tmp_path))
    assert e.value.code == "not_a_repo"


def test_binary_rename_delete_and_odd_names_still_name_their_files(repo):
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 4)
    git(repo, "mv", "a.py", "renamed.py")
    git(repo, "rm", "-q", "docs/guide.md")
    (repo / "sp ace.txt").write_text("s\n")
    git(repo, "add", "-A")
    inp = build_commit_input(str(repo))
    assert "renamed.py" in inp.text and "guide.md" in inp.text and "blob.bin" in inp.text
    assert inp.files == 4


def test_a_user_diff_prefix_config_cannot_hide_the_file_names(repo):
    git(repo, "config", "diff.noprefix", "true")
    git(repo, "config", "diff.mnemonicprefix", "true")
    (repo / "a.py").write_text("print('z')\n")
    git(repo, "add", "a.py")
    inp = build_commit_input(str(repo))
    assert "diff --git a/a.py b/a.py" in inp.text and "a.py" in text_jobs.changed_files(inp.text)


def test_a_repositorys_own_diff_drivers_are_never_executed(repo, tmp_path):
    marker = tmp_path / "RAN"
    script = tmp_path / "drv.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat \"$@\"\n")
    script.chmod(0o755)
    git(repo, "config", "diff.evil.textconv", str(script))
    git(repo, "config", "diff.external", str(script))
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    (repo / "a.py").write_text("print('changed')\n")
    git(repo, "add", "-A")
    build_commit_input(str(repo))
    build_commit_input(str(repo), all_tracked=True)
    assert not marker.exists()


def test_the_hooks_own_git_environment_cannot_redirect_the_repository(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("GIT_DIR", str(other / "nonexistent.git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / "idx"))
    (repo / "a.py").write_text("print('env')\n")
    git_env_free = {k: v for k, v in os.environ.items() if not k.startswith("GIT_DIR") and k != "GIT_INDEX_FILE"}
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True, env=git_env_free)
    assert "print('env')" in build_commit_input(str(repo)).text


# ---- condensing --------------------------------------------------------------------------------------------

def _patch(n_files: int, lines_each: int, width: int = 20) -> str:
    out = []
    for i in range(n_files):
        out.append(f"diff --git a/f{i}.py b/f{i}.py\nindex 1..2 100644\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1,0 +1,{lines_each} @@")
        out.extend("+" + "x" * width for _ in range(lines_each))
    return "\n".join(out) + "\n"


def test_a_huge_file_is_cut_per_file_and_says_so():
    text, files, omitted = condense(_patch(1, 5000))
    assert files == 1 and omitted == 0
    assert text.count("\n+xxxx") == gitinput.BLOCK_MAX_LINES - 1  # the first kept line is the @@ header
    assert "more lines of this file omitted" in text and "diff --git a/f0.py b/f0.py" in text


def test_many_files_stay_inside_the_budget_and_every_one_that_fits_is_named():
    text, files, omitted = condense(_patch(900, 3), budget=20_000)
    assert files == 900 and omitted > 0
    assert len(text) <= 20_000 + 200
    assert "## Changed files (900)" in text and "... and 840 more" in text
    assert text.count("diff --git ") >= 100  # headers survive past the point where bodies stop


def test_the_default_budget_is_under_the_text_job_cap_even_for_a_giant_commit():
    text, files, omitted = condense(_patch(5000, 200, width=200))
    assert len(text) < text_jobs.MAX_INPUT_CHARS
    assert files == 5000


def test_a_minified_line_is_cut():
    patch = "diff --git a/m.js b/m.js\n--- a/m.js\n+++ b/m.js\n@@ -0,0 +1 @@\n+" + "x" * 200_000 + "\n"
    text, _, _ = condense(patch)
    assert len(text) < 2_000 and "[line cut]" in text


def test_counts_and_names_come_from_the_patch():
    text, files, _ = condense("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-a\n-b\n+c\n")
    assert files == 1 and "x (+1/-2)" in text


# ---- PR input ----------------------------------------------------------------------------------------------

def _branch(repo: Path, name: str = "feat", n: int = 3) -> None:
    git(repo, "checkout", "-q", "-b", name)
    for i in range(n):
        (repo / f"m{i}.py").write_text(f"v{i}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", f"feat: add m{i}")


def test_a_pr_input_has_branch_commits_stat_and_diff_against_the_base(repo):
    _branch(repo)
    inp = build_pr_input(str(repo), title="Add modules")
    assert "## Branch\nHEAD -> main" in inp.text
    assert "## Commits (3)" in inp.text and "feat: add m2" in inp.text and "feat: first" not in inp.text
    assert "## Changed files (3)" in inp.text and "diff --git a/m1.py b/m1.py" in inp.text
    assert inp.hint and "Add modules" in inp.hint
    assert text_jobs.changed_files(inp.text) == ["m0.py", "m1.py", "m2.py"]


def test_the_remote_base_is_preferred_and_a_named_base_head_are_used(repo):
    _branch(repo)
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert "HEAD -> origin/main" in build_pr_input(str(repo)).text
    git(repo, "checkout", "-q", "main")
    inp = build_pr_input(str(repo), base="main", head="feat")
    assert "feat -> origin/main" in inp.text and "## Commits (3)" in inp.text


def test_a_long_branch_lists_forty_commits_and_the_count(repo):
    _branch(repo, n=45)
    inp = build_pr_input(str(repo))
    assert "## Commits (45)" in inp.text and "... and 5 more" in inp.text


@pytest.mark.parametrize("setup,code", [
    ("none", "no_commits"),
    ("nobase", "no_base"),
    ("nohead", "no_head"),
])
def test_pr_input_refusals(repo, setup, code):
    if setup == "none":
        git(repo, "checkout", "-q", "-b", "same")
        with pytest.raises(InputRefused) as e:
            build_pr_input(str(repo))
    elif setup == "nobase":
        _branch(repo)
        with pytest.raises(InputRefused) as e:
            build_pr_input(str(repo), base="nonexistent")
    else:
        with pytest.raises(InputRefused) as e:
            build_pr_input(str(repo), head="owner:branch")
    assert e.value.code == code and e.value.message


def test_a_pr_input_fits_the_text_job_cap(repo):
    git(repo, "checkout", "-q", "-b", "big")
    for i in range(300):
        (repo / f"big{i}.txt").write_text("line\n" * 50)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "big")
    assert len(build_pr_input(str(repo)).text) < text_jobs.MAX_INPUT_CHARS


# ---- bounded git ---------------------------------------------------------------------------------------------

def test_output_past_the_byte_cap_is_cut_and_still_a_success(repo, monkeypatch):
    monkeypatch.setattr(gitinput, "_GIT_BYTES_CAP", 5_000)
    (repo / "a.py").write_text("x = 1\n" * 20_000)
    git(repo, "add", "a.py")
    inp = build_commit_input(str(repo))
    assert "diff --git a/a.py b/a.py" in inp.text and inp.files == 1


def test_a_git_that_hangs_is_killed_and_reported_not_mistaken_for_an_empty_diff(repo, monkeypatch, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    slow = fake / "git"
    slow.write_text("#!/bin/sh\nsleep 30\n")  # the sleep is a CHILD that inherits the pipe
    slow.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    # The fake git's `sleep 30` child holds the pipe: if the process group were not killed this call would
    # block for the 30 s (pytest's per-test timeout would fail it), so returning is the proof.
    rc, out = gitinput._git(str(repo), "status", timeout=0.4)
    assert rc == gitinput.RC_TIMEOUT and out == ""
    monkeypatch.setattr(gitinput, "_GIT_TIMEOUT_S", 0.4)
    monkeypatch.setattr(gitinput, "_git", lambda repo, *a, **k: (gitinput.RC_TIMEOUT, ""))
    with pytest.raises(InputRefused) as e:
        build_commit_input(str(repo))
    assert e.value.code == "git_timeout" and "did not answer" in e.value.message
