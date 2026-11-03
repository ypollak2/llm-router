"""W7 text-job-bind: what a command with the placeholder asks for, what is refused, and the rewrite.

The rewrite is checked the only way that means anything: the rewritten command is run by real
``bash`` and ``zsh`` against a real git repository (and a stub ``gh``), and what git or gh
received is compared with what the model asked for.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from llm_router.text_job_bind.plan import JOB_COMMIT, JOB_PR, Unsupported, analyse
from llm_router.text_job_bind.shellscan import ScanError, scan_commands

T = '"${LLM_TEXT:?}"'
GEN = "app: set x\n\nBody with 'quotes', \"double\", $HOME, `ticks`, $(date) and \\ backslashes."


def plan(cmd: str, cwd: str = "/base"):
    return analyse(cmd, cwd)


# ---- supported commit shapes ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,rewritten,repo,text", [
    (f"git commit -m {T}", "git commit -F P", "/base", "G\n"),
    (f"git commit -am {T}", "git commit -a -F P", "/base", "G\n"),
    (f"git commit -sm {T}", "git commit -s -F P", "/base", "G\n"),
    (f"git commit --message={T}", "git commit --file=P", "/base", "G\n"),
    (f"git commit --message {T}", "git commit --file P", "/base", "G\n"),
    ("git commit -m '${LLM_TEXT:?}'", "git commit -F P", "/base", "G\n"),
    ("git commit -m ${LLM_TEXT:?}", "git commit -F P", "/base", "G\n"),
    (f"git commit --no-verify -m {T} --trailer 'A: b'", "git commit --no-verify -F P --trailer 'A: b'", "/base", "G\n"),
    (f"git commit -m {T} -m 'Co-Authored-By: X <x@y>'", "git commit -F P", "/base", "G\n\nCo-Authored-By: X <x@y>\n"),
    (f"git commit -m 'Para one' -m {T}", "git commit -F P", "/base", "Para one\n\nG\n"),
    ('git commit -m "${LLM_TEXT:?}\n\nCo-Authored-By: X <x@y>"', "git commit -F P", "/base", "G\n\nCo-Authored-By: X <x@y>\n"),
    (f"cd /repo && git commit -m {T}", "cd /repo && git commit -F P", "/repo", "G\n"),
    (f"cd sub && git commit -m {T}", "cd sub && git commit -F P", "/base/sub", "G\n"),
    (f"git -C /repo commit -m {T}", "git -C /repo commit -F P", "/repo", "G\n"),
    (f"git -C /a -C b commit -m {T}", "git -C /a -C b commit -F P", "/a/b", "G\n"),
    (f"git status --short && git diff --stat; git commit -m {T}", "git status --short && git diff --stat; git commit -F P", "/base", "G\n"),
    (f"git commit -m {T} && git push", "git commit -F P && git push", "/base", "G\n"),
    (f"GIT_AUTHOR_NAME=a git commit -m {T} 2>&1", "GIT_AUTHOR_NAME=a git commit -F P 2>&1", "/base", "G\n"),
    (f"git commit -m {T} > /dev/null", "git commit -F P > /dev/null", "/base", "G\n"),
    (f"git commit -m {T} --author='A <a@b.c>' --date=2026-01-01", "git commit -F P --author='A <a@b.c>' --date=2026-01-01", "/base", "G\n"),
    (f"/usr/bin/git commit -m {T}", "/usr/bin/git commit -F P", "/base", "G\n"),
])
def test_supported_commit_shapes(cmd, rewritten, repo, text):
    p = plan(cmd)
    assert p is not None and p.job == JOB_COMMIT and p.repo_dir == repo
    assert p.final_text("G") == text
    assert p.rewrite("P") == rewritten


def test_commit_all_flag_is_read_from_a_cluster_and_from_the_long_form():
    assert plan(f"git commit -am {T}").all_tracked and plan(f"git commit --all -m {T}").all_tracked
    assert not plan(f"git commit -m {T}").all_tracked


# ---- supported PR shapes ------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,rewritten", [
    (f"gh pr create --title Title --body {T}", "gh pr create --title Title --body-file P"),
    (f"gh pr create -t Title -b {T}", "gh pr create -t Title --body-file P"),
    (f"gh pr create --title Title --body={T}", "gh pr create --title Title --body-file=P"),
    (f"gh pr create --title Title --body-file {T} --draft", "gh pr create --title Title --body-file P --draft"),
    (f"gh pr create -F {T} --title Title", "gh pr create --body-file P --title Title"),
    (f"git push -u origin HEAD && gh pr create --title Title --body {T}", "git push -u origin HEAD && gh pr create --title Title --body-file P"),
    (f"gh pr create --base main --head feat --title Title --body {T} --reviewer bob --label x", "gh pr create --base main --head feat --title Title --body-file P --reviewer bob --label x"),
    ('gh pr create --title T --body "${LLM_TEXT:?}\n\n(footer)"', "gh pr create --title T --body-file P"),
])
def test_supported_pr_shapes(cmd, rewritten):
    p = plan(cmd)
    assert p is not None and p.job == JOB_PR
    assert p.rewrite("P") == rewritten


def test_pr_options_are_read_for_the_job_input():
    p = plan(f"gh pr create -B main -H feat -t 'My title' -b {T}")
    assert (p.base, p.head, p.title) == ("main", "feat", "My title")
    assert plan(f"gh pr create --body {T}").title is None
    assert plan('gh pr create --title T --body "${LLM_TEXT:?}\n\nfooter"').final_text("G") == "G\n\nfooter\n"


# ---- everything else is refused with a code, or is none of our business ------------------------------------

@pytest.mark.parametrize("cmd,code", [
    (f"git add a && git commit -m {T}", "mutating_predecessor"),
    (f"git add -A; git commit -m {T}", "mutating_predecessor"),
    (f"git stash && git commit -m {T}", "mutating_predecessor"),
    (f"sed -i s/a/b/ f && git commit -m {T}", "mutating_predecessor"),
    (f"make test && git commit -m {T}", "mutating_predecessor"),
    (f"echo x > f && git commit -m {T}", "redirect"),
    (f"git commit -m {T} file.txt", "positional"),
    (f"git commit -m {T} -- file.txt", "positional"),
    (f"git commit --amend -m {T}", "option"),
    (f"git commit --allow-empty -m {T}", "option"),
    (f"git commit -F msg.txt -m {T}", "message_source"),
    (f"git commit -C HEAD -m {T}", "message_source"),
    (f"git commit -m {T} -m \"$(date)\"", "expansion"),
    ("git commit -m \"$(echo ${LLM_TEXT:?})\"", "token_in_expansion"),
    (f"git commit -m {T} -m {T}", "misplaced"),
    (f"git commit --author {T} -m x", "misplaced"),
    ("git commit -m'${LLM_TEXT:?}'", "option"),
    (f"git commit -m{T}", "option"),
    (f"git commit -amx {T}", "option"),
    (f"for f in a b; do git commit -m {T}; done", "structure"),
    (f"if true; then git commit -m {T}; fi", "structure"),
    (f"(git commit -m {T})", "structure"),
    (f"env X=1 git commit -m {T}", "wrapper"),
    (f"sudo git commit -m {T}", "wrapper"),
    (f"GIT_DIR=/x git commit -m {T}", "git_env"),
    (f"GIT_INDEX_FILE=/x git commit -m {T}", "git_env"),
    (f"git --git-dir=/x commit -m {T}", "git_dir"),
    (f"git --work-tree=/x commit -m {T}", "git_dir"),
    (f"cd $HOME/x && git commit -m {T}", "expansion"),
    (f"cd - && git commit -m {T}", "cd"),
    (f"cd && git commit -m {T}", "cd"),
    (f"git commit -m {T} > out.txt", "redirect"),
    (f"git commit -z -m {T}", "option"),
    (f"git log | git commit -m {T}", None),  # a pipe feeding git is fine: git log is neutral
    (f"gh pr create --fill --body {T}", "pr_option"),
    (f"gh pr create --web --body {T}", "pr_option"),
    (f"gh pr create -R o/r --body {T}", "pr_option"),
    (f"gh pr create --template t.md --body {T}", "pr_option"),
    (f"gh pr create --body {T} --body-file f.md", "pr_body"),
    (f"gh pr create --body-file x{T[1:-1]}y", "pr_body"),
    (f"gh pr create --title {T}", "misplaced"),
    (f"gh pr create {T}", "misplaced"),
    (f"gh pr create --body {T} extra", "positional"),
    (f"gh pr create --nonsense --body {T}", "option"),
    (f"git commit -m x && gh pr create --body {T}", "mutating_predecessor"),
    (f"gh pr merge 1 && gh pr create --body {T}", "mutating_predecessor"),
    (f"gh pr view 1 --json title && gh pr create --body {T}", None),
    (f"gh issue create --body {T}", "not_pr"),
    (f"git log -m {T}", "not_commit"),
])
def test_refusals_carry_a_code(cmd, code):
    if code is None:
        assert plan(cmd) is not None
        return
    with pytest.raises(Unsupported) as e:
        plan(cmd)
    assert e.value.code == code, (cmd, e.value.code, e.value.message)
    assert e.value.message and "\n" not in e.value.message[:1]


@pytest.mark.parametrize("cmd", [
    "git commit -m 'hand written'",
    "git status",
    f"echo {T}",
    f"printf '%s' {T} > notes.txt",
    "cat <<'EOF' > doc.md\ngit commit -m \"${LLM_TEXT:?}\"\nEOF",
    "bash -c 'git commit -m \"${LLM_TEXT:?}\"'",
    f"grep -r {T} src/",
    "git commit -m \"$(cat <<'EOF'\nfix: a\nEOF\n)\"",
])
def test_commands_that_are_not_a_placeholder_use_are_left_alone(cmd):
    assert plan(cmd) is None


def test_an_unreadable_command_is_a_scan_error_for_the_caller_to_report():
    with pytest.raises(ScanError):
        plan(f"git commit -m {T} 'unterminated")


def test_messages_name_the_command_to_run_first():
    with pytest.raises(Unsupported) as e:
        plan(f"git add a.py && git commit -m {T}")
    assert "git add" in e.value.message and "own command" in e.value.message and "commit" in e.value.message
    with pytest.raises(Unsupported) as e:
        plan(f"git commit -m x && gh pr create --body {T}")
    assert "pull request" in e.value.message


def test_the_rewrite_never_contains_model_text():
    p = plan(f"git commit -m {T} -m 'Co-Authored-By: X'")
    out = p.rewrite("/p/m.txt")
    for fragment in ("quotes", "$HOME", "`ticks`", "Body"):
        assert fragment not in out and fragment not in p.rewrite("/p/m.txt")
    assert out == "git commit -F /p/m.txt"


def test_a_path_that_needs_quoting_is_quoted():
    assert plan(f"git commit -m {T}").rewrite("/a b/c'd.txt") == "git commit -F " + shlex.quote("/a b/c'd.txt")


# ---- what real git and gh receive ---------------------------------------------------------------------------

SHELLS = [s for s in ("bash", "zsh") if shutil.which(s)]


def _run(shell: str, cmd: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    argv = [shell, "-c", cmd] if shell == "bash" else [shell, "-f", "-c", cmd]
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
           "HOME": str(tmp_path)}
    for a in (["init", "-q", "."], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *a], cwd=r, check=True, env=env)
    (r / "f.txt").write_text("1\n")
    subprocess.run(["git", "add", "f.txt"], cwd=r, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=r, check=True, env=env)
    (r / "f.txt").write_text("2\n")
    return r, env


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("cmd,expected", [
    (f"git commit -am {T}", GEN),
    (f"git commit -am {T} -m 'Co-Authored-By: X <x@y>' --no-verify", GEN + "\n\nCo-Authored-By: X <x@y>"),
    (f"git commit --all --message={T}", GEN),
    (f"git commit -a -m 'Lead' -m {T}", "Lead\n\n" + GEN),
])
def test_rewritten_commit_commands_commit_exactly_the_text(repo, shell, cmd, expected):
    r, env = repo
    p = plan(cmd.replace("git commit", f"git -C {shlex.quote(str(r))} commit", 1), cwd=str(r))
    assert p is not None and p.repo_dir == str(r)
    msg = r / "msg.txt"
    msg.write_text(p.final_text(GEN))
    out = _run(shell, p.rewrite(str(msg)), r, env)
    assert out.returncode == 0, out.stderr
    got = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=r, env=env, capture_output=True, text=True).stdout
    assert got.strip() == expected.strip()


@pytest.mark.parametrize("shell", SHELLS)
def test_the_placeholder_alone_aborts_the_command_in_every_shell(repo, shell):
    """The reason it is a variable: with no hook, nothing is committed."""
    r, env = repo
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, env=env, capture_output=True, text=True).stdout
    out = _run(shell, f"git commit -am {T} && echo AFTER", r, env)
    assert out.returncode != 0 and "AFTER" not in out.stdout and "LLM_TEXT" in out.stderr
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, env=env, capture_output=True, text=True).stdout == before
    out = _run(shell, 'gh pr create --body "${LLM_TEXT:?}\n\nfooter" || true; echo AFTER', r, env)
    assert "AFTER" not in out.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_rewritten_pr_commands_hand_gh_the_body_file(tmp_path, shell):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    gh = stub_dir / "gh"
    gh.write_text(textwrap.dedent('''\
        #!/bin/sh
        : > "$GH_OUT"
        for a in "$@"; do printf '%s\\n' "$a" >> "$GH_OUT"; done
        prev=""
        for a in "$@"; do
          if [ "$prev" = "--body-file" ]; then cat "$a" > "$GH_OUT.body"; fi
          prev="$a"
        done
        '''))
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}", "GH_OUT": str(tmp_path / "out")}
    cmd = 'gh pr create --title "T t" --body "${LLM_TEXT:?}\n\n(footer, with $ signs escaped \\$HOME)" --draft'
    p = plan(cmd, cwd=str(tmp_path))
    assert p is not None
    body = tmp_path / "body.txt"
    body.write_text(p.final_text(GEN))
    out = _run(shell, p.rewrite(str(body)), tmp_path, env)
    assert out.returncode == 0, out.stderr
    argv = (tmp_path / "out").read_text().splitlines()
    assert argv == ["pr", "create", "--title", "T t", "--body-file", str(body), "--draft"]
    assert (tmp_path / "out.body").read_text() == p.final_text(GEN)
    assert (tmp_path / "out.body").read_text().startswith(GEN)


def test_scan_of_the_rewritten_command_has_no_placeholder_and_the_same_shape():
    for cmd in (f"cd /r && git commit -am {T} -m 'x' && git push", f"git push && gh pr create --title t --body {T} --draft"):
        p = plan(cmd)
        out = p.rewrite("/p/m")
        assert len(scan_commands(out)) == len(scan_commands(cmd))
        assert not any(w.has_token for c in scan_commands(out) for w in c.argv)
