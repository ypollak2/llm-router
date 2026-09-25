"""S: run_command accepts the command shapes models actually write — safely.

The continuation replay (2026-09-24, 20 real moments) scored 0/20, and a trace
showed why: the local model oriented itself exactly as Claude would —
`git log --oneline -12 && git status --short | head -20` — and run_command,
which has no shell by design, passed `&&` and `|` as literal arguments
("ambiguous argument '&&'", "unknown switch `2'"). It also refused the
read-only `git branch --show-current`. The model retried variants until all
15 steps were gone and never reached an edit.

Still no shell: sequences and pipelines are split by a tokenizer, EVERY segment
passes the allowlist before ANY runs, and pipes are chained in Python.
"""
from __future__ import annotations

import subprocess
import time

import pytest

from llm_router.hooks.agent_loop import execute_tool


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text(f"line {i}\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", f"c{i}"],
                       cwd=tmp_path, check=True)
    return tmp_path


def run(cmd, root):
    return execute_tool("run_command", {"command": cmd}, root)


def test_a_pipeline_runs(repo):
    out = run("git log --oneline | head -2", repo)
    # Compare commit *subjects* (the last token of each `--oneline` line), not
    # raw substrings of `out`: an abbreviated hash is hex and can coincidentally
    # contain "c0" (e.g. "c0d84a2"), which would fail this assertion for a
    # reason that has nothing to do with the pipeline under test.
    subjects = {ln.rsplit(None, 1)[-1] for ln in out.strip().splitlines() if ln}
    assert subjects == {"c2", "c1"}, out


def test_yes_pipeline_terminates_early(repo):
    """`head` must bound an UNBOUNDED upstream by exiting the moment it has
    its line(s) — not after the producer finishes. If the pipeline stages
    were run sequentially (drain stage 1 fully, then feed stage 2), `yes`
    never finishes and this would hang until the 30s command timeout instead
    of returning almost instantly."""
    start = time.monotonic()
    out = run("yes | head -1", repo)
    elapsed = time.monotonic() - start
    assert out.strip() == "y", out
    assert elapsed < 5, f"took {elapsed:.1f}s — upstream was not bounded by head"


def test_a_sequence_runs_both(repo):
    out = run("git log --oneline -1 && ls", repo)
    assert "c2" in out and "f0.txt" in out, out


def test_and_stops_after_a_failure_semicolon_does_not(repo):
    out = run("ls no-such-file && echo AFTER", repo)
    assert "AFTER" not in out
    assert "AFTER" in run("ls no-such-file ; echo AFTER", repo)


def test_stderr_to_devnull_is_accepted(repo):
    out = run("git log --oneline -1 2>/dev/null", repo)
    assert "c2" in out and "Error" not in out, out


def test_every_segment_is_checked_before_any_runs(repo):
    out = run("echo RAN > /dev/null; git push origin main", repo)
    assert "REFUSED" in out and "RAN" not in out, out
    out = run("ls && rm -rf f0.txt", repo)
    assert "REFUSED" in out and (repo / "f0.txt").exists()
    # …including a program that only appears AFTER a pipe.
    out = run("git log --oneline | rm f0.txt", repo)
    assert "REFUSED" in out and (repo / "f0.txt").exists(), out


def test_a_redirect_into_a_file_is_refused(repo):
    out = run("echo hi > out.txt", repo)
    assert "write_file" in out and not (repo / "out.txt").exists(), out


def test_still_no_shell(repo):
    run("ls $(touch pwned)", repo)
    run("ls `touch pwned2`", repo)
    assert not (repo / "pwned").exists() and not (repo / "pwned2").exists()


def test_read_only_git_branch_is_allowed_but_creating_one_is_not(repo):
    assert "REFUSED" not in run("git branch --show-current", repo)
    assert "REFUSED" not in run("git branch -a", repo)
    out = run("git branch newbranch", repo)
    assert "REFUSED" in out
    assert "newbranch" not in subprocess.run(["git", "branch"], cwd=repo,
                                             capture_output=True, text=True).stdout


def test_stderr_merged_into_stdout(repo):
    """`2>&1` — the second idiom the run-2 trace hit ("'>&' needs a shell")."""
    out = run("git log --oneline -1 2>&1 | head -1", repo)
    assert "c2" in out and "REFUSED" not in out, out
    out = run("ls no-such-file 2>&1", repo)
    assert "No such file" in out and "STDERR" not in out, out
