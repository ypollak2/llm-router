"""Read a Bash command line and decide whether, and how, its placeholder can be bound.

``analyse`` answers: does this command put ``"${LLM_TEXT:?}"`` where a commit message or a PR
body goes, which repository does it act on, and is that repository, at the moment the hook
runs, in the state the command will find it in? The last part is why predecessors are checked:
the checked text is built from the repository as it is BEFORE the command runs, so a
``git add`` earlier on the same line would make it stale. Every other shape that cannot be
proven safe is refused with a message that says what to do instead. Nothing is guessed.

The rewrite never puts model text in the command. The text goes to a private file and the
command gets ``-F <file>`` (``git commit``) or ``--body-file <file>`` (``gh pr create``), so there
is nothing to quote, nothing to escape and no way for the text to be read as shell.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field

from llm_router.text_job_bind.shellscan import MAX_COMMAND_CHARS, MENTION, Command, Piece, ScanError, Word, scan_commands

JOB_COMMIT = "commit_message"
JOB_PR = "pr_description"

#: A ``git commit`` / ``gh pr create`` anywhere in the text. A regex, not the scanner: it must work on any
#: command the model writes, including ones the scanner cannot read.
COMMIT_CMD = re.compile(r"(?<![\w-])git\s+(?:(?:-C|-c)\s+\S+\s+|--[\w-]+(?:=\S+)?\s+)*commit(?![\w-])")
PR_CMD = re.compile(r"(?<![\w-])gh\s+pr\s+create(?![\w-])")

STRAY_MESSAGE = (
    "LLM_TEXT is written where bash would not replace it with the checked text (single quotes, after a "
    "backslash, in a here-document or a comment, as ${LLM_TEXT:-...}, or without the :? guard), so it would "
    'be committed or posted as literal text. Write the placeholder exactly as "${LLM_TEXT:?}" as the value of '
    "-m (git commit) or --body (gh pr create). To commit text that itself mentions it, write the message to a "
    "file and run git commit -F <file>."
)

#: Env assignments that cannot change which repository ``git`` acts on or which index it reads.
_GIT_ENV_OK = frozenset({
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
})

#: Commands that read or print and change nothing a diff or a commit list depends on.
_NEUTRAL_COMMANDS = frozenset({
    "pwd", "echo", "printf", "true", "false", ":", "test", "[", "ls", "cat", "head", "tail", "wc",
    "grep", "which", "sleep", "date", "whoami",
})
_NEUTRAL_GIT = frozenset({
    "status", "log", "diff", "show", "rev-parse", "rev-list", "branch", "remote", "config", "fetch",
    "push", "ls-files", "ls-tree", "cat-file", "show-ref", "for-each-ref", "merge-base", "var",
    "shortlog", "blame", "grep", "check-ignore", "diff-index", "diff-tree", "name-rev", "describe",
})
_NEUTRAL_GH = frozenset({
    ("pr", "view"), ("pr", "list"), ("pr", "status"), ("pr", "checks"), ("pr", "diff"),
    ("auth", "status"), ("repo", "view"),
})

#: ``git`` global options that take no value and do not move the repository.
_GIT_GLOBAL_FLAGS = frozenset({
    "--no-pager", "-P", "--paginate", "-p", "--no-optional-locks", "--no-replace-objects",
    "--literal-pathspecs", "--no-literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
    "--icase-pathspecs",
})
_GIT_GLOBAL_MOVES = ("--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path", "--bare")

_COMMIT_SPEC = {
    "long_flags": frozenset({
        "all", "no-verify", "signoff", "quiet", "verbose", "no-gpg-sign", "gpg-sign", "no-status",
        "status", "reset-author", "no-edit",
    }),
    "long_values": frozenset({
        "message", "file", "trailer", "author", "date", "cleanup", "reuse-message",
        "reedit-message", "template", "fixup", "squash",
    }),
    "short_flags": frozenset("asnqvS"),
    "short_values": frozenset("mFCct"),
    "where": "git commit",
}
#: Message sources that cannot be combined with a generated message.
_COMMIT_CONFLICTS = frozenset({"file", "F", "reuse-message", "C", "reedit-message", "c", "template", "t",
                               "fixup", "squash"})

_PR_SPEC = {
    "long_flags": frozenset({
        "draft", "fill", "fill-first", "fill-verbose", "web", "no-maintainer-edit", "dry-run", "editor",
    }),
    "long_values": frozenset({
        "title", "body", "body-file", "base", "head", "repo", "assignee", "label", "milestone",
        "project", "reviewer", "template", "recover",
    }),
    "short_flags": frozenset("dfwe"),
    "short_values": frozenset("tbFBHRalmprT"),
    "where": "gh pr create",
}
_PR_REFUSED = frozenset({"fill", "fill-first", "fill-verbose", "web", "editor", "template", "repo",
                         "recover", "f", "w", "e", "T", "R"})


class Unsupported(Exception):
    """The command cannot be bound. ``code`` is a short stable reason for the ledger; ``message``
    is what the model is told (plain text, says what to do instead)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Opt:
    """One option read from an argument list."""

    word: Word  # the option word (for ``--name=value`` it also carries the value)
    name: str  # long name without dashes, or the short letter
    value: Word | None  # None for a flag
    eq: bool = False  # ``--name=value`` in a single word
    prefix: str = ""  # short cluster: the letters before this one (``a`` for ``-am``)
    long: bool = False


