"""W7 follow-up to #825: deny-only hardening, round 2 (`cp` / `mv`, and environment assignments).

Two changes, and both only ADD denials (every line that `tests/test_w7_deny_only.py` holds as denied is still denied; the keep-cases
here are lines that bind or pass today and must go on doing so):

* (a) `cp` and `mv` are no longer "harmless readers": they change files, so a line that runs one next to a mention of the
  placeholder is refused and no longer passes with no decision.
* (b) a line that mentions the placeholder must not SET a variable that changes which program, directory, configuration or startup
  file a later command uses (`PATH`, `IFS`, `BASH_ENV`, `ENV`, ..., every `GIT_*` outside the six identity variables, `DYLD_*`,
  `LD_*`), in any form: bare (`PATH=x;`), in front of a command, `export` / `declare` / `env`, `+=`, `${PATH:=x}`, after the
  commit, on the unreadable path (judged on the text), bound or stray.
"""

from __future__ import annotations

import pytest

from llm_router.text_job_bind import plan as plan_mod
from llm_router.text_job_bind.plan import Unsupported, analyse, env_assignments_in_text, unreadable_line_is_harmless
from llm_router.text_job_bind.shellscan import ScanError

T = '"${LLM_TEXT:?}"'  # the placeholder as the model writes it
P = "'${LLM_TEXT:?}'"  # a stray mention
DIV = "echo \"${Y:-'}\""  # bash opens a quote here and other shells do not: the scanner refuses to read the line


def refusal(cmd: str) -> Unsupported | None:
    try:
        analyse(cmd, "/base")
    except Unsupported as u:
        return u
    return None


def denied(cmd: str) -> bool:
    """The engine's decision for a line that mentions the placeholder: a refusal, or an unreadable line that is not harmless."""
    try:
        analyse(cmd, "/base")
    except Unsupported:
        return True
    except ScanError:
        return not unreadable_line_is_harmless(cmd)
    return False


# ---- (a) cp and mv ----------------------------------------------------------------------------------------------------------

_FILE_CHANGING = ["cp a.py b.py", "mv a.py b.py", "cp -r d e", "mv -f a b", "cp /bin/sh x"]


@pytest.mark.parametrize("cmd", _FILE_CHANGING)
@pytest.mark.parametrize("shape", ["{c}; echo {P}", "echo {P} && {c}", "{c} && grep -n {P} notes.md", "cd /r && {c}; cat {P}"])
def test_a_line_that_changes_files_and_mentions_the_placeholder_elsewhere_is_refused(cmd, shape):
    line = shape.format(c=cmd, P=P)
    u = refusal(line)
    assert u is not None, line
    assert u.code == "placeholder_spelling"
    assert f"{cmd.split()[0]} (it changes files)" in u.message and "change files" in u.message and "command of their own" in u.message


@pytest.mark.parametrize("cmd", _FILE_CHANGING)
def test_the_same_on_a_line_the_scanner_cannot_read(cmd):
    line = f"{DIV}; {cmd}; echo {P}"
    with pytest.raises(ScanError):
        analyse(line, "/base")
    assert not unreadable_line_is_harmless(line), line


@pytest.mark.parametrize("name", ["cp", "mv"])
def test_cp_and_mv_are_off_the_harmless_list_and_the_readers_are_not(name):
    assert name not in plan_mod._HARMLESS and name in plan_mod._FILE_CHANGERS
    assert {"echo", "printf", "grep", "rg", "cat", "ls", "cd", "tee", "touch", "mkdir"} <= plan_mod._HARMLESS


@pytest.mark.parametrize("cmd", [
    f"echo {P}", f"grep -rn {P} docs", f"cat notes.md {P}", f"ls | grep {P}", f"cd /repo && grep -rn {P} docs",
    f"echo {P} | tee out.txt", f"printf '%s\\n' {P} | grep -c x", f"touch a.txt; echo {P}", f"mkdir d; echo {P}",
])
def test_the_readers_still_pass_with_no_decision(cmd):
    assert not denied(cmd), cmd


# ---- (b) environment assignments --------------------------------------------------------------------------------------------

#: name -> why the hook refuses it is in ``plan._ENV_EFFECT``; the rest is a family
_NAMES = [
    "PATH", "IFS", "BASH_ENV", "ENV",  # the owner's four
    "CDPATH", "HOME", "XDG_CONFIG_HOME", "ZDOTDIR", "SHELLOPTS", "BASHOPTS", "PS4", "EDITOR", "VISUAL",
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_EDITOR", "GIT_EXEC_PATH", "GIT_CONFIG_GLOBAL", "GIT_SSH_COMMAND",
    "GIT_NAMESPACE", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH",
]

