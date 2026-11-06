"""W7 follow-up (#825): deny-only hardening of the stray-mention rule.

A line that mentions the placeholder where the shell does not replace it is denied unless every command on it is a harmless
reader (the rule of #820, unchanged), and this change only ADDS denials to that rule: the adversarial reviews found, one after
another, shapes that passed and then committed the literal text or ran something, so nothing is relaxed. The tests here are

* the property that matters, as data: every line that main (37246b16) denies is still denied
  (``fixtures/w7_main_denied_lines.json``: the lines of the W7 tests and of the reviewers' case lists, and templates x stray
  spellings, that main denies; 725 lines);
* every shape the reviews found, as a denial, and the same refusals on the commit path;
* the lines that still pass, so the hardening is not a blanket refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_router.text_job_bind.plan import Unsupported, analyse, unreadable_line_is_harmless
from llm_router.text_job_bind.shellscan import ScanError

FIXTURE = Path(__file__).parent / "fixtures" / "w7_main_denied_lines.json"
P = "'${LLM_TEXT:?}'"  # a stray mention
GC = "git commit --allow-empty -m"


def denied(cmd: str) -> bool:
    """The engine's decision for a line that mentions the placeholder: a refusal, or a line the scanner cannot read that is not
    harmless. (A line that plans as a commit or a PR binds; a line that passes gets no decision.)"""
    try:
        analyse(cmd, "/base")
    except Unsupported:
        return True
    except ScanError:
        return not unreadable_line_is_harmless(cmd)
    return False


def test_every_line_main_denied_is_still_denied():
    lines = json.loads(FIXTURE.read_text())
    assert len(lines) >= 700, "the recorded set is empty or truncated: nothing would be checked"
    loosened = [c for c in lines if not denied(c)]
    assert loosened == []


def test_the_recorded_set_covers_each_kind_of_line_main_denies():
    """So the property above is not satisfied by a set of one kind: wrappers, interpreters, groups, git forms, unreadable lines."""
    lines = json.loads(FIXTURE.read_text())
    for needle in ("timeout 5", "python3 -c", "for f in", "git ci -m", "gh pr create", "G=git", "xargs", "bash -c", "| sh",
                   "<<EOF", "unterminated", "${Y:-'}"):
        assert any(needle in c for c in lines), needle


#: What the reviews found, in the form that passed (with ``P`` a stray mention).
_FOUND = [
    # arithmetic: $(( is a subshell, $[x] evaluates its operand, and an operand a[$(cmd)] runs
    f"echo {P}; echo $(({GC} {P}); echo done)", f"x='a[$'\"({GC} wip)\"']'; echo {P} $[x]",
    f"x='a[$'\"({GC} wip)\"']'; echo \"${{LLM_TEXT:?$[x]}}\"", f"echo $[1+2] {P}", f"echo \"$[1+2]\" {P}",
    "echo ${LLM_TEXT:?$(date)}", "echo ${LLM_TEXT:?`date`}", f"echo ${{LLM_TEXT?$HOME}} {P}",
    # printf -v assigns to a name that is evaluated (zsh runs a[$(cmd)])
    f"printf -v 'a[$'\"({GC} wip)\"']' x; echo {P}", f"printf -v x '%s' y; echo {P}", f"printf -vx '%s' y; echo {P}",
    f"O=-v; printf $O x y; echo {P}", f"O=-v; printf \"$O\" x y; echo {P}", f"printf '%d' \"$x\"; echo {P}",
    f"printf \"$FMT\" a; echo {P}",
    # indirect and zsh expansions evaluate a value
    f"y='a[$({GC} x)]'; echo ${{!y}} {P}", f"x='$({GC} x)'; echo ${{(e)x}} {P}", f"x='*(e:\"{GC} wip\":)'; echo $~x; echo {P}",
    f"echo ${{~x}} {P}", f"echo ${{y:x}} {P}", f"echo ${{a[x]}} {P}", f"echo ${{x:-$({GC} x)}} {P}",
    # an unquoted here-document expands its body
    f"cat <<EOF\n$({GC} {P})\nEOF", f"cat <<EOF\n`{GC} {P}`\nEOF", f"echo {P}; cat <<EOF\n$(date)\nEOF",
    # an option behind a variable, in a place a parser does not look: the value of the variable is the option
    f"O=--pre=sh; rg -g -e \"$O\" . s.txt; echo {P}", f"O=--pre=sh; rg -e -e \"$O\" . s.txt; echo {P}",
    f"O=--pre=sh; rg -n -g -e \"$O\" . s.txt; echo {P}", f"O=--pre=sh; rg -t -- \"$O\" . s.txt; echo {P}",
    f"O=--pre=sh; rg $O x f; echo {P}", f"O=--pre=sh; rg \"$O\" x f; echo {P}", f"O=--pre=sh; grep $O x f; echo {P}",
    f"O=-Osh; git grep --no-index $O commit s.txt; echo {P}", f"O=--compress-program=sh; echo x | sort -S 1 $O; echo {P}",
    f"O=--compress-program=sh; echo x | sort -S 1 \"$O\"; echo {P}", f"X=foo; grep -e \"$X\" f; echo {P}",
    f"rg --pre=sh x f; echo {P}", f"rg --pre sh x f; echo {P}", f"rg --hostname-bin sh x f; echo {P}",
    f"sort --compress-program=sh f; echo {P}", f"sort --comp=sh f; echo {P}", f"git grep -nO'sh' x; echo {P}",
    # a harmless name that is not that command: a path, a copy of the shell, a prefix variable, a pager, test and [
    f"cp /bin/sh ./echo; ./echo -c '{GC} x'; echo {P}", f"./echo hi {P}", f"/bin/echo hi {P}", f"LC_ALL=C grep -n x f; echo {P}",
    f"LESSOPEN='|{GC} x' less f {P}", f"less f {P}", f"more f {P}", f"[ -v 'a[$({GC} x)]' ]; echo {P}",
    f"test -v 'a[$({GC} x)]'; echo {P}", f"[ \"$x\" -eq 1 ]; echo {P}",
    # git at all, and what it can do besides commit
    f"git -c diff.external='{GC} x #' diff; echo {P}", f"git -c core.pager='{GC} x' --paginate log -1; echo {P}",
    f"git log --output=a.py -S{P}", f"git reflog expire --expire=now --all; echo {P}", f"git log -S{P}", f"gh pr view 1 --web; echo {P}",
    # a write that runs code later
    f"printf 'echo hi\\n' >> ~/.zshrc; echo {P}", f"echo x > .git/hooks/pre-commit; echo {P}", f"echo {P} | tee -a ~/.bashrc",
    f"cp f .git/config; echo {P}",
]


@pytest.mark.parametrize("cmd", _FOUND)
def test_every_shape_the_reviews_found_is_denied(cmd):
    assert denied(cmd), cmd


# ---- the same on the commit path: a line that binds a commit is refused when it also has one of these --------------------------

_COMMIT_PATH = [
    "$[x]", "${(e)x}", "${!y}", "$~x", "${~x}", "printf -v 'a[$(date)]' x", "printf -vx '%s' y",
]


@pytest.mark.parametrize("extra", _COMMIT_PATH)
def test_a_commit_line_with_an_expression_that_runs_code_is_refused(extra):
    ok = 'git commit -m "${LLM_TEXT:?}"'
    assert analyse(ok, "/base") is not None  # the control: it binds
    with pytest.raises(Unsupported) as e:
        analyse(f"{ok}; echo {extra}", "/base")
    assert e.value.code == "expansion_runs_code" and "Nothing was run" in e.value.message


def test_a_commit_line_with_an_unquoted_here_document_that_runs_code_is_refused_a_quoted_one_is_not():
    ok = 'git commit -m "${LLM_TEXT:?}"'
    with pytest.raises(Unsupported) as e:
        analyse(f"{ok}\ncat <<EOF\n$(date)\nEOF", "/base")
    assert e.value.code == "expansion_runs_code"
    with pytest.raises(Unsupported):
        analyse(f"{ok}\ncat <<EOF\n`date`\nEOF", "/base")
    assert analyse(f"{ok}\ncat <<'EOF'\n$(date)\nEOF", "/base") is not None  # quoted: the body is data
    assert analyse(f"{ok}\ncat <<EOF\nplain\nEOF", "/base") is not None


def test_a_placeholder_whose_message_expands_something_is_refused_the_plain_one_is_not():
    assert analyse('git commit -m "${LLM_TEXT:?}"', "/base") is not None
    for bad in ('git commit -m "${LLM_TEXT:?$[x]}"', 'git commit -m "${LLM_TEXT:?$(date)}"', 'git commit -m "${LLM_TEXT:?`date`}"'):
        with pytest.raises(Unsupported):
            analyse(bad, "/base")


# ---- the unreadable path: the same terms, judged on the text ------------------------------------------------------------------

_DIV = "echo \"${Y:-'}\""  # bash opens a quote here and other shells do not: the scanner refuses to read the line
_UNREADABLE = [
    "; X=' echo' git commit -m {P}", "; A='x echo' git commit -m {P}", "; A=\"x echo\" git commit -m {P}", "; A=$HOME git commit -m {P}",
    "; A=\\ echo git commit -m {P}", "; A=1 echo {P}", "; echo \"<<X\"\ngit commit -m {P}", "; echo '<<X'\ngit commit -m {P}",
    "; cat <<'EOF'\ngit commit -m {P}\nEOF", "; O=--pre=sh; rg $O x f; echo {P}", "; O=--pre=sh; rg -e -e \"$O\" x; echo {P}",
    "; O=--compress-program=sh; sort $O f; echo {P}", "; printf -v 'a[$(date)]' x; echo {P}", "; echo $[1+2] {P}",
    "; echo ${{LLM_TEXT:?$[x]}}", "; echo $~x {P}", "; ./echo hi {P}", "; less f {P}", "; [ -v x ] {P}",
    "; echo x > .git/hooks/pre-commit; echo {P}", "; git log -S{P}", "; echo `date` {P}", "; echo $(date) {P}",
]


@pytest.mark.parametrize("tail", _UNREADABLE)
def test_an_unreadable_line_with_any_of_these_is_denied(tail):
    cmd = _DIV + tail.format(P=P)
    with pytest.raises(ScanError):
        analyse(cmd, "/base")
    assert not unreadable_line_is_harmless(cmd), cmd


# ---- what still passes (the rule of #820, minus what is refused above) ----------------------------------------------------------

_STILL_PASSES = [
    f"echo {P}", f"echo {P} > notes.txt", f"grep -rn {P} docs", f"rg {P} src", f"rg -n --glob '*.md' {P} docs",
    f"cat notes.md {P}", f"echo \"$HOME\" {P}", "L=\"note: \\${LLM_TEXT:?} is the placeholder\"; printf '%s\\n' \"$L\"",
    "L=\"note: \\${LLM_TEXT:?} is the placeholder\"; printf '%s\\n' \"$L\" >> log.txt 2>&1", f"printf '%s\\n' {P} | grep -c x",
    f"cat <<'EOF'\nuse {P} in git commit\nEOF", f"ls | grep {P}", f"cd /repo && grep -rn {P} docs", f"echo {P} | tee out.txt",
    f"# {P}", f"true {P}",
]


@pytest.mark.parametrize("cmd", _STILL_PASSES)
def test_a_line_that_only_prints_or_searches_still_passes(cmd):
    assert not denied(cmd), cmd


@pytest.mark.parametrize("cmd", [_DIV + f" {P}", _DIV + f"; grep -n {P} notes.md", f"echo \"unterminated {P}"])
def test_an_unreadable_line_that_only_prints_or_searches_still_passes(cmd):
    with pytest.raises(ScanError):
        analyse(cmd, "/base")
    assert unreadable_line_is_harmless(cmd)


# ---- the message of a refusal that has no commit in it ----------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,name", [
    ("sed -i 's/x/${LLM_TEXT:?}/' notes.md", "sed"), ("awk '/${LLM_TEXT:?}/' notes.md", "awk"), (f"make test X={P}", "make"),
    (f"git log -S{P} --oneline", "git"), (f"less f {P}", "less"),
    (f"O=--pre=sh; rg \"$O\" x f; echo {P}", "rg with an expansion in its arguments"), (f"X=1 echo {P}", "a variable set in front"),
    (f"echo \"$[1+2]\" {P}", "an expansion"),
])
def test_a_refusal_for_a_line_with_no_commit_says_what_it_could_not_see_through_and_does_not_claim_a_commit(cmd, name):
    try:
        analyse(cmd, "/base")
    except Unsupported as u:
        msg = u.message
    else:
        pytest.fail("not refused")
    if "also has" in msg:
        assert name in msg and "literal text" not in msg and "printf" in msg
    else:
        assert "runs a command whatever else it does" in msg  # the unconditional refusals have their own wording


@pytest.mark.parametrize("cmd", [f"git commit -m {P}", "bash -c 'git commit -m \"${LLM_TEXT:?}\"'", f"echo x; gh pr create --body {P}"])
def test_a_line_that_may_commit_keeps_the_literal_text_message(cmd):
    with pytest.raises(Unsupported) as e:
        analyse(cmd, "/base")
    assert "literal text" in e.value.message and "also has" not in e.value.message