@dataclass
class Edit:
    start: int
    end: int
    text: str


@dataclass
class Plan:
    job: str
    repo_dir: str
    command: str
    messages: list[Opt] = field(default_factory=list)  # message options (commit) / the body option (PR)
    all_tracked: bool = False
    base: str | None = None
    head: str | None = None
    title: str | None = None
    index: int = 0  # which command of the line holds the placeholder
    flags: frozenset[str] = frozenset()  # the valueless options of that command (-a, --no-verify, ...)

    def final_text(self, generated: str) -> str:
        """The text the file holds: the generated text where the placeholder was and every other
        paragraph (trailers, a footer) exactly as the model wrote it."""
        parts = []
        for o in self.messages:
            assert o.value is not None
            v = o.value.value_with(generated)
            assert v is not None
            parts.append(v.strip("\n").rstrip())
        return "\n\n".join(p for p in parts if p) + "\n"

    def rewrite(self, path: str) -> str:
        """The command with the message source replaced by the file at ``path``."""
        q = shlex.quote(path)
        first, rest = self.messages[0], self.messages[1:]
        edits: list[Edit] = []
        if self.job == JOB_COMMIT:
            if first.eq:
                edits.append(Edit(first.word.start, first.word.end, f"--file={q}"))
            else:
                assert first.value is not None
                if first.long:
                    flag = "--file"
                else:
                    flag = f"-{first.prefix} -F" if first.prefix else "-F"
                edits.append(Edit(first.word.start, first.word.end, flag))
                edits.append(Edit(first.value.start, first.value.end, q))
            for extra in rest:
                # An extra message option is folded into the file, but the flags that share its short
                # cluster (-a in -am, -s in -sm) are not messages and must stay: -am "x" becomes -a.
                if not extra.eq and not extra.long and extra.prefix:
                    assert extra.value is not None
                    edits.append(Edit(extra.word.start, extra.word.end, f"-{extra.prefix}"))
                    edits.append(Edit(_gap_start(self.command, extra.value.start), extra.value.end, ""))
                    continue
                last = extra.word if extra.eq else extra.value
                assert last is not None
                edits.append(Edit(_gap_start(self.command, extra.word.start), last.end, ""))
        else:
            if first.eq:
                edits.append(Edit(first.word.start, first.word.end, f"--body-file={q}"))
            else:
                assert first.value is not None
                # -db "${LLM_TEXT:?}": the -d (--draft) shares the cluster and must survive the rewrite
                flag = f"-{first.prefix} --body-file" if first.prefix and not first.long else "--body-file"
                edits.append(Edit(first.word.start, first.word.end, flag))
                edits.append(Edit(first.value.start, first.value.end, q))
        out = self.command
        for e in sorted(edits, key=lambda e: e.start, reverse=True):
            out = out[:e.start] + e.text + out[e.end:]
        return out


def _gap_start(src: str, pos: int) -> int:
    while pos > 0 and src[pos - 1] in " \t":
        pos -= 1
    return pos


