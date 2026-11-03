"""A strict scanner for the small shell subset the text-job-bind hook rewrites.

The hook has to answer one question about a Bash command line before it may touch it: is
the placeholder ``"${LLM_TEXT:?}"`` the value of a message option of one ``git commit`` or of
the body option of one ``gh pr create``, and what exactly is on the line before it. This
module reads the command into words (with their raw offsets), operators and simple commands,
and nothing else. It does not evaluate anything.

Strict on purpose. Anything it does not understand raises :class:`ScanError`, and the caller
turns that into a refusal that names the problem. It never guesses, because a wrong guess
here either rewrites the wrong argument or lets a placeholder through to the shell. The
shell's own ``${LLM_TEXT:?}`` abort is the last line of defence, not this module's excuse.

Supported: single quotes, double quotes with backslash escapes, backslash escapes, ``$(...)``,
``${...}``, ``$name``, backticks and ``$'...'`` (kept as opaque expansions), comments, the
separators ``&& || ; | |& &`` and newline, redirections (with an optional file-descriptor
prefix), top-level here-documents (their bodies are skipped, never scanned) and ``( )``
grouping, which is reported as structure the caller refuses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The placeholder is the shell variable ``LLM_TEXT``, written ``${LLM_TEXT:?}``: with the ``:?``
#: the shell aborts the whole command if nothing replaced it, so a missing or killed hook can
#: never commit the placeholder itself.
TOKEN_NAME = "LLM_TEXT"

#: Hook-side cap. A longer command is refused, not scanned.
MAX_COMMAND_CHARS = 32_000

_MAX_DEPTH = 24

#: What the placeholder may look like between the braces: ``${LLM_TEXT:?}`` or ``${LLM_TEXT:?message}`` (also
#: ``?`` without the colon). The ``?`` is the abort guard: bash stops the whole command when the variable is
#: unset, which is what makes a hook that never ran (not registered, killed) fail closed. ``${LLM_TEXT}`` and
#: ``$LLM_TEXT`` expand too, but to nothing, and an empty ``--body`` would be posted; they are not the placeholder.
_TOKEN_INNER = re.compile(rf"{TOKEN_NAME}:?\?[^}}]*")
#: The guarded and unguarded forms and the bare ``$LLM_TEXT``, found inside an expansion.
TOKEN_ANYWHERE = re.compile(
    rf"\$\{{{TOKEN_NAME}(?::?\?[^}}]*)?\}}|\${TOKEN_NAME}(?![A-Za-z0-9_])"
)
#: Any place the variable is written with its ``$``: expanding or not (single quotes, a backslash, a quoted
#: here-document, a comment, ``${LLM_TEXT:-x}``). A command that mentions it more often than the scanner found
#: the real placeholder has a mention that bash would NOT replace with the checked text.
MENTION = re.compile(rf"\$\{{?{TOKEN_NAME}(?![A-Za-z0-9_])")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")

_OP = re.compile(
    r"(?P<fd>\d+)?(?P<redir>&>>|&>|>>|>&|>\||<<<|<<-|<<|<&|<>|>|<)"
    r"|(?P<ctl>&&|\|\||;;|\|&|&|\||;|\(|\)|\n)"
)
_SEPARATORS = frozenset({"&&", "||", ";", ";;", "|", "|&", "&", "\n"})
_HEREDOC_OPS = frozenset({"<<", "<<-"})
_RESERVED = frozenset({
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac",
    "select", "function", "{", "}", "!", "time", "coproc",
})


class ScanError(ValueError):
    """The command is outside the shell subset this module understands."""


@dataclass(frozen=True)
class Piece:
    """One lexical piece of a word.

    ``kind`` is ``lit`` (literal text, quotes and escapes already removed), ``tok`` (the
    placeholder, ``text`` is its source) or ``exp`` (any other expansion, ``text`` is its
    source and its value is unknown).
    """

    kind: str
    text: str


@dataclass(frozen=True)
class Word:
    start: int
    end: int
    raw: str
    pieces: tuple[Piece, ...]

    @property
    def has_token(self) -> bool:
        return any(p.kind == "tok" for p in self.pieces)

    @property
    def has_expansion(self) -> bool:
        return any(p.kind == "exp" for p in self.pieces)

    @property
    def token_in_expansion(self) -> bool:
        """The placeholder written where nothing can replace it: inside ``$(...)`` or backticks."""
        return any(
            p.kind == "exp" and p.text[:2] in ("$(", "$'") or p.kind == "exp" and p.text[:1] == "`"
            for p in self.pieces
            if TOKEN_ANYWHERE.search(p.text)
        )

    def literal(self) -> str | None:
        """The word's value when it is plain text, else ``None``."""
        if any(p.kind != "lit" for p in self.pieces):
            return None
        return "".join(p.text for p in self.pieces)

    def value_with(self, text: str) -> str | None:
        """The word's value with the placeholder replaced by ``text``; ``None`` if any other
        expansion makes the value unknowable."""
        if self.has_expansion:
            return None
        return "".join(text if p.kind == "tok" else p.text for p in self.pieces)


