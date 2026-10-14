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
_WRAPPERS = {"command", "exec", "time", "nohup", "nice", "builtin", "env", "timeout", "stdbuf",
             "setsid"}
_INTERPRETERS_STDIN_PATHS = {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
#: Options of an interpreter that take a value, so the next word is not a script.
_INTERP_VALUED = {
    "python": {"-W", "-X", "-Q"}, "node": {"-r", "--require", "--import", "--loader"},
    "sh": {"-o", "-O", "+o", "+O", "--rcfile", "--init-file"},
    "perl": {"-I", "-M", "-m", "-x"}, "ruby": {"-I", "-r", "-C"},
}

_KEYWORDS = {"{", "}", "!", "do", "then", "else", "elif", "if", "while", "until",
             "done", "fi", "esac"}          # grammar words: the command follows them
_HEADS = {"for", "select", "case"}           # whole segment is a loop/case head (data only)
_ARITH_CMD = re.compile(r"(?<![\w$-])(?:let|declare|typeset|read)\b|\blocal\s+-\w*i|\(\(|\[|\$\{!")
_SED_WRITE = re.compile(r"(?:^|[/;}\s])[wW]\s*\S")
_LOOPS_OR_FUNCS = re.compile(r"(?<![\w-])(?:for|while|until|select|function)(?![\w-])|\(\s*\)")
_ARITH_WHY = ("`$(`/backtick on a line that also evaluates arithmetic or a subscript "
              "(let/declare/typeset/read/local -i/((/[ ]/${!}: bash re-evaluates it even inside quotes)")

_NOT_WIRED = " — not run (needs approval; approval is not wired, so treated as refused)"
_ARITH = re.compile(r"[A-Za-z0-9_ \t+\-*/%()!~^=,?:$]*")
_SUBST_WHY = ("command substitution can run unseen commands "
              "(write the text to a file instead, e.g. `git commit -F file`)")


def _dollar_paren(cmd: str, i: int) -> tuple[int, bool]:
    """cmd[i:] starts with ``$(``. Returns (index after the construct, is_substitution).
    Only a strict ``$((arithmetic))`` is not a substitution: the matching close is an
    adjacent ``))`` and the content is nothing but identifiers, digits, spaces and
    ``+ - * / % ( ) ! ~ ^ = , ? : $``. In doubt it is a substitution (needs approval).
    ``$(cat <<E ... E)`` is NOT exempt: a heredoc body can nest ``$()`` and its end
    cannot be found reliably."""
    if cmd.startswith("$((", i):
        depth, j = 0, i + 1
        while j < len(cmd):
            depth += (cmd[j] == "(") - (cmd[j] == ")")
            j += 1
            if depth == 0:
                inner = cmd[i + 3:j - 2]
                ok = (cmd[j - 2:j] == "))" and "$(" not in inner
                        and _ARITH.fullmatch(inner) is not None)
                return j, not ok
        return len(cmd), True
    return i + 2, True


def _raw_nested_exec(cmd: str) -> bool:
    """Any ``$(``/backtick in the raw text, quoted or not, that is not a strict
    ``$((arithmetic))`` (so ``$((cmd) )`` counts)."""
    if "`" in cmd:
        return True
    i = cmd.find("$(")
    while i >= 0:
        end, is_sub = _dollar_paren(cmd, i)
        if is_sub:
            return True
        i = cmd.find("$(", max(end, i + 2))
    return False


Token = tuple[str, str, bool]          # (kind 'op'|'word', text, has_expansion)


def _tokenize(cmd: str) -> tuple[list[Token], bool]:
    """Quote-aware tokenizer. Returns (tokens, saw_substitution). Raises
    ValueError on an unbalanced quote. Heredoc bodies are skipped (they are
    data, not commands); an unquoted newline becomes a ``;`` operator."""
    toks: list[Token] = []
    subst = False
    pending: list[tuple[str, bool, bool]] = []      # (delimiter, strip tabs, quoted)
    heredoc_next: bool | None = None
    last_word_end = -1
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\n":
            toks.append(("op", ";", False))
            i += 1
            for delim, strip, quoted in pending:
                while i < n:
                    j = cmd.find("\n", i)
                    line = cmd[i:] if j < 0 else cmd[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
                    if not quoted and _raw_nested_exec(line):
                        subst = True             # an unquoted heredoc body is expanded
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
        quoted = False
        brace = comma = False                   # unquoted `{`/`,` (brace expansion)
        while i < n and cmd[i] not in " \t\r\n" and cmd[i] not in _OP_START:
            c = cmd[i]
            if c in "'\"\\":
                quoted = True
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
                    if d == "$" and cmd[i + 1:i + 2] == "(":
                        i, sub = _dollar_paren(cmd, i)
                        subst = subst or sub
                        expands = True
                        continue
                    if d == "`":
                        subst = True
                    if d == "$":
                        expands = True
                    word.append(d)
                    i += 1
            elif c == "\\":
                if cmd[i + 1:i + 2] == "\n":
                    i += 2                      # line continuation: joins the word
                    continue
                word.append(cmd[i + 1] if i + 1 < n else "")
                i += 2
            else:
                if c == "{":
                    brace = True
                elif c == "," and brace:
                    comma = True
                elif c == "}" and comma:
                    expands = True              # {a,b}: bash brace expansion
                if c == "$" and cmd[i + 1:i + 2] == "(":
                    i, sub = _dollar_paren(cmd, i)
                    subst = subst or sub
                    expands = True
                    continue
                if c == "`":
                    subst = True
                if c == "$":
                    expands = True
                word.append(c)
                i += 1
        text = "".join(word)
        if heredoc_next is not None:
            pending.append((text, heredoc_next, quoted))
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
        if text == "~-" or text.startswith("~-/"):
            self.need(f"write target uses ~- (previous directory): {text}")
            return
        if text == "~+" or text.startswith("~+/"):
            if self.cwd is None:
                self.need(f"write target uses ~+ after an unresolvable cd: {text}")
                return
            text = str(self.cwd) + text[2:]
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


_PATH_WRITERS: dict[str, set[str]] = {
    "touch": {"-d", "-t", "-r", "--date", "--reference"},
    "mkdir": {"-m", "--mode"},
    "truncate": {"-s", "-r", "--size", "--reference"},
    "rm": set(), "chmod": set(), "chown": set(), "chgrp": set(), "rename": set(),
}


def _split_opts(args: list[str]) -> tuple[list[str], list[str]]:
    """(leading options, the rest). A lone ``-`` is a positional (stdin)."""
    k = 0
    while k < len(args) and args[k].startswith("-") and args[k] != "-":
        if args[k] == "--":
            k += 1
            break
        k += 1
    return args[:k], args[k:]


def _interp_opts(args: list[str], valued: set[str]) -> tuple[list[str], list[str]]:
    """(options, the rest) of an interpreter's argv: options run until the first
    positional (the script), skipping the value of an option that takes one
    (``python -W ignore -c ...``)."""
    k = 0
    opts: list[str] = []
    while k < len(args) and (args[k].startswith("-") or args[k].startswith("+")) and args[k] != "-":
        if args[k] == "--":
            k += 1
            break
        opts.append(args[k])
        k += 2 if args[k] in valued else 1
    return opts, args[k:]


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
        elif not a.startswith("--") and "t" in a[1:]:
            j = a.index("t", 1)                        # -vt DIR / -vtDIR
            if j == len(a) - 1 and k + 1 < len(args):
                tdir = args[k + 1]
                k += 1
            elif j < len(a) - 1:
                tdir = a[j + 1:]
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
        if _ASSIGN.match(w) or w in _KEYWORDS:
            i += 1
        elif w == "function":
            i += 2                                     # `function NAME`; the body follows
        elif w in _HEADS and i == 0:
            texts = [t[1] for t in words]
            if w != "case" and "do" in texts:
                i = texts.index("do") + 1              # `for x do BODY`: the body is a command
                continue
            return                                     # `for N in W`, `case W in`: data only
        elif base in _WRAPPERS or base in ("sudo", "doas"):
            if base in ("sudo", "doas"):
                scan.need("privilege escalation (sudo)")
            i += 1
            # the wrapper's own options (and their values)
            valued = {"timeout": {"-s", "-k", "--signal", "--kill-after"},
                      "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
                      "nice": {"-n", "--adjustment"}, "env": {"-u", "--unset"},
                      "exec": {"-a"}}.get(base, set())
            while i < len(words) and words[i][1].startswith("-") and words[i][1] != "-":
                o = words[i][1]
                if base == "env" and (o in ("-S", "-C", "--split-string", "--chdir")
                                      or o.startswith(("--split-string=", "--chdir="))
                                      or (not o.startswith("--") and ("S" in o or "C" in o))):
                    scan.need(f"env {o} runs a string or changes directory")
                i += 2 if o in valued else 1
            if base == "timeout":
                i += 1                                 # the DURATION operand
        else:
            break
    if i >= len(words):
        return
    if words[i][2]:
        scan.need(f"command name built from an expansion: {words[i][1]}")
        return
    prog = os.path.basename(words[i][1])
    args = [w[1] for w in words[i + 1:]]
    expands = {w[1]: w[2] for w in words[i + 1:]}
    opts, rest = _split_opts(args)

    def tgt(text: str) -> None:
        scan.target(text, expands.get(text, False))

    if prog in ("cd", "pushd"):
        pos = [a for a in args if not a.startswith("-") or a == "-"]
        relative_name = bool(pos) and not pos[0].startswith(("/", "./", "../", "~")) and pos[0] not in (".", "..")
        if (not pos or pos[0] == "-" or pos[0].startswith("~-") or expands.get(pos[0], False)
                or scan.cwd is None or (relative_name and os.environ.get("CDPATH"))):
            scan.cwd = None                            # CDPATH can send a bare name anywhere
        else:
            d0 = pos[0]
            if d0 == "~+" or d0.startswith("~+/"):
                d0 = str(scan.cwd) + d0[2:]
            p = Path(os.path.expanduser(d0))
            scan.cwd = (p if p.is_absolute() else scan.cwd / p).resolve()
        return
    if prog == "popd":
        scan.cwd = None
        return
    family = ("python" if re.fullmatch(r"python[\d.]*", prog) else "node" if prog in ("node", "nodejs")
              else "sh" if prog in ("sh", "bash", "zsh", "dash", "ksh", "fish") else prog)
    if family in _INTERP_VALUED or family in ("php", "lua"):
        opts, rest = _interp_opts(args, _INTERP_VALUED.get(family, set()))
    if rest[:1] and rest[0] in _INTERPRETERS_STDIN_PATHS and family in (
            "python", "node", "sh", "perl", "ruby"):
        scan.need(f"{prog} {rest[0]} (code from stdin)")
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
        hard = (prog == "ln" and not symbolic) or (prog == "cp" and any(
            o == "--link" or (o.startswith("-") and not o.startswith("--") and "l" in o)
            for o in args if o.startswith("-")))
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
        elif hard:
            # an in-root hard link to an outside inode: writing it writes outside
            for s in srcs:
                tgt(s)
    elif prog in _PATH_WRITERS:
        takes = _PATH_WRITERS[prog]
        if prog == "chmod":                            # -w, +x, =r are modes, not options
            args_ = [a for a in args if not re.fullmatch(r"[-+=][rwxXstugoa,+=-]*", a)]
            mode_like = len(args_) != len(args)
            pos, _ = _positionals(args_, takes)
            if not mode_like and not any(a.startswith("--reference") for a in args):
                pos = pos[1:]
        else:
            pos, _ = _positionals(args, takes)
            if prog in ("chown", "chgrp", "rename") and not any(
                    a.startswith("--reference") for a in args):
                pos = pos[1:]                          # owner / s-expression
        for a in pos:
            tgt(a)
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
                if a.startswith(("--expression=", "--file=")) or (
                        re.fullmatch(r"-[nEsurz]*[ef].+", a)):
                    has_script = True                  # -eSCRIPT, --expression=SCRIPT
                    k += 1
                    continue
                if not a.startswith("-") or a == "-":
                    files.append(a)
                k += 1
            for f in (files if has_script else files[1:]):
                tgt(f)
        if any(_SED_WRITE.search(a) for a in args if not a.startswith("-")):
            scan.need("sed w/W command writes files")
    elif prog in ("awk", "gawk", "mawk", "nawk"):
        if any("system" in a or ">" in a or "|" in a for a in args if not a.startswith("-")):
            scan.need(f"{prog} program can run commands or write files")
    elif prog == "find" and (set(args) & {"-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}):
        start = 0
        while start < len(args) and not args[start].startswith(("-", "(", "!")):
            tgt(args[start])                           # the search paths -delete may remove
            start += 1
        for k2, a in enumerate(args):
            if a in ("-fprint", "-fprint0", "-fprintf", "-fls") and k2 + 1 < len(args):
                tgt(args[k2 + 1])
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


def _arith_end(toks: list[Token], k: int) -> int | None:
    """toks[k:k+2] is ``((``. Index after the matching adjacent ``))`` if the content is
    plain arithmetic (no expansion, no ``/``, no two words in a row, i.e. not a command),
    else None."""
    depth, j, prev_word = 0, k, False
    while j < len(toks):
        kind, text, exp = toks[j]
        if kind == "op" and text in ("(", ")"):
            depth += 1 if text == "(" else -1
            prev_word = False
            if depth == 0:
                return j + 1 if toks[j - 1][1:] == (")", False) and j - 1 > k + 1 else None
        elif kind == "word":
            if exp or "/" in text or prev_word:
                return None
            prev_word = True
        else:
            prev_word = False
        j += 1
    return None


def check_command(cmd: str, root: Path) -> Verdict:
    """Judge a shell command line against the project *root* before it runs."""
    if not cmd.strip():
        return Verdict(ALLOW)
    try:
        toks, subst = _tokenize(cmd)
    except ValueError as exc:
        return Verdict(NEEDS_APPROVAL, f"cannot parse command ({exc})" + _NOT_WIRED)
    scan = _Scan(root)
    if subst:
        scan.need(_SUBST_WHY)
    if _ARITH_CMD.search(cmd) and _raw_nested_exec(cmd):
        scan.need(_ARITH_WHY)
    if re.search(r"(?<![\w-])(?:cd|pushd|popd)(?![\w-])", cmd) and _LOOPS_OR_FUNCS.search(cmd):
        scan.need("cd combined with a loop or a function: the body may run in another directory")
    if "CDPATH" in cmd:
        scan.need("CDPATH changes where cd goes")

    words: list[Token] = []
    redirs: list[tuple[str, Token | None]] = []
    prev_sep = ""
    stdin_code = False
    k = 0
    cwd_stack: list[tuple[str, Path | None]] = []   # ("sub", cwd) per `(`, ("case", None) per case

    def flush(sep: str) -> None:
        nonlocal words, redirs, prev_sep, stdin_code
        if words or redirs:
            _segment(scan, words, redirs, prev_sep, stdin_code)
        words, redirs, prev_sep, stdin_code = [], [], sep, False

    while k < len(toks):
        kind, text, exp = toks[k]
        if (kind == "op" and text == "(" and toks[k + 1:k + 2] == [("op", "(", False)]
                and all(w[1] in ("for", "if", "while", "until", "elif", "then", "do", "else", "!")
                        for w in words) and not redirs):
            end = _arith_end(toks, k)                  # `((expr))` / `for ((a;b;c))`
            if end is None or (not words or words[0][1] != "for") and any(
                    t[1] == ";" for t in toks[k:end]):
                scan.need("arithmetic command that cannot be parsed confidently")
                break
            words = []
            k = end
            continue
        if kind == "op" and text in _SEPARATORS:
            flush(text)
            if text == "(":
                cwd_stack.append(("sub", scan.cwd))    # a subshell's cd stays inside it
            elif text == ")" and cwd_stack and cwd_stack[-1][0] == "sub":
                scan.cwd = cwd_stack.pop()[1]          # a case pattern's `)` closes nothing
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
            if kind == "word" and not words and text == "case":
                cwd_stack.append(("case", None))
            elif kind == "word" and not words and text == "esac":
                while cwd_stack and cwd_stack[-1][0] != "case":
                    cwd_stack.pop()
                if cwd_stack:
                    cwd_stack.pop()
            words.append((kind, text, exp))
        k += 1
    flush("")

    targets = tuple(scan.targets)
    if scan.outside:
        return Verdict(REFUSE, "write target outside project root: "
                       + ", ".join(scan.outside) + " — not run", targets)
    if scan.approvals:
        return Verdict(NEEDS_APPROVAL, "; ".join(scan.approvals) + _NOT_WIRED, targets)
    return Verdict(ALLOW, "", targets)