def _short(c: Command, n: int = 40) -> str:
    return " ".join(w.raw for w in c.argv[:2])[:n] or "that command"


def _lit(w: Word, what: str) -> str:
    v = w.literal()
    if v is None:
        raise Unsupported("expansion", f"{what} uses a shell expansion; write it as plain text.")
    return v


def _resolve(cwd: str, w: Word, what: str) -> str:
    target = _lit(w, what)
    if w.raw[:1] == "~":  # an unquoted leading tilde
        target = os.path.expanduser(target)
    return os.path.normpath(target if os.path.isabs(target) else os.path.join(cwd, target))


def _check_redirs(c: Command) -> None:
    for op, target in c.redirs:
        r, t = op.redir, target.literal()
        if r in (">&", "<&") and t is not None and t.isdigit():
            continue
        if t == "/dev/null" and r in (">", ">>", "<", "&>", "&>>"):
            continue
        raise Unsupported(
            "redirect", f"`{_short(c)}` redirects output or input; run it as its own command first."
        )


def _check_assigns(c: Command) -> None:
    for a in c.assigns:
        name = a.raw.split("=", 1)[0]
        if name.startswith("GIT_") and name not in _GIT_ENV_OK:
            raise Unsupported(
                "git_env", f"{name} changes which repository git reads; run the command without it."
            )


def _git_subcommand(args: list[Word]) -> str | None:
    """The git subcommand after its global options, ``None`` if it cannot be read plainly."""
    i = 0
    while i < len(args):
        a = args[i].literal()
        if a is None:
            return None
        if a in ("-C", "-c"):
            i += 2
        elif a in _GIT_GLOBAL_FLAGS:
            i += 1
        elif a.startswith("-"):
            return None
        else:
            return a
    return None


def _neutral(c: Command, cwd: str, job: str) -> str:
    """The working directory after ``c``; raises if ``c`` could change what the job reads."""
    if c.structural:
        raise Unsupported(
            "structure",
            "The line uses grouping or a shell keyword before the placeholder command; "
            "run the placeholder command on its own.",
        )
    _check_redirs(c)
    _check_assigns(c)
    if not c.argv:
        return cwd
    name0 = c.argv[0].literal()
    if name0 is None:
        raise Unsupported(
            "unknown_predecessor",
            "A command before the placeholder command is not plain text; run the placeholder command alone.",
        )
    name = os.path.basename(name0)
    if name == "cd":
        args = [w for w in c.argv[1:] if w.literal() not in ("--", "-P", "-L", "-e", "-@")]
        if len(args) != 1 or args[0].literal() == "-":
            raise Unsupported("cd", "`cd` before the placeholder command needs exactly one plain directory.")
        return _resolve(cwd, args[0], "`cd` target")
    if name in _NEUTRAL_COMMANDS:
        return cwd
    if name == "git" and _git_subcommand(c.argv[1:]) in _NEUTRAL_GIT:
        return cwd
    if name == "gh" and len(c.argv) >= 3 and (c.argv[1].literal(), c.argv[2].literal()) in _NEUTRAL_GH:
        return cwd
    raise Unsupported(
        "mutating_predecessor",
        f"`{_short(c)}` runs before the placeholder command on the same line, so the checked text "
        "would be built from a repository that has not changed yet. Run it as its own command "
        f"first, then run the {'commit' if job == JOB_COMMIT else 'pull request'} command alone.",
    )


def _option_parts(w: Word) -> tuple[str, Word | None] | None:
    """``(name text, attached value)`` for an option word, ``None`` for a positional word."""
    first = w.pieces[0] if w.pieces else None
    if first is None or first.kind != "lit" or not first.text.startswith("-") or first.text == "-":
        return None
    t = first.text
    if t.startswith("--"):
        if "=" in t:
            name, _, rest = t.partition("=")
            pieces = ((Piece("lit", rest),) if rest else ()) + w.pieces[1:]
            n = len(name) + 1
            return name, Word(w.start + n, w.end, w.raw[n:], pieces)
        if len(w.pieces) == 1:
            return t, None
        raise Unsupported("option", f"Write {t} and its value as separate words.")
    if len(w.pieces) == 1:
        return t, None
    raise Unsupported("option", f"Write {t} and its value as separate words, not attached.")