@dataclass(frozen=True)
class Op:
    start: int
    end: int
    text: str

    @property
    def redir(self) -> str | None:
        """The redirection operator without its file-descriptor prefix, or ``None``."""
        m = _OP.fullmatch(self.text)
        return m.group("redir") if m else None


@dataclass
class Command:
    """One simple command: leading assignments, the argument words and the redirections."""

    before: str
    start: int
    end: int
    assigns: list[Word] = field(default_factory=list)
    argv: list[Word] = field(default_factory=list)
    redirs: list[tuple[Op, Word]] = field(default_factory=list)
    structural: bool = False  # ( ) grouping or a shell keyword: not a simple command


def _flush(buf: list[str], pieces: list[Piece]) -> None:
    if buf:
        pieces.append(Piece("lit", "".join(buf)))
        buf.clear()


def _skip_backtick(src: str, i: int) -> int:
    j, n = i + 1, len(src)
    while j < n:
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == "`":
            return j + 1
        j += 1
    raise ScanError("unterminated backtick")


def _skip_dq(src: str, i: int, depth: int) -> int:
    if depth > _MAX_DEPTH:
        raise ScanError("quoting is nested too deeply")
    j, n = i + 1, len(src)
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
        elif c == '"':
            return j + 1
        elif c == "$" and src[j + 1:j + 2] == "(":
            j = _skip_paren(src, j + 1, depth + 1)
        elif c == "$" and src[j + 1:j + 2] == "{":
            j = _skip_brace(src, j + 1, depth + 1)
        elif c == "`":
            j = _skip_backtick(src, j)
        else:
            j += 1
    raise ScanError("unterminated double quote")


def _skip_paren(src: str, i: int, depth: int = 0) -> int:
    """``src[i]`` is ``(``; return the index after its matching ``)``."""
    if depth > _MAX_DEPTH:
        raise ScanError("command substitution is nested too deeply")
    level, j, n = 0, i, len(src)
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            k = src.find("'", j + 1)
            if k < 0:
                raise ScanError("unterminated single quote")
            j = k + 1
            continue
        if c == '"':
            j = _skip_dq(src, j, depth + 1)
            continue
        if c == "`":
            j = _skip_backtick(src, j)
            continue
        if c == "$" and src[j + 1:j + 2] == "(":
            level += 1
            j += 2
            continue
        if c == "(":
            level += 1
        elif c == ")":
            level -= 1
            if level == 0:
                return j + 1
        j += 1
    raise ScanError("unterminated command substitution")


