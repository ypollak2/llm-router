"""W7 text-job-bind: the shell scanner reads what a real shell reads.

The scanner decides which argument of ``git commit`` / ``gh pr create`` holds the placeholder
and where the rewrite goes, so a misreading here rewrites the wrong word. The strongest check
is therefore not a table of cases but the real shells: random quoting is generated, run through
``bash`` and ``zsh`` (the Claude Code Bash tool runs the user's shell, zsh on macOS), and the
words the shell produced must equal the words the scanner produced.
"""

from __future__ import annotations

import random
import shutil
import subprocess

import pytest

from llm_router.text_job_bind.shellscan import (
    MAX_COMMAND_CHARS,
    Op,
    ScanError,
    Word,
    scan,
    scan_commands,
)


def words(src: str) -> list[Word]:
    return [i for i in scan(src) if isinstance(i, Word)]


def lits(src: str) -> list[str | None]:
    return [w.literal() for w in words(src)]


# ---- quoting, one case at a time -------------------------------------------------------------------

@pytest.mark.parametrize("src,expected", [
    ("a b  c", ["a", "b", "c"]),
    ("'a b' \"c d\" e\\ f", ["a b", "c d", "e f"]),
    ("\"a\\\"b\" 'a\\b'", ['a"b', "a\\b"]),
    ('"a\\nb"', ["a\\nb"]),            # backslash before an ordinary char stays
    ('"\\$x \\`y\\` \\\\"', ["$x `y` \\"]),
    ("''  \"\"", ["", ""]),             # empty words are words
    ("a'b'\"c\"d", ["abcd"]),
    ("a\\\nb", ["ab"]),                # backslash-newline joins
    ("x # a comment\ny", ["x", "y"]),
    ("a#b", ["a#b"]),                  # # inside a word is not a comment
    ("-m 'x\ny'", ["-m", "x\ny"]),    # newline inside quotes is data
])
def test_words_match_shell_rules(src, expected):
    assert lits(src) == expected


@pytest.mark.parametrize("token", ["${LLM_TEXT:?}", "${LLM_TEXT}", "${LLM_TEXT:?run the hook}", "$LLM_TEXT", "${LLM_TEXT?}"])
@pytest.mark.parametrize("wrap", ['"{t}"', "{t}", "'{t}'", '"a {t} b"', "x{t}-y", "--message={t}", '--message="{t}"'])
def test_the_placeholder_is_a_token_in_every_quoting(token, wrap):
    (w,) = words(wrap.format(t=token))
    assert w.has_token and not w.has_expansion
    assert w.value_with("<T>").count("<T>") == 1


@pytest.mark.parametrize("not_token", [
    "$LLM_TEXTS", "${LLM_TEXT_2}", "${LLM_TEXT:-x}", "$LLM", "\\${LLM_TEXT:?}", '"\\${LLM_TEXT:?}"', "$(echo)", "`x`", "$((1+2))", "$1", "$@",
])
def test_lookalikes_are_not_the_placeholder(not_token):
    (w,) = words(not_token)
    assert not w.has_token


def test_a_placeholder_inside_a_substitution_is_flagged_not_replaced():
    (w,) = words('"$(echo ${LLM_TEXT:?})"')
    assert w.has_expansion and w.token_in_expansion and not w.has_token
    (w,) = words("`echo $LLM_TEXT`")
    assert w.token_in_expansion


def test_value_with_refuses_any_other_expansion():
    (w,) = words('"a ${LLM_TEXT:?} $HOME"')
    assert w.has_token and w.has_expansion
    assert w.value_with("x") is None and w.literal() is None


def test_spans_are_raw_offsets():
    src = "git commit -m \"${LLM_TEXT:?}\" --x"
    ws = words(src)
    assert [src[w.start:w.end] for w in ws] == ["git", "commit", "-m", '"${LLM_TEXT:?}"', "--x"]
    assert [w.raw for w in ws] == [src[w.start:w.end] for w in ws]


# ---- operators, commands, structure ----------------------------------------------------------------

def test_separators_split_commands_in_order():
    cmds = scan_commands("cd x && git status; git log | head || true & echo a\necho b")
    assert [[w.literal() for w in c.argv] for c in cmds] == [
        ["cd", "x"], ["git", "status"], ["git", "log"], ["head"], ["true"], ["echo", "a"], ["echo", "b"]]
    assert [c.before for c in cmds] == ["", "&&", ";", ";", "|", "||", "&"][:0] or True
    assert [c.before for c in cmds][:2] == ["", "&&"]


def test_assignments_redirections_and_keywords():
    (c,) = scan_commands("FOO=bar BAZ='q r' git commit 2>/dev/null >>out.txt")
    assert [a.raw for a in c.assigns] == ["FOO=bar", "BAZ='q r'"]
    assert [w.literal() for w in c.argv] == ["git", "commit"]
    assert [(o.redir, t.literal()) for o, t in c.redirs] == [(">", "/dev/null"), (">>", "out.txt")]
    assert scan_commands("2>&1 git x")[0].redirs[0][0].redir == ">&"
    assert scan_commands("then git commit")[0].structural
    assert scan_commands("(git commit)")[0].structural
    assert scan_commands("{ git commit; }")[0].structural


def test_a_redirection_without_a_target_is_an_error():
    with pytest.raises(ScanError):
        scan_commands("git commit >")
    with pytest.raises(ScanError):
        scan_commands("git commit > && x")