def _read_options(argv: list[Word], i: int, spec: dict) -> list[Opt]:
    where = spec["where"]
    out: list[Opt] = []
    while i < len(argv):
        w = argv[i]
        parts = _option_parts(w)
        if parts is None:
            if w.has_token:
                raise Unsupported("misplaced", "Pass the placeholder as the value of -m (git commit) or --body (gh pr create).")
            raise Unsupported("positional", f"`{where}` with a path or other positional argument is not supported with the placeholder.")
        text, attached = parts
        if text == "--":
            raise Unsupported("positional", f"`{where} -- <paths>` is not supported with the placeholder.")
        if text.startswith("--"):
            name = text[2:]
            if name in spec["long_values"]:
                if attached is not None:
                    out.append(Opt(w, name, attached, eq=True, long=True))
                    i += 1
                else:
                    if i + 1 >= len(argv):
                        raise Unsupported("option_value", f"{text} needs a value.")
                    out.append(Opt(w, name, argv[i + 1], long=True))
                    i += 2
            elif name in spec["long_flags"] and attached is None:
                out.append(Opt(w, name, None, long=True))
                i += 1
            else:
                raise Unsupported("option", f"`{where} --{name}` is not supported with the placeholder.")
            continue
        letters = text[1:]
        took_value = False
        for k, ch in enumerate(letters):
            if ch in spec["short_flags"]:
                out.append(Opt(w, ch, None, prefix=letters[:k]))
            elif ch in spec["short_values"]:
                if k != len(letters) - 1:
                    raise Unsupported("option", f"Write -{ch} with its value as the next word, not attached.")
                if i + 1 >= len(argv):
                    raise Unsupported("option_value", f"-{ch} needs a value.")
                out.append(Opt(w, ch, argv[i + 1], prefix=letters[:k]))
                took_value = True
            else:
                raise Unsupported("option", f"`{where} -{ch}` is not supported with the placeholder.")
        i += 2 if took_value else 1
    return out


def _git_dirs(argv: list[Word], cwd: str) -> tuple[str, int]:
    """Apply ``git -C <dir>`` globals to ``cwd``; return it and the index of the subcommand."""
    i = 1
    while i < len(argv):
        a = _lit(argv[i], "a git option")
        if a == "-C":
            if i + 1 >= len(argv):
                raise Unsupported("git_dir", "`git -C` needs a directory.")
            cwd = _resolve(cwd, argv[i + 1], "`git -C` directory")
            i += 2
        elif a == "-c":
            i += 2
        elif a in _GIT_GLOBAL_FLAGS:
            i += 1
        elif a.startswith(_GIT_GLOBAL_MOVES):
            raise Unsupported("git_dir", f"{a.split('=')[0]} moves the repository; run the commit without it.")
        elif a.startswith("-"):
            raise Unsupported("option", f"git option {a.split('=')[0]} is not supported with the placeholder.")
        else:
            return cwd, i
    raise Unsupported("not_commit", "No git subcommand found.")


def _plan_commit(command: str, argv: list[Word], cwd: str) -> Plan:
    repo, sub = _git_dirs(argv, cwd)
    if _lit(argv[sub], "the git subcommand") != "commit":
        raise Unsupported("not_commit", "The placeholder is only supported as the message of `git commit`.")
    messages: list[Opt] = []
    all_tracked = False
    for o in _read_options(argv, sub + 1, _COMMIT_SPEC):
        if o.name in ("m", "message"):
            messages.append(o)
        elif o.name in _COMMIT_CONFLICTS:
            raise Unsupported("message_source", f"`git commit -{'-' if o.long else ''}{o.name}` conflicts with the placeholder message.")
        elif o.name in ("a", "all"):
            all_tracked = True
    return Plan(JOB_COMMIT, repo, command, messages=messages, all_tracked=all_tracked)