def _skip_brace(src: str, i: int, depth: int = 0) -> int:
    """``src[i]`` is ``{`` of a ``${``; return the index after its matching ``}``."""
    if depth > _MAX_DEPTH:
        raise ScanError("parameter expansion is nested too deeply")
    level, j, n = 0, i, len(src)
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            k = src.find("'", j + 1)
            if k < 0:
                raise ScanError("unterminated single quote")
            j = k + 1
            continue
        if c == '"':
            j = _skip_dq(src, j, depth + 1)
            continue
        if c == "{":
            level += 1
        elif c == "}":
            level -= 1
            if level == 0:
                return j + 1
        j += 1
    raise ScanError("unterminated parameter expansion")


def _skip_ansi_c(src: str, i: int) -> int:
    """``src[i]`` is the ``'`` of ``$'...'``; return the index after its closing quote."""
    j, n = i + 1, len(src)
    while j < n:
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == "'":
            return j + 1
        j += 1
    raise ScanError("unterminated $'...' string")


def _read_dq(src: str, i: int, buf: list[str], pieces: list[Piece]) -> int:
    """``src[i]`` is the opening ``"``; append its content and return the index after the close."""
    n = len(src)
    i += 1
    while True:
        if i >= n:
            raise ScanError("unterminated double quote")
        c = src[i]
        if c == '"':
            return i + 1
        if c == "\\":
            nxt = src[i + 1:i + 2]
            if nxt and nxt in '$`"\\':
                buf.append(nxt)
                i += 2
            elif nxt == "\n":
                i += 2
            else:
                buf.append("\\")
                i += 1
            continue
        if c == "$":
            i = _read_dollar(src, i, buf, pieces, in_dq=True)
            continue
        if c == "`":
            j = _skip_backtick(src, i)
            _flush(buf, pieces)
            pieces.append(Piece("exp", src[i:j]))
            i = j
            continue
        buf.append(c)
        i += 1


def _read_dollar(src: str, i: int, buf: list[str], pieces: list[Piece], in_dq: bool) -> int:
    """``src[i]`` is ``$``. Append what it starts and return the index after it."""
    nxt = src[i + 1:i + 2]
    if nxt == "(":
        j = _skip_paren(src, i + 1)
        _flush(buf, pieces)
        pieces.append(Piece("exp", src[i:j]))
        return j
    if nxt == "{":
        j = _skip_brace(src, i + 1)
        _flush(buf, pieces)
        inner = src[i + 2:j - 1]
        pieces.append(Piece("tok" if _TOKEN_INNER.fullmatch(inner) else "exp", src[i:j]))
        return j
    m = _NAME.match(src, i + 1)
    if m:
        _flush(buf, pieces)
        pieces.append(Piece("exp", src[i:m.end()]))  # $LLM_TEXT has no abort guard: not the placeholder
        return m.end()
    if nxt and nxt in "@*#?$!-0123456789":
        _flush(buf, pieces)
        pieces.append(Piece("exp", src[i:i + 2]))
        return i + 2
    if nxt == "'" and not in_dq:
        j = _skip_ansi_c(src, i + 1)
        _flush(buf, pieces)
        pieces.append(Piece("exp", src[i:j]))
        return j
    if nxt == '"' and not in_dq:
        return _read_dq(src, i + 1, buf, pieces)
    buf.append("$")
    return i + 1


def _read_word(src: str, i: int) -> tuple[Word, int]:
    n = len(src)
    start = i
    pieces: list[Piece] = []
    buf: list[str] = []
    while i < n:
        c = src[i]
        if c in " \t\n;&|()<>":
            break
        if c == "\\":
            nxt = src[i + 1:i + 2]
            if not nxt:
                buf.append("\\")
                i += 1
            elif nxt == "\n":
                i += 2
            else:
                buf.append(nxt)
                i += 2
            continue
        if c == "'":
            j = src.find("'", i + 1)
            if j < 0:
                raise ScanError("unterminated single quote")
            buf.append(src[i + 1:j])  # nothing expands inside single quotes: the text is literal, a ``$`` too
            i = j + 1
            continue
        if c == '"':
            i = _read_dq(src, i, buf, pieces)
            continue
        if c == "$":
            i = _read_dollar(src, i, buf, pieces, in_dq=False)
            continue
        if c == "`":
            j = _skip_backtick(src, i)
            _flush(buf, pieces)
            pieces.append(Piece("exp", src[i:j]))
            i = j
            continue
        buf.append(c)
        i += 1
    _flush(buf, pieces)
    if not pieces and i == start:  # defensive: a word must consume something
        raise ScanError("could not read a word")
    # A quoted empty string ("" or '') is a real, empty word.
    return Word(start, i, src[start:i], tuple(pieces)), i