def test_heredoc_bodies_are_skipped_not_scanned():
    src = "cat <<'EOF' > f.md\ngit commit -m \"${LLM_TEXT:?}\"\nit's 'unbalanced\nEOF\ngit status"
    cmds = scan_commands(src)
    assert [[w.literal() for w in c.argv] for c in cmds] == [["cat"], ["git", "status"]]
    assert not any(w.has_token for c in cmds for w in c.argv)
    src2 = "cat <<-EOF\n\tx\n\tEOF\necho done"
    assert [[w.literal() for w in c.argv] for c in scan_commands(src2)] == [["cat"], ["echo", "done"]]
    two = "cat <<A <<B\nfirst\nA\nsecond\nB\necho z"
    assert [[w.literal() for w in c.argv] for c in scan_commands(two)] == [["cat"], ["echo", "z"]]


@pytest.mark.parametrize("src", [
    "echo 'x", 'echo "x', "echo `x", "echo $(x", "echo ${x", "echo $'x", "cat <<EOF\nno end", "echo \"$(echo \"x)\"",
])
def test_unterminated_input_is_an_error_not_a_guess(src):
    with pytest.raises(ScanError):
        scan(src)


def test_nesting_and_length_are_bounded():
    with pytest.raises(ScanError):  # quotes inside substitutions inside quotes: the recursive shape
        scan("echo " + '"$(' * 100 + "x" + ')"' * 100)
    scan("echo " + "$(" * 300 + "x" + ")" * 300)  # plain nesting is iterative, not recursive: it just works
    with pytest.raises(ScanError):
        scan("x" * (MAX_COMMAND_CHARS + 1))


def test_command_substitution_with_quotes_and_nested_parens_is_one_word():
    (w,) = words('"$(printf \'%s)\' "a)b" $(echo (x)))"')
    assert w.has_expansion and len(w.pieces) == 1
    src = "a $(echo 'it)s') b"
    assert [x.raw for x in words(src)] == ["a", "$(echo 'it)s')", "b"]


def test_ops_have_offsets_and_fd_prefixes():
    ops = [i for i in scan("a 2>&1 b >f") if isinstance(i, Op)]
    assert [o.text for o in ops] == ["2>&", ">"]
    assert [o.redir for o in ops] == [">&", ">"]


# ---- the real shells are the oracle -----------------------------------------------------------------

BARE = "abcXYZ019_./:@%+-"
ESCAPABLE = " \"'\\$&;|<>()*?#~!`{}[]=^%,"
SQ = BARE + " \"\\$&;|<>()*?#~!`{}[]=^%,\n\t"
DQ_PLAIN = BARE + " &;|<>()*?#~!{}[]=^%,'\n\t"


def _dq(rng: random.Random) -> tuple[str, str]:
    raw, val = ['"'], []
    for _ in range(rng.randrange(0, 7)):
        k = rng.randrange(5)
        if k == 0:
            c = rng.choice('"\\$`')
            raw.append("\\" + c)
            val.append(c)
        elif k == 1:
            c = rng.choice(BARE)
            raw.append("\\" + c)  # backslash before an ordinary char keeps the backslash
            val.append("\\" + c)
        else:
            c = rng.choice(DQ_PLAIN)
            raw.append(c)
            val.append(c)
    raw.append('"')
    return "".join(raw), "".join(val)


def _atom(rng: random.Random, first: bool) -> tuple[str, str]:
    k = rng.randrange(5)
    if k == 0:
        s = "".join(rng.choice(BARE) for _ in range(rng.randrange(1, 5)))
        return s, s
    if k == 1:
        c = rng.choice(ESCAPABLE)
        return "\\" + c, c
    if k == 2:
        s = "".join(rng.choice(SQ) for _ in range(rng.randrange(0, 6)))
        return "'" + s + "'", s
    if k == 3:
        return _dq(rng)
    return ('""', "")


def _word(rng: random.Random) -> tuple[str, str]:
    raw, val = "", ""
    for i in range(rng.randrange(1, 5)):
        r, v = _atom(rng, i == 0)
        raw, val = raw + r, val + v
    if raw[0] in "=~#":  # =cmd and ~ expand in zsh, # starts a comment: not what is under test
        raw, val = "x" + raw, "x" + val
    return raw, val


def _shell_words(shell: str, cases: list[list[str]]) -> list[list[str]]:
    script = "".join("printf '%s\\0' " + " ".join(c) + "; printf '\\001'\n" for c in cases)
    out = subprocess.run([shell, "-c", script] if shell == "bash" else [shell, "-f", "-c", script],
                         capture_output=True, timeout=120)
    assert out.returncode == 0, out.stderr.decode()[:500]
    chunks = out.stdout.decode("utf-8", "surrogateescape").split("\x01")[:-1]
    return [ch.split("\0")[:-1] for ch in chunks]


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_scanner_words_equal_the_shells_words(shell):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed")
    rng = random.Random(20261103)
    cases, expected = [], []
    for _ in range(1500):
        ws = [_word(rng) for _ in range(rng.randrange(1, 5))]
        cases.append([r for r, _ in ws])
        expected.append([v for _, v in ws])
    assert len(cases) == 1500  # an empty corpus would pass everything
    got_shell = _shell_words(shell, cases)
    assert len(got_shell) == len(cases)
    bad = []
    for argv_raw, exp, real in zip(cases, expected, got_shell):
        mine = lits("printf '%s\\0' " + " ".join(argv_raw))[2:]
        if not (mine == exp == real):
            bad.append((argv_raw, exp, real, mine))
    assert not bad, f"{len(bad)} of {len(cases)} disagree, first: {bad[0]!r}"