def _plan_pr(command: str, argv: list[Word], cwd: str) -> Plan:
    if len(argv) < 3 or argv[1].literal() != "pr" or argv[2].literal() != "create":
        raise Unsupported("not_pr", "The placeholder is only supported as the body of `gh pr create`.")
    plan = Plan(JOB_PR, cwd, command)
    for o in _read_options(argv, 3, _PR_SPEC):
        if o.name in _PR_REFUSED:
            raise Unsupported("pr_option", f"`gh pr create {'--' if o.long else '-'}{o.name}` is not supported with the placeholder.")
        if o.name in ("b", "body", "F", "body-file"):
            plan.messages.append(o)
        elif o.name in ("B", "base"):
            plan.base = o.value.literal() if o.value is not None else None
        elif o.name in ("H", "head"):
            plan.head = o.value.literal() if o.value is not None else None
        elif o.name in ("t", "title"):
            plan.title = o.value.literal() if o.value is not None else None
    return plan


def _validate(plan: Plan, job_cmd: Command) -> None:
    where = "-m (git commit) or --body (gh pr create)"
    held = {id(o.value) for o in plan.messages if o.value is not None}
    for w in job_cmd.argv:
        if w.has_token and id(w) not in held and not any(o.word is w and o.eq for o in plan.messages):
            raise Unsupported("misplaced", f"Pass the placeholder as the value of {where}.")
    if not plan.messages:
        raise Unsupported("misplaced", f"Pass the placeholder as the value of {where}.")
    if sum(1 for o in plan.messages if o.value is not None and o.value.has_token) != 1:
        raise Unsupported("misplaced", "Use the placeholder in exactly one message or body option.")
    for o in plan.messages:
        assert o.value is not None
        if o.value.has_expansion:
            raise Unsupported("expansion", "The message or body uses a shell expansion; write it as plain text.")
    if plan.job == JOB_PR:
        if len(plan.messages) != 1:
            raise Unsupported("pr_body", "Give gh pr create one body option, not several.")
        o = plan.messages[0]
        if o.name in ("F", "body-file"):
            v = o.value
            if v is None or not (len(v.pieces) == 1 and v.pieces[0].kind == "tok"):
                raise Unsupported("pr_body", "With --body-file the value must be exactly the placeholder.")


#: Commands that read or print and never start another program with their arguments. A mention of the placeholder
#: that sits only in these (a `grep`, an `echo`, a `cat` with a here-document) cannot be delivered to git. NOT on the list:
#: ``awk`` (``system()``) and ``sed`` (the ``e`` command), which can run a program built from their own arguments.
_HARMLESS = frozenset({
    "echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "cat", "head", "tail", "wc", "sort", "uniq", "cut", "tr",
    "ls", "tee", "touch", "cp", "mv", "cd", "mkdir", "date", "true", "false", "test", "[", "pwd", "which", "whoami", "sleep",
    "basename", "dirname", "jq", "diff", "cmp", "less", "more", "file", "stat", "du", "df",
})


def _substitutes(w: Word) -> bool:
    """Does the word run a command (``$(...)`` or backticks, in a plain word or inside double quotes)?"""
    return any(p.kind == "exp" and p.text.startswith(("$(", "`")) for p in w.pieces)


def _runs_job_tool(cmds: list[Command], command: str) -> bool:
    """May this line run anything but a harmless reader? Deny-by-default: ``False`` only when EVERY command on the line is
    on the :data:`_HARMLESS` list, written with a plain name, outside any group, loop or keyword, and no word of the line
    (nor an assignment or a redirection target) runs a command with ``$(...)`` or backticks.

    So ``git``, ``gh`` however quoted, a name only known at run time (``$G commit``, ``${GIT:-git} commit``), a wrapper
    (``env``, ``timeout``, ``xargs``, ``bash -c``, ``exec "$G"``), an interpreter reading a here-document, a build tool, a
    loop or a group, ``awk`` and ``sed`` are all "may run git". The decision does not look for the word ``git``: a name
    held in a variable (``G=git; timeout 5 $G commit``) or built from quotes has none to find.

    A command that only QUOTES the placeholder next to ``git commit`` (a ``grep``, an ``echo``, a ``cat`` with a
    here-document) is not one."""
    for c in cmds:
        if any(_substitutes(w) for w in (*c.assigns, *c.argv, *(t for _o, t in c.redirs))):
            return True
        if c.structural:
            return True
        if not c.argv:
            continue  # bare assignments
        name = c.argv[0].literal()
        if name is None or os.path.basename(name) not in _HARMLESS:
            return True
    return False