#: every form of "this line sets @N@" next to a placeholder that is bound, or one that is stray
_FORMS = [
    "@N@=/x; git commit -m " + T,  # bare
    "@N@=/x git commit -m " + T,  # in front of the command
    "export @N@=/x; git commit -m " + T,
    "declare -x @N@=/x; git commit -m " + T,
    "env @N@=/x git commit -m " + T,
    "@N@+=:/x; git commit -m " + T,
    ": ${@N@:=/x}; git commit -m " + T,
    "git commit -m " + T + " && @N@=/x",  # after the command: no exception for position
    "cd /r && @N@=/x git commit -m " + T,
    "@N@=/x gh pr create --title t --body " + T,
    "@N@=/x; echo " + T,  # the placeholder in an echo: no decision on main, a refusal now
    "@N@=/x; echo " + P,  # a stray mention with no git on the line
    "export @N@=$X; echo " + P,  # a value that expands
    "export '@N@=/x'; echo " + P,  # a quoted assignment word
]


@pytest.mark.parametrize("n", _NAMES)
@pytest.mark.parametrize("form", _FORMS)
def test_a_line_that_mentions_the_placeholder_and_sets_the_variable_is_refused_with_the_assignment_named(n, form):
    line = form.replace("@N@", n)
    u = refusal(line)
    assert u is not None, line
    assert u.code == ("git_env" if n.startswith("GIT_") else "env_assignment"), (line, u.code)
    assert "also sets" in u.message and f"{n} (" in u.message, (line, u.message)
    # every refusal says what to do instead
    assert "Take the assignment out of this line" in u.message and "git -C <dir> commit" in u.message


@pytest.mark.parametrize("n", _NAMES)
def test_the_same_on_a_line_the_scanner_cannot_read_judged_on_its_text(n):
    for tail in ("; @N@=/x; echo " + P, "; export @N@=/x; echo " + P, "; @N@=/x git commit -m " + T, "; echo '@N@=/x' " + P,
                 "; @N@+=:/x; echo " + P, "; echo ${@N@:=/x} " + P):
        line = DIV + tail.replace("@N@", n)
        with pytest.raises(ScanError):
            analyse(line, "/base")
        assert not unreadable_line_is_harmless(line), line
        assert env_assignments_in_text(line) == [n], line


def test_an_assignment_inside_arithmetic_is_refused():
    line = "echo $((IFS=1)) " + P
    u = refusal(line)
    assert u is not None and u.code == "env_assignment" and "IFS (" in u.message
    assert env_assignments_in_text(DIV + "; echo $(( PATH = 1 )) " + P) == ["PATH"]


def test_several_assignments_are_all_named_up_to_three_and_the_first_decides_the_code():
    u = refusal("PATH=/x IFS=/ HOME=/y BASH_ENV=/z git commit -m " + T)
    assert u is not None and u.code == "env_assignment"
    assert "PATH (" in u.message and "IFS (" in u.message and "HOME (" in u.message and "BASH_ENV" not in u.message
    assert refusal("GIT_DIR=/x PATH=/y git commit -m " + T).code == "git_env"


# ---- the unreadable path reaches the model with the assignment named (engine level: tests/test_w7_engine.py) ------------------

def test_the_text_scan_ignores_names_that_only_end_in_a_listed_one():
    assert env_assignments_in_text("NODE_ENV=production RAILS_ENV=x MYPATH=1 XPATH+=2 LDX=1 DYLDX=1 GITX_DIR=1 path=1") == []
    assert env_assignments_in_text("-DPATH=1 --env=x") == []
    assert env_assignments_in_text("X=1 PATH=/a:/b") == ["PATH"]


# ---- what must go on working: nothing here is refused, and each was bound or passed before ----------------------------------