def _skip_heredocs(src: str, i: int, pending: list[tuple[str, bool]]) -> int:
    """``i`` is just after a newline with here-documents pending: skip their bodies."""
    n = len(src)
    for delimiter, strip_tabs in pending:
        while True:
            if i >= n:
                raise ScanError("unterminated here-document")
            j = src.find("\n", i)
            line = src[i:] if j < 0 else src[i:j]
            i = n if j < 0 else j + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
    return i


def scan(src: str) -> list[Word | Op]:
    """Read ``src`` into words and operators, in order, with raw offsets.

    Raises :class:`ScanError` for anything outside the supported subset.
    """
    if len(src) > MAX_COMMAND_CHARS:
        raise ScanError(f"command is longer than {MAX_COMMAND_CHARS} characters")
    items: list[Word | Op] = []
    pending: list[tuple[str, bool]] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in " \t":
            i += 1
            continue
        if c == "\\" and src[i + 1:i + 2] == "\n":
            i += 2
            continue
        if c == "#":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        m = _OP.match(src, i)
        if m:
            op = Op(i, m.end(), m.group(0))
            items.append(op)
            i = m.end()
            if op.text == "\n" and pending:
                i = _skip_heredocs(src, i, pending)
                pending = []
            continue
        word, i = _read_word(src, i)
        prev = items[-1] if items else None
        if isinstance(prev, Op) and prev.redir in _HEREDOC_OPS:
            delimiter = word.literal()
            if delimiter is None:
                raise ScanError("here-document delimiter is not plain text")
            pending.append((delimiter, prev.redir == "<<-"))
        items.append(word)
    if pending:
        raise ScanError("unterminated here-document")
    return items


def commands(items: list[Word | Op]) -> list[Command]:
    """Group the scanned items into simple commands, in order."""
    out: list[Command] = []
    cur: Command | None = None
    before = ""
    waiting: Op | None = None

    def open_command(pos: int) -> Command:
        return Command(before=before, start=pos, end=pos)

    def close() -> None:
        nonlocal cur
        if cur is not None and (cur.assigns or cur.argv or cur.redirs or cur.structural):
            out.append(cur)
        cur = None

    for it in items:
        if isinstance(it, Op):
            if waiting is not None:
                raise ScanError("a redirection has no target")
            if it.text in _SEPARATORS:
                close()
                before = it.text
            elif it.text in ("(", ")"):
                if cur is None:
                    cur = open_command(it.start)
                cur.structural = True
                cur.end = it.end
                close()
                before = it.text
            else:
                if cur is None:
                    cur = open_command(it.start)
                waiting = it
                cur.end = it.end
            continue
        if cur is None:
            cur = open_command(it.start)
        cur.end = it.end
        if waiting is not None:
            cur.redirs.append((waiting, it))
            waiting = None
        elif not cur.argv and _ASSIGN.match(it.raw):
            cur.assigns.append(it)
        else:
            if not cur.argv and it.literal() in _RESERVED:
                cur.structural = True
            cur.argv.append(it)
    if waiting is not None:
        raise ScanError("a redirection has no target")
    close()
    return out


def scan_commands(src: str) -> list[Command]:
    """:func:`scan` then :func:`commands`."""
    return commands(scan(src))