_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?[ \t]*(['\"]?)([A-Za-z_]\w*)\1([^\n]*)\n(.*?)(?:\n[ \t]*\2[ \t]*(?=\n|\Z)|\Z)", re.S)
_COMMAND_START = re.compile(r"\$\(|[;&|()`\n]")
_LEADING = frozenset({"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until", "time"})
_ASSIGN_WORD = re.compile(r"[A-Za-z_]\w*=\S*$")


def _without_heredoc_bodies(text: str) -> str:
    """``text`` with the body of every here-document removed (it is data for the command that owns it), unless the body is
    expanded (an unquoted delimiter) and holds ``$(...)`` or a backtick: that runs, so it stays and is judged."""
    def drop(m: re.Match) -> str:
        if not m.group(1) and ("$(" in m.group(4) or "`" in m.group(4)):
            return m.group(0)
        return "<<" + m.group(2) + m.group(3)

    return _HEREDOC.sub(drop, text)


def unreadable_line_is_harmless(command: str) -> bool:
    """May a line that mentions the placeholder but that the scanner cannot read be left to the shell?

    Only when EVERY command in it is a harmless reader (the :data:`_HARMLESS` list: ``echo``, ``printf``, ``grep``,
    ``cat`` ...). There are no scanned words to go by, so this reads the text and over-approximates: every ``;``, ``&``,
    ``|``, parenthesis, backtick, ``$(`` and newline starts a command, quotes or not, and the first word of each (quotes
    and backslashes taken out: ``g'i't`` is ``git``) must be exactly a harmless name. A name only known at run time
    (``$G``), ``awk``, ``sed``, an interpreter, a shell that a pipe feeds, ``git`` and ``gh`` all fail it, with or without
    the words ``git commit`` in the line. The body of a here-document is data for its command and is not judged (but
    the command is: ``cat <<EOF`` passes, ``sh <<EOF`` and ``python3 - <<EOF`` do not)."""
    if len(command) > MAX_COMMAND_CHARS:
        return False
    for segment in _COMMAND_START.split(_without_heredoc_bodies(command)):
        words = segment.split()
        while words and (words[0] in _LEADING or _ASSIGN_WORD.match(words[0])):
            words.pop(0)
        if not words:
            continue
        name = words[0].replace("'", "").replace('"', "").replace("\\", "")
        if os.path.basename(name) not in _HARMLESS:
            return False
    return True


#: Keywords that open a loop, a conditional or a brace group; their closers; and the two prefixes that still run the
#: command after them (``time git commit``, ``! git commit``).
_OPENERS = frozenset({"if", "for", "while", "until", "case", "select", "{"})
_CLOSERS = frozenset({"fi", "done", "esac", "}"})
_PREFIXES = frozenset({"!", "time"})


def executed_job(command: str) -> tuple[str | None, bool]:
    """``(job, uses_the_placeholder)`` for a Bash command that RUNS ``git commit`` or ``gh pr create`` DIRECTLY;
    ``(None, False)`` for one that only mentions them (an ``echo``, a ``grep``, a here-document, a doc) or that runs
    them through something else (``env``, ``bash -c``, ``timeout``, a variable holding the name).

    For the adoption count over transcripts. Only a TOP-LEVEL command counts: the model ran it for certain. One inside a
    loop, a conditional, a brace group, a subshell or a function body (``for x in; do git commit -m x; done`` never runs
    it) is unknown, and unknown is not executed. A line the scanner cannot read counts for nothing: a quote that never
    closes is not a commit. When a line has both a commit and a PR command, the one that holds the placeholder is the
    one reported."""
    try:
        cmds = scan_commands(command)
    except ScanError:
        return None, False
    found: list[tuple[str, bool]] = []
    depth = 0
    for c in cmds:
        argv = list(c.argv)
        first = argv[0].literal() if argv else None
        if first in _OPENERS:
            depth += 1
            continue
        if first in _CLOSERS:
            if depth == 0:
                break  # a closer with no opener: the line is not what it looks like
            depth -= 1
            continue
        if depth:
            continue
        if c.structural and first not in _PREFIXES:
            break  # ``(`` ``)`` grouping, ``f() {``, ``function``, a stray ``then``: from here on nothing is known
        while argv and argv[0].literal() in _PREFIXES:
            argv.pop(0)
        if not argv:
            continue
        name = argv[0].literal()
        base = os.path.basename(name) if name is not None else ""
        if base == "git" and _git_subcommand(argv[1:]) == "commit":
            found.append((JOB_COMMIT, any(w.has_token for w in argv)))
        elif base == "gh" and len(argv) >= 3 and (argv[1].literal(), argv[2].literal()) == ("pr", "create"):
            found.append((JOB_PR, any(w.has_token for w in argv)))
    if not found:
        return None, False
    return next((f for f in found if f[1]), found[0])


def option_flags(job: str, command: str, index: int) -> frozenset[str]:
    """The valueless options (``a``, ``s``, ``no-verify``, ``draft``, ...) of command ``index`` of the line.

    Used twice: on the original command, and on the rewritten one to prove that the rewrite kept every
    flag that shared a short cluster with a message option (``-am`` must become ``-a -F``)."""
    argv = scan_commands(command)[index].argv
    if job == JOB_COMMIT:
        _repo, sub = _git_dirs(argv, "/")
        opts = _read_options(argv, sub + 1, _COMMIT_SPEC)
    else:
        opts = _read_options(argv, 3, _PR_SPEC)
    return frozenset(o.name for o in opts if o.value is None)


def analyse(command: str, cwd: str) -> Plan | None:
    """The plan for ``command``; ``None`` if it does not use the placeholder in a git or gh command.

    Raises :class:`Unsupported` when it does but the shape cannot be bound safely, and
    :class:`~llm_router.text_job_bind.shellscan.ScanError` when the line cannot be read.
    """
    cmds = scan_commands(command)
    for c in cmds:
        for w in (*c.assigns, *c.argv, *(t for _o, t in c.redirs)):
            if w.token_in_expansion:
                raise Unsupported(
                    "token_in_expansion",
                    "The placeholder is inside $(...), backticks or $'...'; use it as a plain \"${LLM_TEXT:?}\" argument.",
                )
    valid = sum(1 for c in cmds for w in (*c.assigns, *c.argv, *(t for _o, t in c.redirs)) for p in w.pieces if p.kind == "tok")
    if len(MENTION.findall(command)) > valid and _runs_job_tool(cmds, command):
        raise Unsupported("placeholder_spelling", STRAY_MESSAGE)
    holders = [k for k, c in enumerate(cmds) if any(w.has_token for w in c.argv)]
    elsewhere = any(w.has_token for c in cmds for w in (*c.assigns, *(t for _o, t in c.redirs)))
    if not holders and not elsewhere:
        return None
    if elsewhere or len(holders) > 1:
        raise Unsupported(
            "misplaced",
            "Use one placeholder, as the value of -m (git commit) or --body (gh pr create), in one command.",
        )
    idx = holders[0]
    job_cmd = cmds[idx]
    tool = os.path.basename(job_cmd.argv[0].literal() or "")
    if tool not in ("git", "gh"):
        if any(os.path.basename(w.literal() or "") in ("git", "gh") for w in job_cmd.argv[1:4]):
            if job_cmd.structural:
                raise Unsupported("structure", "Run the placeholder command on its own, not inside a group, loop or keyword.")
            raise Unsupported("wrapper", "Run git or gh directly (no env, sudo, time or command in front) with the placeholder.")
        return None  # the placeholder sits in some other command (an echo, a doc): not ours
    if job_cmd.structural:
        raise Unsupported("structure", "Run the placeholder command on its own, not inside a group or keyword.")
    _check_redirs(job_cmd)
    _check_assigns(job_cmd)
    job = JOB_COMMIT if tool == "git" else JOB_PR
    cwd_now = cwd
    for pc in cmds[:idx]:
        cwd_now = _neutral(pc, cwd_now, job)
    plan = (_plan_commit if tool == "git" else _plan_pr)(command, job_cmd.argv, cwd_now)
    _validate(plan, job_cmd)
    plan.index = idx
    plan.flags = option_flags(plan.job, command, idx)
    return plan