_BINDS = [
    'git commit -m "${LLM_TEXT:?}"',
    'gh pr create --title t --body "${LLM_TEXT:?}"',
    'cd /repo && git commit -m "${LLM_TEXT:?}"',
    'git -C /repo commit -m "${LLM_TEXT:?}"',
    'git -C /a -C b commit -am "${LLM_TEXT:?}"',
    'git status --short && git diff --stat; git commit -m "${LLM_TEXT:?}"',
    'git push -u origin HEAD && gh pr create --title t --body "${LLM_TEXT:?}"',
    # the identity variables, in front of the command and on their own
    'GIT_AUTHOR_NAME=a git commit -m "${LLM_TEXT:?}"',
    'GIT_COMMITTER_EMAIL=a@b.c GIT_AUTHOR_DATE=2026-01-01T00:00:00 git commit -m "${LLM_TEXT:?}"',
    'GIT_AUTHOR_NAME=a; git commit -m "${LLM_TEXT:?}"',
    # a variable that changes nothing the hook relies on
    'X=1; git commit -m "${LLM_TEXT:?}"', 'LANG=C git commit -m "${LLM_TEXT:?}"', 'TZ=UTC git commit -m "${LLM_TEXT:?}"',
    # a listed name that is read, not set; or that is data (a message, an argument) and not an assignment
    'echo $PATH; git commit -m "${LLM_TEXT:?}"', 'echo "$IFS" | wc -c && git commit -m "${LLM_TEXT:?}"',
    'git commit -m "${LLM_TEXT:?}\n\nNotes: run it with PATH=/x set."',
    'git commit -m "${LLM_TEXT:?}" -m "Refs: GIT_DIR=/x in the docs"',
    'gh pr create --title "Fix PATH=/x handling" --body "${LLM_TEXT:?}"',
]


@pytest.mark.parametrize("cmd", _BINDS)
def test_a_line_that_binds_today_still_binds(cmd):
    assert analyse(cmd, "/base") is not None, cmd


@pytest.mark.parametrize("n", sorted(plan_mod._GIT_ENV_OK))
def test_the_six_identity_variables_are_not_refused_in_any_form(n):
    for form in ("@N@=x git commit -m " + T, "@N@=x; git commit -m " + T, "git commit -m " + T + " && @N@=x"):
        assert analyse(form.replace("@N@", n), "/base") is not None, form


@pytest.mark.parametrize("cmd", [
    "PATH=/x git commit -m hello", "export PATH=/x; git commit -m hello", "IFS=/ gh pr create --title t --body body",
    "HOME=/x; git commit -am wip",
])
def test_a_hand_written_commit_that_never_mentions_the_placeholder_is_not_this_hooks_business(cmd):
    assert analyse(cmd, "/base") is None  # the engine does not even call analyse for it (no mention); the contract holds too


@pytest.mark.parametrize("cmd", [
    f"grep -n 'PATH=' notes.md {P}", f"echo \"PATH=/x\" {P}", f"echo 'export PATH=/x' >> NOTES.md; cat {P}",
    f"grep -rn 'GIT_DIR=' docs {P}", f"cat <<'EOF'\nPATH=/x\nuse {P} here\nEOF",
])
def test_a_listed_name_written_as_data_on_a_readable_line_is_not_an_assignment(cmd):
    assert not denied(cmd), cmd


def test_the_same_as_data_on_an_unreadable_line_is_refused_because_no_quote_is_trusted_there():
    """The price of judging a line on its text: `'PATH=...'` in a quote is refused. Stated, because it is the one place where the
    new rule over-denies; a readable line (above) does not."""
    line = DIV + f"; grep -n 'PATH=' notes.md {P}"
    with pytest.raises(ScanError):
        analyse(line, "/base")
    assert not unreadable_line_is_harmless(line)


def test_a_plain_stray_mention_is_still_judged_by_the_rule_of_825():
    for ok in (f"echo {P}", f"grep -n {P} notes.md"):
        assert not denied(ok)
    for bad in (f"sed -i 's/x/y/' f; echo {P}", f"git log -S{P}", f"make test X={P}"):
        assert denied(bad)


def test_git_add_on_the_same_line_is_still_refused_with_the_reason_the_deploy_smoke_test_reads():
    u = refusal(f"git add a.py && git commit -m {T}")
    assert u is not None and u.code == "mutating_predecessor" and "same line" in u.message


def test_the_refusal_table_is_complete():
    """Every listed name has a reason the model can read, and none of them is one of the six identity variables."""
    assert all(isinstance(v, str) and len(v) > 20 for v in plan_mod._ENV_EFFECT.values())
    assert not set(plan_mod._ENV_EFFECT) & plan_mod._GIT_ENV_OK
    assert {"PATH", "IFS", "BASH_ENV", "ENV"} <= set(plan_mod._ENV_EFFECT)
    assert plan_mod._env_effect("GIT_AUTHOR_NAME") is None and plan_mod._env_effect("GIT_DIR")
    assert plan_mod._env_effect("LD_PRELOAD") and plan_mod._env_effect("DYLD_INSERT_LIBRARIES") and plan_mod._env_effect("LANG") is None
