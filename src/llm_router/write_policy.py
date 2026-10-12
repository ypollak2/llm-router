"""Static shell-write tripwire (PLAN v16 P2.9-b, R-LOC-7).

P0.13 confines the file tools with ``_resolve_path``, but a path check on file
tools cannot see a path inside a command string: ``echo x > ../out/y`` wrote
outside the project root. ``check_command`` reads the command text *before* it
runs and refuses the write forms it can see.

THIS IS A TRIPWIRE, NOT A SANDBOX. Static parsing of shell is not sound:
variable and glob expansion, aliases, ``curl -o``, ``git`` plumbing, ``make``
recipes and any program that writes on its own are not seen. An OS-level write
sandbox is 16.1; until then R-LOC-7 reads "partial: static tripwire".

Verdict policy (each choice is deliberate):

* ``refuse``         a write target resolves outside ``root`` (symlinks followed).
                     Definite and not recoverable by asking; the model must use
                     the confined file tools instead.
* ``needs_approval`` the command can write arbitrarily or cannot be judged
                     statically: interpreter one-liners (``python -c``,
                     ``node -e``, ``sh -c``, ``eval``, ``curl | sh`` ...), a
                     shell or interpreter reading code from stdin, ``sudo``,
                     ``xargs``/``find -exec``, command substitution, a target
                     built from an unexpanded ``$VAR``, an unresolvable ``cd``,
                     and any parse failure (unbalanced quote, dangling
                     redirect). Fail closed.
* ``allow``          every target seen resolves inside ``root``; ``/dev/null``
                     and fd dups such as ``2>&1`` are not writes.

Precedence: refuse > needs_approval > allow.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ALLOW = "allow"
REFUSE = "refuse"
NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class Verdict:
    decision: str
    reason: str = ""
    targets: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW


_OPS = ("&>>", "<<<", "<<-", ">>", ">|", "&>", ">&", "<<", "<&", "<>", "&&", "||",
        "|&", ";;", ";", "&", "|", ">", "<", "(", ")")
_OP_START = set(";&|<>()")
_SEPARATORS = {";", "&&", "||", "|", "&", "|&", ";;", "(", ")"}
_WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>", ">&"}
_READ_REDIRECTS = {"<", "<<", "<<-", "<<<", "<&"}
_NULL_DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {"command", "exec", "time", "nohup", "nice", "builtin", "env"}

Token = tuple[str, str, bool]          # (kind 'op'|'word', text, has_expansion)


def _tokenize(cmd: str) -> tuple[list[Token], bool]:
    """Quote-aware tokenizer. Returns (tokens, saw_substitution). Raises
    ValueError on an unbalanced quote. Heredoc bodies are skipped (they are
    data, not commands); an unquoted newline becomes a ``;`` operator."""
    toks: list[Token] = []
    subst = False
    pending: list[tuple[str, bool]] = []
    heredoc_next: bool | None = None
    last_word_end = -1
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\n":
            toks.append(("op", ";", False))
            i += 1
            for delim, strip in pending:
                while i < n:
                    j = cmd.find("\n", i)
                    line = cmd[i:] if j < 0 else cmd[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            pending = []
            continue
        if c in " \t\r":
            i += 1
            continue
        if c == "\\" and cmd[i + 1:i + 2] == "\n":
            i += 2
            continue
        if c == "#":
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        if c in _OP_START:
            op = next(o for o in _OPS if cmd.startswith(o, i))
            if (("<" in op or ">" in op) and toks and toks[-1][0] == "word"
                    and toks[-1][1].isdigit() and last_word_end == i):
                toks.pop()                  # the fd number of `2>file`, not an argument
            toks.append(("op", op, False))
            i += len(op)
            if op in ("<<", "<<-"):
                heredoc_next = op == "<<-"
            continue
        word: list[str] = []
        expands = False
        while i < n and cmd[i] not in " \t\r\n" and cmd[i] not in _OP_START:
            c = cmd[i]
            if c == "'":
                j = cmd.find("'", i + 1)
                if j < 0:
                    raise ValueError("unbalanced single quote")
                word.append(cmd[i + 1:j])
                i = j + 1
            elif c == '"':
                i += 1
                while True:
                    if i >= n:
                        raise ValueError("unbalanced double quote")
                    d = cmd[i]
                    if d == '"':
                        i += 1
                        break
                    if d == "\\" and i + 1 < n:
                        word.append(cmd[i + 1] if cmd[i + 1] in '"\\$`' else d + cmd[i + 1])
                        i += 2
                        continue
                    if d == "`" or (d == "$" and cmd[i + 1:i + 2] == "("):
                        subst = True
                    if d == "$":
                        expands = True
                    word.append(d)
                    i += 1
            elif c == "\\":
                word.append(cmd[i + 1] if i + 1 < n else "")
                i += 2
            else:
                if c == "`" or (c == "$" and cmd[i + 1:i + 2] == "("):
                    subst = True
                if c == "$":
                    expands = True
                word.append(c)
                i += 1
        text = "".join(word)
        if heredoc_next is not None:
            pending.append((text, heredoc_next))
            heredoc_next = None
        toks.append(("word", text, expands))
        last_word_end = i
    return toks, subst


class _Scan:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.cwd: Path | None = self.root
        self.targets: list[str] = []
        self.outside: list[str] = []
        self.approvals: list[str] = []

    def need(self, why: str) -> None:
        if why not in self.approvals:
            self.approvals.append(why)

    def target(self, text: str, expands: bool = False, base: Path | None = None) -> None:
        if text in _NULL_DEVICES:
            return
        if expands:
            self.need(f"write target built from an expansion: {text}")
            return
        p = Path(os.path.expanduser(text))
        if not p.is_absolute():
            anchor = base if base is not None else self.cwd
            if anchor is None:
                self.need(f"relative write target after an unresolvable cd: {text}")
                return
            p = anchor / p
        resolved = p.resolve()
        self.targets.append(str(resolved))
        if not resolved.is_relative_to(self.root):
            self.outside.append(str(resolved))


def _split_opts(args: list[str]) -> tuple[list[str], list[str]]:
    """(leading options, the rest). A lone ``-`` is a positional (stdin)."""
    k = 0
    while k < len(args) and args[k].startswith("-") and args[k] != "-":
        if args[k] == "--":
            k += 1
            break
        k += 1
    return args[:k], args[k:]


def _positionals(args: list[str], takes_value: set[str]) -> tuple[list[str], str | None]:
    """Positional args of a cp/mv/install/ln style command, plus a -t DIR value."""
    pos: list[str] = []
    tdir: str | None = None
    k = 0
    end_opts = False
    while k < len(args):
        a = args[k]
        if end_opts or not a.startswith("-") or a == "-":
            pos.append(a)
        elif a == "--":
            end_opts = True
        elif a.startswith("--target-directory="):
            tdir = a.split("=", 1)[1]
        elif a == "--target-directory" and k + 1 < len(args):
            tdir = args[k + 1]
            k += 1
        elif a == "-t" and k + 1 < len(args):
            tdir = args[k + 1]
            k += 1
        elif a.startswith("-t") and not a.startswith("--") and len(a) > 2:
            tdir = a[2:]
        elif a in takes_value:
            k += 1
        k += 1
    return pos, tdir


def _segment(scan: _Scan, words: list[Token], redirs: list[tuple[str, Token | None]],
             prev_sep: str, stdin_code: bool) -> None:
    for op, tok in redirs:
        if tok is None:
            scan.need(f"redirect '{op}' without a target")
            continue
        if op == ">&" and (tok[1].isdigit() or tok[1] == "-"):
            continue                                   # fd dup: 2>&1, >&2, >&-
        scan.target(tok[1], tok[2])

    i = 0
    while i < len(words):                              # strip wrappers
        w = words[i][1]
        base = os.path.basename(w)
        if _ASSIGN.match(w) or base in _WRAPPERS or (w.startswith("-") and i > 0):
            i += 1
        elif base in ("sudo", "doas"):
            scan.need("privilege escalation (sudo)")
            i += 1
        elif w in ("{", "!"):
            i += 1
        else:
            break
    if i >= len(words):
        return
    prog = os.path.basename(words[i][1])
    args = [w[1] for w in words[i + 1:]]
    expands = {w[1]: w[2] for w in words[i + 1:]}
    opts, rest = _split_opts(args)

    def tgt(text: str) -> None:
        scan.target(text, expands.get(text, False))

    if prog in ("cd", "pushd"):
        pos = [a for a in args if not a.startswith("-") or a == "-"]
        if not pos or pos[0] == "-" or expands.get(pos[0], False) or scan.cwd is None:
            scan.cwd = None
        else:
            p = Path(os.path.expanduser(pos[0]))
            scan.cwd = (p if p.is_absolute() else scan.cwd / p).resolve()
        return
    if prog == "popd":
        scan.cwd = None
        return
    if prog == "eval":
        scan.need("eval")
    elif re.fullmatch(r"python[\d.]*", prog):
        if any(re.fullmatch(r"-[A-Za-z]*c", o) for o in opts):
            scan.need("python -c")
        elif "-m" not in opts and not rest and not (
                set(opts) & {"-V", "--version", "-h", "--help"}):
            scan.need("python reading code from stdin")
    elif prog in ("node", "nodejs"):
        if any(o in ("-e", "--eval", "-p", "--print") or re.fullmatch(r"-[A-Za-z]*[ep]", o)
               for o in opts):
            scan.need("node -e")
        elif not rest and not (set(opts) & {"-v", "--version", "-h", "--help"}):
            scan.need("node reading code from stdin")
    elif prog in ("sh", "bash", "zsh", "dash", "ksh", "fish"):
        if any(re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", o) for o in opts):
            scan.need(f"{prog} -c")
        elif prev_sep in ("|", "|&"):
            scan.need(f"pipe into {prog} (curl|sh, base64 -d|sh)")
        elif stdin_code or not rest:
            scan.need(f"{prog} reading code from stdin")
    elif prog in ("perl", "ruby", "php", "lua"):
        flag = {"perl": r"-[A-Za-z]*[eE]", "ruby": r"-[A-Za-z]*e",
                "php": r"-r", "lua": r"-e"}[prog]
        if any(re.fullmatch(flag, o) for o in opts):
            scan.need(f"{prog} one-liner")
    elif prog == "xargs":
        scan.need("xargs runs arbitrary commands")
    elif prog == "find" and any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in args):
        scan.need("find -exec runs arbitrary commands")

    if prog == "tee":
        for a in rest:
            tgt(a)
    elif prog == "dd":
        for a in args:
            if a.startswith("of="):
                tgt(a[3:])
    elif prog in ("cp", "mv", "install", "ln"):
        pos, tdir = _positionals(args, {"-m", "-o", "-g", "-S", "--mode", "--owner", "--group"}
                                 if prog == "install" else {"-S", "--suffix"})
        symbolic = prog == "ln" and any(
            o == "--symbolic" or (o.startswith("-") and not o.startswith("--") and "s" in o)
            for o in args if o.startswith("-"))
        if tdir is not None:
            tgt(tdir)
            dest_dir = scan.cwd / os.path.expanduser(tdir) if scan.cwd else None
            srcs = pos
        elif pos:
            if prog == "ln" and len(pos) == 1:
                tgt(os.path.basename(pos[0]))          # ln -s /x  -> ./x
                srcs = pos
                dest_dir = scan.cwd
            else:
                tgt(pos[-1])
                srcs = pos[:-1]
                d = Path(os.path.expanduser(pos[-1]))
                dest_dir = (d if d.is_absolute() else scan.cwd / d if scan.cwd else None)
                dest_dir = dest_dir.parent if dest_dir is not None else None
        else:
            srcs, dest_dir = [], None
        if prog == "mv":
            for s in srcs:                             # mv removes its source
                tgt(s)
        if symbolic:
            # a link inside root pointing outside is the escape primitive; a
            # later `> link/x` in the same command line would not be seen.
            for s in srcs:
                scan.target(s, expands.get(s, False), base=dest_dir)
    elif prog == "sed":
        if any(re.fullmatch(r"-[nEsurz]*i.*|--in-place(=.*)?", o) for o in args
               if o.startswith("-")):
            has_script, files, k = False, [], 0
            while k < len(args):
                a = args[k]
                if a in ("-e", "-f", "--expression", "--file"):
                    has_script = True
                    k += 2
                    continue
                if not a.startswith("-") or a == "-":
                    files.append(a)
                k += 1
            for f in (files if has_script else files[1:]):
                tgt(f)
    elif prog == "perl":
        if any(re.fullmatch(r"-[plnaw0-9]*i.*", o) for o in args if o.startswith("-")):
            has_script, files, k = False, [], 0
            while k < len(args):
                a = args[k]
                if a in ("-e", "-E"):
                    has_script = True
                    k += 2
                    continue
                if not a.startswith("-") or a == "-":
                    files.append(a)
                k += 1
            for f in (files if has_script else files[1:]):
                tgt(f)


def check_command(cmd: str, root: Path) -> Verdict:
    """Judge a shell command line against the project *root* before it runs."""
    if not cmd.strip():
        return Verdict(ALLOW)
    try:
        toks, subst = _tokenize(cmd)
    except ValueError as exc:
        return Verdict(NEEDS_APPROVAL, f"cannot parse command ({exc})")
    scan = _Scan(root)
    if subst:
        scan.need("command substitution can run unseen commands")

    words: list[Token] = []
    redirs: list[tuple[str, Token | None]] = []
    prev_sep = ""
    stdin_code = False
    k = 0

    def flush(sep: str) -> None:
        nonlocal words, redirs, prev_sep, stdin_code
        if words or redirs:
            _segment(scan, words, redirs, prev_sep, stdin_code)
        words, redirs, prev_sep, stdin_code = [], [], sep, False

    while k < len(toks):
        kind, text, exp = toks[k]
        if kind == "op" and text in _SEPARATORS:
            flush(text)
        elif kind == "op" and (text in _WRITE_REDIRECTS or text in _READ_REDIRECTS):
            nxt = toks[k + 1] if k + 1 < len(toks) else None
            if nxt is not None and nxt[0] == "op" and nxt[1] == "(" and text in (">", "<"):
                scan.need("process substitution")
            elif nxt is None or nxt[0] == "op":
                if text in _WRITE_REDIRECTS:
                    redirs.append((text, None))
            else:
                if text in _WRITE_REDIRECTS:
                    redirs.append((text, nxt))
                elif text in ("<<", "<<-", "<<<"):
                    stdin_code = True
                k += 1
        else:
            words.append((kind, text, exp))
        k += 1
    flush("")

    targets = tuple(scan.targets)
    if scan.outside:
        return Verdict(REFUSE, "write target outside project root: "
                       + ", ".join(scan.outside), targets)
    if scan.approvals:
        return Verdict(NEEDS_APPROVAL, "; ".join(scan.approvals), targets)
    return Verdict(ALLOW, "", targets)
