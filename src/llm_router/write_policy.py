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
                     redirect), sed shell commands/file scripts, awk effects,
                     find deletion following symlinks, command-string wrappers,
                     and path-resolution errors. Fail closed.
* ``allow``          every target seen resolves inside ``root``; ``/dev/null``
                     and fd dups such as ``2>&1`` are not writes.

Precedence: refuse > needs_approval > allow.

P2.9-a makes this ONE policy for ``llm_act``, ``llm_local_task`` and the toolkit:
``contain`` is the single path-containment test and ``check_write`` its verdict
form; ``check_action`` is ``check_command`` plus the program allowlist (D-322:
any program not on it, and any delete/exec flag of one that is, needs approval;
output and working paths must resolve inside ``root``) plus the ``DESTRUCTIVE``
table (``rm -r``, git state changes, package installs: needs approval, which is
refused while approval is unwired); ``pick_root`` chooses the project root (MCP root, then
``CLAUDE_PROJECT_DIR``, then the caller's request; never the process cwd).
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
_SH_FAMILY = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "mksh", "yash"}
_INTERPRETERS_STDIN_PATHS = {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
#: Options of an interpreter that take a value, so the next word is not a script.
_INTERP_VALUED = {
    "python": {"-W", "-X", "-Q", "--check-hash-based-pycs"},
    "node": {"-r", "--require", "--import", "--loader"},
    "sh": {"-o", "-O", "+o", "+O", "--rcfile", "--init-file"},
    "perl": {"-I", "-M", "-m", "-x"}, "ruby": {"-I", "-r", "-C"},
}

_KEYWORDS = {"{", "}", "!", "do", "then", "else", "elif", "if", "while", "until",
             "done", "fi", "esac"}          # grammar words: the command follows them
_HEADS = {"for", "select", "case"}           # whole segment is a loop/case head (data only)
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
                found = False
                while i < n:
                    j = cmd.find("\n", i)
                    line = cmd[i:] if j < 0 else cmd[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        found = True
                        break
                    if not quoted and _raw_nested_exec(line):
                        subst = True             # an unquoted heredoc body is expanded
                if not found:
                    raise ValueError("heredoc without terminator")
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
    if pending or heredoc_next is not None:
        raise ValueError("heredoc without terminator")
    return toks, subst


def _in_git_dir(rel: Path) -> bool:
    """`.git` in any case: the default macOS filesystem is case-insensitive (rv-785b S2)."""
    return any(part.casefold() == ".git" for part in rel.parts)


def _unresolved_symlink(path: Path) -> bool:
    """A fully resolved path has no symlink left; one left means resolve() hit a loop."""
    return any(q.is_symlink() for q in (path, *path.parents))


class _Scan:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.cwd: Path | None = self.root
        self.targets: list[str] = []
        self.outside: list[str] = []
        self.approvals: list[str] = []
        self.allowlist = False                      # D-322: check_action's program allowlist
        self.escapes: list[str] = []                # non-write paths outside root (git -C ...)

    def need(self, why: str) -> None:
        if why not in self.approvals:
            self.approvals.append(why)

    def _place(self, text: str, expands: bool, base: Path | None, kind: str) -> Path | None:
        """Resolve *text* the way the shell would, or record why it cannot be judged."""
        if text in _NULL_DEVICES:
            return None
        if expands:
            self.need(f"{kind} built from an expansion: {text}")
            return None
        if text == "~-" or text.startswith("~-/"):
            self.need(f"{kind} uses ~- (previous directory): {text}")
            return None
        if text == "~+" or text.startswith("~+/"):
            if self.cwd is None:
                self.need(f"{kind} uses ~+ after an unresolvable cd: {text}")
                return None
            text = str(self.cwd) + text[2:]
        p = Path(os.path.expanduser(text))
        if not p.is_absolute():
            anchor = base if base is not None else self.cwd
            if anchor is None:
                self.need(f"relative {kind} after an unresolvable cd: {text}")
                return None
            p = anchor / p
        resolved = p.resolve()
        if _unresolved_symlink(resolved):
            raise RuntimeError("symlink loop")
        return resolved

    def target(self, text: str, expands: bool = False, base: Path | None = None) -> None:
        resolved = self._place(text, expands, base, "write target")
        if resolved is None:
            return
        self.targets.append(str(resolved))
        if not resolved.is_relative_to(self.root):
            self.outside.append(str(resolved))
        elif self.allowlist and _in_git_dir(resolved.relative_to(self.root)):
            self.need(f"write into .git ({text}): git runs its hooks and config")

    def confine(self, text: str, what: str, expands: bool = False) -> Path | None:
        """A path the command works in or runs (not a write target) must resolve inside root."""
        resolved = self._place(text, expands, None, what)
        if resolved is not None and not resolved.is_relative_to(self.root):
            self.escapes.append(f"{resolved} ({what})")
        return resolved


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
        elif (a.startswith("--") and "=" in a
              and "--target-directory".startswith(a.split("=", 1)[0])
              and len(a.split("=", 1)[0]) >= 3):
            tdir = a.split("=", 1)[1]
        elif (a.startswith("--") and "--target-directory".startswith(a)
              and len(a) >= 3 and k + 1 < len(args)):
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
    stripped: list[tuple[str, str, list[Token]]] = []  # ("assign", NAME, [tok]) / ("wrap", name, opts)
    while i < len(words):                              # strip wrappers
        w = words[i][1]
        base = os.path.basename(w)
        if _ASSIGN.match(w) or w in _KEYWORDS:
            if _ASSIGN.match(w):
                stripped.append(("assign", w.split("=", 1)[0], [words[i]]))
            i += 1
        elif w == "function":
            i += 2                                     # `function NAME`; the body follows
        elif w in _HEADS and _grammar_prefix(words[:i]):
            texts = [t[1] for t in words]
            if w != "case" and "do" in texts:
                i = texts.index("do") + 1              # `for x do BODY`: the body is a command
                continue
            return                                     # `for N in W`, `case W in`: data only
        elif (inner := _wrapper(scan, words, i)) is not None:
            label = base + " run" if base in ("uv", "poetry", "pipx") else base
            stripped.append(("wrap", label, words[i + (2 if label.endswith(" run") else 1):inner]))
            i = inner
        elif base in _WRAPPERS or base in ("sudo", "doas"):
            stripped.append(("wrap", base, []))
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
    if scan.allowlist:
        _allowed_prefix(scan, stripped)
    if i >= len(words):
        return
    if words[i][2]:
        scan.need(f"command name built from an expansion: {words[i][1]}")
        return
    prog = os.path.basename(words[i][1])
    args = [w[1] for w in words[i + 1:]]
    expands: dict[str, bool] = {}
    for w in words[i + 1:]:                            # rv-785b M1: a quoted copy with the
        expands[w[1]] = expands.get(w[1], False) or w[2]   # same text must not clear the flag
    opts, rest = _split_opts(args)
    if scan.allowlist:
        in_uv = any(s[:2] == ("wrap", "uv run") for s in stripped)
        if in_uv and prog.endswith(".py"):
            scan.confine(words[i][1], "uv run script", words[i][2])   # `uv run s.py`
        else:
            _allowed_program(scan, words[i][1], prog, args, expands, in_uv)

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
            if _unresolved_symlink(scan.cwd):
                raise RuntimeError("symlink loop")
        if scan.allowlist:                             # rv-785b M3: later commands work on
            if scan.cwd is None:                       # cwd without naming a path
                scan.need(f"{prog} to a directory that cannot be resolved statically")
            elif not scan.cwd.is_relative_to(scan.root):
                scan.escapes.append(f"{scan.cwd} ({prog})")
        return
    if prog == "popd":
        scan.cwd = None
        if scan.allowlist:
            scan.need("popd to a directory that cannot be resolved statically")
        return
    family = ("python" if re.fullmatch(r"python[\d.]*", prog) else "node" if prog in ("node", "nodejs")
              else "sh" if prog in _SH_FAMILY else prog)
    if family in _INTERP_VALUED or family in ("php", "lua"):
        opts, rest = _interp_opts(args, _INTERP_VALUED.get(family, set()))
    if (rest[:1] and (rest[0] in _INTERPRETERS_STDIN_PATHS
                     or re.fullmatch(r"/dev/fd/\d+", rest[0])) and family in (
            "python", "node", "sh", "perl", "ruby")):
        scan.need(f"{prog} {rest[0]} (code from stdin)")
    extra_flags = {
        "bun": {"-e", "--eval", "-p"}, "tsx": {"-e", "--eval"},
        "ts-node": {"-e", "--eval"}, "Rscript": {"-e"}, "julia": {"-e", "--eval"},
        "osascript": {"-e"}, "expect": {"-c"},
    }
    if prog in extra_flags and any(
            a.split("=", 1)[0] in extra_flags[prog] for a in args):
        scan.need(f"{prog} one-liner")
    if prog == "deno" and rest[:1] == ["eval"]:
        scan.need("deno eval")
    if prog in ("pwsh", "powershell") and any(
            a.lower().startswith(("-c", "-e")) for a in args):
        scan.need(f"{prog} command string")
    if prog == "powershell" and any(
            not a.startswith("-") and not any(
                b.lower() in ("-file", "-f") for b in args[:k])
            for k, a in enumerate(args)):
        scan.need("powershell positional command string")
    if prog == "trap" and args and args[0] not in ("", "-", "-p", "-l"):
        scan.need("trap runs a command string later")
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
    elif prog in _SH_FAMILY:
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
        symbolic = prog in ("ln", "cp") and any(
            o in ("--symbolic", "--symbolic-link") or (
                o.startswith("-") and not o.startswith("--") and "s" in o)
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
    elif prog in ("sed", "gsed"):
        _sed_effects(scan, args, tgt)  # gsed: GNU sed on macOS/BSD
    elif prog in ("awk", "gawk", "mawk", "nawk"):
        _awk_effects(scan, args)
    elif prog == "find":
        _find_effects(scan, args, tgt)
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


def _grammar_prefix(words: list[Token]) -> bool:
    """Only shell grammar may precede a reserved word in this segment."""
    return all(w[1] in _KEYWORDS | {"time"} or (
        w[1] == "-p" and k > 0 and words[k - 1][1] == "time")
        for k, w in enumerate(words))


def _has_loop_or_function(cmd: str) -> bool:
    # Quoting a reserved word makes it an ordinary command name, not shell grammar.
    syntax = re.sub(r"""(?:\$'(?:\\.|[^'\\])*'|'[^']*'|"(?:\\.|[^"\\])*"|\\.)""",
                    "__quoted__", cmd)
    toks, _ = _tokenize(syntax)
    position = True
    for k, (kind, text, _) in enumerate(toks):
        if kind == "op":
            if text in _SEPARATORS:
                position = True
            continue
        if not position:
            continue
        if text in {"for", "while", "until", "select", "function"}:
            return True
        if re.fullmatch(r"[A-Za-z_]\w*", text) and toks[k + 1:k + 3] == [
                ("op", "(", False), ("op", ")", False)]:
            return True
        if text not in _KEYWORDS | {"time", "-p"} and not _ASSIGN.match(text):
            position = False
    return False


def _without_single_quotes(cmd: str) -> str:
    """Arithmetic punctuation in single-quoted data is not evaluated."""
    result: list[str] = []
    i, double = 0, False
    while i < len(cmd):
        c = cmd[i]
        if c == "\\":
            result.append(cmd[i:i + 2])
            i += 2
            continue
        if c == '"':
            double = not double
        if c == "'" and not double:
            ansi = i > 0 and cmd[i - 1] == "$"
            i += 1
            while i < len(cmd):
                if ansi and cmd[i] == "\\":
                    i += 2
                elif cmd[i] == "'":
                    i += 1
                    break
                else:
                    i += 1
            result.append(" ")
            continue
        result.append(c)
        i += 1
    return "".join(result)


def _sed_script_effects(script: str) -> tuple[list[str], bool, bool]:
    """Read sed syntax so letters inside regexes and replacements stay data."""
    i, n, depth = 0, len(script), 0
    files: list[str] = []
    runs = False

    def blanks() -> None:
        nonlocal i
        while i < n and script[i] in " \t\r":
            i += 1

    def delimited(delim: str) -> bool:
        nonlocal i
        while i < n:
            c = script[i]
            i += 1
            if c == "\\":
                if i == n:
                    return False
                i += 1
            elif c == delim:
                return True
        return False

    def address(second: bool = False) -> bool:
        nonlocal i
        blanks()
        m = re.match(r"(?:\d+(?:~\d+)?|\$" + (r"|[+~]\d+" if second else "") + ")",
                     script[i:])
        if m:
            i += len(m[0])
            return True
        if i < n and script[i] == "/":
            i += 1
            if not delimited("/"):
                raise ValueError("sed address")
            return True
        if i + 1 < n and script[i] == "\\":
            delim = script[i + 1]
            i += 2
            if not delimited(delim):
                raise ValueError("sed address")
            return True
        return False

    def line() -> str:
        nonlocal i
        start = i
        while i < n and script[i] != "\n":
            i += 1
        return script[start:i].strip()

    try:
        while i < n:
            if script[i] in " \t\r;\n":
                i += 1
                continue
            if address():
                blanks()
                if i < n and script[i] == ",":
                    i += 1
                    if not address(True):
                        return files, runs, False
            blanks()
            if i < n and script[i] == "!":
                i += 1
                blanks()
            if i == n:
                return files, runs, False
            c = script[i]
            i += 1
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth < 0:
                    return files, runs, False
            elif c in "#aicrR":
                line()
            elif c in "wW":
                name = line()
                if not name:
                    return files, runs, False
                files.append(name)
            elif c == "e":
                runs = True
                line()
            elif c in ":btT":
                while i < n and script[i] not in ";\n":
                    i += 1
            elif c in "sy":
                if i == n or script[i] in "\\\n":
                    return files, runs, False
                delim = script[i]
                i += 1
                if not delimited(delim) or not delimited(delim):
                    return files, runs, False
                if c == "s":
                    while i < n and script[i] in "gpiImMe0123456789":
                        runs |= script[i] == "e"
                        i += 1
                    blanks()
                    if i < n and script[i] == "w":
                        i += 1
                        name = line()
                        if not name:
                            return files, runs, False
                        files.append(name)
            elif c in "=dDgGhHlnNpPqQxzFL":
                if c in "qQlL":
                    blanks()
                    while i < n and script[i].isdigit():
                        i += 1
            else:
                return files, runs, False
            blanks()
            if c != "{" and i < n and script[i] not in ";\n}":
                return files, runs, False
        return files, runs, depth == 0
    except ValueError:
        return files, runs, False


def _long_option(arg: str, options: set[str]) -> str | None:
    """Resolve exact names first, then unambiguous GNU-style prefixes."""
    name = arg.split("=", 1)[0]
    if name in options:
        return name
    matches = [option for option in options if option.startswith(name)]
    return matches[0] if len(matches) == 1 else None


def _sed_effects(scan: _Scan, args: list[str], tgt) -> None:
    long_options = {
        "--expression", "--file", "--in-place", "--line-length", "--null-data",
        "--zero-terminated", "--quiet", "--silent", "--regexp-extended", "--separate",
        "--sandbox", "--unbuffered", "--posix", "--debug", "--follow-symlinks",
        "--binary", "--help", "--version",
    }
    scripts: list[str] = []
    files: list[str] = []
    explicit = inplace = False
    k, end = 0, False
    while k < len(args):
        a = args[k]
        if not end and a == "--":
            end = True
        elif not end and a.startswith("--"):
            option = _long_option(a, long_options)
            if option is None:
                scan.need("sed option not recognised")
            elif option == "--in-place":
                inplace = True
            elif option in ("--expression", "--file", "--line-length"):
                value = a.split("=", 1)[1] if "=" in a else None
                if value is None:
                    k += 1
                    value = args[k] if k < len(args) else None
                if value is None:
                    scan.need("sed option value missing")
                if option != "--line-length":
                    explicit = True
                    if option == "--file":
                        scan.need("sed script from a file")
                    elif value is not None:
                        scripts.append(value)
            elif "=" in a:
                scan.need("sed option not recognised")
        elif not end and a.startswith("-") and a != "-":
            j = 1
            while j < len(a):
                option = a[j]
                j += 1
                if option not in "nErsuziefl":
                    scan.need("sed option not recognised")
                    break
                if option == "i":
                    inplace = True
                    break  # The remainder is an optional backup suffix.
                if option in "efl":
                    value = a[j:] or None
                    if value is None:
                        k += 1
                        value = args[k] if k < len(args) else None
                    if value is None:
                        scan.need("sed option value missing")
                    if option != "l":
                        explicit = True
                        if option == "f":
                            scan.need("sed script from a file")
                        elif value is not None:
                            scripts.append(value)
                    break
        else:
            files.append(a)
        k += 1
    if not explicit and files:
        scripts.append(files.pop(0))
    if inplace:
        for name in files:
            tgt(name)
    if not scripts and not explicit:
        scan.need("sed script could not be parsed")
    if scripts:
        # sed joins -e fragments; a block may begin in one and end in another.
        writes, runs, ok = _sed_script_effects("\n".join(scripts))
        for name in writes:
            if "$" in name or "`" in name:
                # the shell expands it before sed runs; the real path is unknown
                scan.need("sed w file name built from an expansion")
            else:
                tgt(name)
        if runs:
            scan.need("sed e command runs a shell command")
        if not ok:
            scan.need("sed script could not be parsed")


def _awk_runs_or_writes(program: str) -> bool:
    """Tokenize awk before checking effects; strings, regexes and comments are data."""
    # awk joins a backslash-newline continuation; without this `print "a" \<nl> > F` hides F
    program = program.replace("\\\n", " ")
    tokens: list[str] = []
    i, regex_next = 0, True
    while i < len(program):
        c = program[i]
        if c in " \t\r":
            i += 1
            continue
        if c == "#":
            end = program.find("\n", i)
            i = len(program) if end < 0 else end
            continue
        if c == '"' or (c == "/" and regex_next):
            delim = c
            start = i
            i += 1
            bracket = False
            while i < len(program):
                c = program[i]
                i += 1
                if c == "\\":
                    i += 1
                elif delim == "/" and c == "[":
                    bracket = True
                elif delim == "/" and c == "]":
                    bracket = False
                elif c == delim and not bracket:
                    break
            else:
                return True  # malformed program cannot be judged
            # keep a string's text so a redirect to a standard stream can be recognised
            tokens.append("STR:" + program[start + 1:i - 1] if delim == '"' else "VALUE")
            regex_next = False
            continue
        m = re.match(r"[A-Za-z_]\w*|\d+(?:\.\d+)?", program[i:])
        if m:
            token = m[0]
            i += len(token)
        else:
            token = next((op for op in ("|&", "||", "&&", ">>", ">=", "<=", "==", "!=",
                                        "++", "--", "!~") if program.startswith(op, i)), c)
            i += len(token)
        tokens.append(token)
        regex_next = token in {"(", ",", "{", "}", ";", "!", "~", "&&", "||", "?", ":",
                               "\n", "=", "+", "-", "*", "/", "%", "<", ">", ">=", "<=",
                               "==", "!=", "!~", "+=", "-=", "^", "|", ">>", "$",
                               "print", "printf", "return"}
    depth = 0
    prints: list[int] = []
    for k, token in enumerate(tokens):
        nxt = tokens[k + 1:k + 2]
        if token == "@" or token == "system" and nxt == ["("]:
            return True
        if token == "|&" or token == "|" and nxt == ["getline"]:
            return True
        if token in ("print", "printf"):
            prints.append(depth)
        elif token in (";", "\n", "}"):
            prints.clear()
        elif token == "(":
            depth += 1
        elif token == ")":
            prints = [d for d in prints if d < depth]
            depth -= 1
        elif token in (">", ">>", "|") and depth in prints:
            if token != "|" and nxt and nxt[0].startswith("STR:") and nxt[0][4:] in _NULL_DEVICES:
                continue  # print > "/dev/stderr" writes no file
            return True
    return False


def _awk_effects(scan: _Scan, args: list[str]) -> None:
    long_options = {
        "--file", "--source", "--assign", "--field-separator", "--load", "--include",
        "--exec", "--dump-variables", "--pretty-print", "--profile", "--debug", "--lint",
        "--posix", "--traditional", "--sandbox", "--characters-as-bytes", "--use-lc-numeric",
        "--non-decimal-data", "--optimize", "--no-optimize", "--re-interval", "--bignum",
        "--copyright", "--gen-pot", "--help", "--version", "--csv",
    }
    valued = {"--file", "--source", "--assign", "--field-separator", "--load",
              "--include", "--exec"}
    short = {"f": "--file", "e": "--source", "v": "--assign", "F": "--field-separator",
             "l": "--load", "i": "--include", "E": "--exec", "o": "--pretty-print",
             "d": "--dump-variables", "p": "--profile"}
    outputs = {"--pretty-print": "awkprof.out", "--dump-variables": "awkvars.out",
               "--profile": "awkprof.out"}
    programs: list[str] = []
    positionals: list[str] = []
    explicit = False
    k, end = 0, False
    while k < len(args):
        a = args[k]
        if not end and a == "--":
            end = True
        elif not end and a.startswith("-") and a != "-":
            value = None
            if a.startswith("--"):
                option = _long_option(a, long_options)
                if "=" in a:
                    value = a.split("=", 1)[1]
            else:
                option = short.get(a[1])
                value = a[2:] or None
                if a.startswith("-W"):
                    scan.need("mawk -W may run code")
            if option is None:
                scan.need("awk option not recognised")
            elif option in outputs:
                scan.target(value if value is not None else outputs[option])
            elif option in valued:
                if value is None:
                    k += 1
                    value = args[k] if k < len(args) else None
                if value is None:
                    scan.need("awk option value missing")
                if option in ("--file", "--load", "--include", "--exec"):
                    scan.need("awk loads code from a file or library")
                elif option == "--source":
                    explicit = True
                    if value is not None:
                        programs.append(value)
        else:
            positionals.append(a)
        k += 1
    if not explicit:
        programs = positionals[:1]
    if any(_awk_runs_or_writes(p) for p in programs):
        scan.need("awk program can run commands or write files")


def _find_effects(scan: _Scan, args: list[str], tgt) -> None:
    nullary = {
        "-delete", "-print", "-print0", "-ls", "-prune", "-quit", "-depth", "-xdev",
        "-mount", "-empty", "-true", "-false", "-follow", "-nouser", "-nogroup",
        "-readable", "-writable", "-executable", "-daystart", "-noleaf",
        "-ignore_readdir_race", "-noignore_readdir_race", "-d",
        "-nowarn", "-warn", "-acl", "-xattr", "-help", "-version",
    }
    operators = {"(", ")", "!", ",", "-a", "-o", "-and", "-or", "-not"}
    paths: list[str] = []
    follows = False
    k = 0
    while k < len(args):
        a = args[k]
        if a in ("-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x") or re.fullmatch(
                r"-O[0-3]", a):
            follows |= a in ("-H", "-L")
        elif a == "-D":
            k += 1
        elif a == "-f":
            if k + 1 < len(args):
                paths.append(args[k + 1])
            k += 1
        else:
            break
        k += 1
    while k < len(args) and not args[k].startswith(("-", "(", ")", "!", ",")):
        paths.append(args[k])
        k += 1
    delete = False
    while k < len(args):
        a = args[k]
        if a in operators:
            k += 1
            continue
        if a in ("-exec", "-execdir", "-ok", "-okdir"):
            scan.need("find -exec runs arbitrary commands")
            k += 1
            while k < len(args) and args[k] not in (";", "+"):
                k += 1
            k += 1
            continue
        follows |= a == "-follow"
        delete |= a == "-delete"
        if a.startswith("-") and a not in nullary:
            if a in ("-fprint", "-fprint0", "-fprintf", "-fls") and k + 1 < len(args):
                tgt(args[k + 1])
            k += 3 if a == "-fprintf" else 2
        else:
            if not a.startswith("-"):
                paths.append(a)
            k += 1
    if delete:
        for name in paths or ["."]:
            tgt(name)
        if follows:
            scan.need("find follows symlinks while deleting")


def _wrapper(scan: _Scan, words: list[Token], i: int) -> int | None:
    """Return the inner argv index, recording wrapper writes before unwrapping."""
    base = os.path.basename(words[i][1])
    args = [w[1] for w in words]
    n = len(args)
    if base in ("chroot", "watch"):
        scan.need(f"{base} changes path meaning or runs a command string")
        return n
    sub = {"pdm": "run", "hatch": "run", "conda": "run", "rye": "run", "pipenv": "run",
           "pnpm": "exec", "bundle": "exec"}.get(base)
    if base in ("uvx", "npx", "bunx", "xvfb-run", "dbus-run-session", "su", "runuser",
                "pkexec", "systemd-run", "nsenter", "unshare") or (
            sub is not None and sub in args[i + 1:]):
        scan.need(f"{base} runs another command")
        return n
    if base in ("uv", "poetry", "pipx"):
        if i + 1 >= n or args[i + 1] != "run":
            if "run" in args[i + 2:]:
                scan.need("options before run")
                return n
            return None
        base += " run"
        i += 1
    valued = {
        "ionice": {"-c", "-n", "-t", "-p", "--class", "--classdata", "--pid", "--pgid", "--uid"},
        "taskset": {"-c", "--cpu-list"},
        "flock": {"-w", "-E", "--timeout", "--conflict-exit-code"},
        "strace": {"-e", "-p", "-s", "-u", "-a", "-P", "-I", "-b", "-X", "-O", "-S"},
        "unbuffer": set(), "caffeinate": {"-t", "-w"},
        "fakeroot": {"-s", "-i", "-l", "-b"},
        "busybox": set(),
        "uv run": {"--with", "--with-editable", "--with-requirements", "-p", "--python",
                   "--package", "--extra", "--group", "--env-file", "--index", "--index-url",
                   "--extra-index-url", "-w", "--only-group", "--no-group", "--default-index",
                   "--find-links", "-f", "--upgrade-package", "-P", "--reinstall-package",
                   "--refresh-package", "--resolution", "--prerelease", "--index-strategy",
                   "--keyring-provider", "--exclude-newer", "--exclude-newer-package",
                   "--link-mode", "--config-setting", "-C", "--no-build-package",
                   "--no-binary-package", "--python-platform", "--cache-dir", "--config-file",
                   "--color", "--allow-insecure-host", "--no-sources-package", "--fork-strategy"},
        "pipx run": {"--spec", "--python"}, "poetry run": set(),
    }
    if base not in valued:
        return None
    i += 1
    if base == "busybox":
        return i
    cpu_list = False
    pid = False
    while i < n and args[i].startswith("-") and args[i] != "-":
        a = args[i]
        if a == "--":
            i += 1
            break
        if base == "uv run" and a.split("=", 1)[0] in ("--directory", "--project"):
            scan.need("uv run changes directory")
            return n
        if base == "uv run" and a.split("=", 1)[0] not in valued[base] | {
                "--frozen", "--locked", "--no-sync", "--isolated", "--no-project", "--all-extras",
                "--no-dev", "--dev", "--no-default-groups", "--all-groups", "--script", "-s",
                "--module", "-m", "--gui-script", "--no-cache", "-n", "--offline", "--quiet",
                "-q", "--verbose", "-v", "--active", "--no-active", "--exact", "--inexact",
                "--native-tls", "--refresh", "--reinstall", "--upgrade", "-U", "--no-build",
                "--no-binary", "--compile-bytecode", "--no-env-file", "--no-editable",
                "--all-packages", "--no-config", "--no-progress", "--preview",
                "--no-python-downloads", "--managed-python", "--no-managed-python"}:
            # an option we do not know may take a value and hide the inner command
            scan.need("uv run option not recognised")
            return n
        if base == "poetry run" and a.split("=", 1)[0] in (
                "-C", "--directory", "-P", "--project"):
            scan.need("poetry run changes directory")
            return n
        if base == "fakeroot" and a.startswith("-s"):
            name = args[i + 1] if a == "-s" and i + 1 < n else a[2:]
            if name:
                scan.target(name, any(t[1] == name and t[2] for t in words))
            else:
                scan.need("fakeroot state file missing")
            i += 2 if a == "-s" else 1
            continue
        if base == "flock" and (a in ("-c", "--command") or a.startswith("--command=")):
            scan.need("flock runs a command string")
            return n
        if base == "taskset":
            cpu_list |= a in ("-c", "--cpu-list") or a.startswith("--cpu-list=")
            pid |= a == "-p" or (not a.startswith("--") and "p" in a[1:])
        if base == "ionice" and (a.startswith("-p") or a.split("=", 1)[0] in (
                "--pid", "--pgid", "--uid")):
            pid = True
        if base == "strace" and (a == "-o" or a.startswith(("-o", "--output="))):
            name = args[i + 1] if a == "-o" and i + 1 < n else (
                a.split("=", 1)[1] if a.startswith("--output=") else a[2:])
            if name.startswith(("|", "!")):
                scan.need("strace output runs a command")
            else:
                scan.target(name, any(t[1] == name and t[2] for t in words))
            i += 2 if a == "-o" else 1
            continue
        i += 2 if a in valued[base] else 1
    if pid:
        return n  # -p operates on an existing process, not an inner command
    if base == "taskset" and not cpu_list:
        i += 1  # affinity mask operand
    if base == "flock" and i < n:
        if not args[i].isdigit():
            scan.target(args[i], words[i][2])
        i += 1
        if i < n and args[i] in ("-c", "--command"):
            scan.need("flock runs a command string")
            return n
    return i


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
    """Judge before execution; parse and path-resolution errors need approval."""
    return _check(cmd, root, allowlist=False)


def _check(cmd: str, root: Path, allowlist: bool) -> Verdict:
    if not cmd.strip():
        return Verdict(ALLOW)
    try:
        toks, subst = _tokenize(cmd)
    except ValueError as exc:
        return Verdict(NEEDS_APPROVAL, f"cannot parse command ({exc})" + _NOT_WIRED)
    try:
        return _check_tokens(cmd, root, toks, subst, allowlist)
    except (ValueError, RuntimeError, OSError) as exc:
        return Verdict(NEEDS_APPROVAL,
                       f"cannot judge command ({type(exc).__name__})" + _NOT_WIRED)


def _check_tokens(cmd: str, root: Path, toks: list[Token], subst: bool,
                  allowlist: bool = False) -> Verdict:
    scan = _Scan(root)
    scan.allowlist = allowlist
    if subst:
        scan.need(_SUBST_WHY)
    arithmetic = (re.search(r"(?<![\w$-])(?:let|declare|typeset|read)\b|\blocal\s+-\w*i", cmd)
                  or re.search(r"\(\(|\[|\$\{!", _without_single_quotes(cmd)))
    if arithmetic and _raw_nested_exec(cmd):
        scan.need(_ARITH_WHY)
    if re.search(r"(?<![\w-])(?:cd|pushd|popd)(?![\w-])", cmd) and _has_loop_or_function(cmd):
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
            if kind == "word" and _grammar_prefix(words) and text == "case":
                cwd_stack.append(("case", None))
            elif kind == "word" and _grammar_prefix(words) and text == "esac":
                while cwd_stack and cwd_stack[-1][0] != "case":
                    cwd_stack.pop()
                if cwd_stack:
                    cwd_stack.pop()
            words.append((kind, text, exp))
        k += 1
    flush("")

    targets = tuple(scan.targets)
    if scan.outside or scan.escapes:
        why = []
        if scan.outside:
            why.append("write target outside project root: " + ", ".join(scan.outside))
        if scan.escapes:
            why.append("path outside project root: " + ", ".join(scan.escapes))
        return Verdict(REFUSE, "; ".join(why) + " — not run", targets)
    if scan.approvals:
        return Verdict(NEEDS_APPROVAL, "; ".join(scan.approvals) + _NOT_WIRED, targets)
    return Verdict(ALLOW, "", targets)


# ── D-322 (SAFEFIX): the local-action program allowlist ──────────────────────
#
# UN-15 measured that llm_act refused only what its denylist knew (`crontab -r`,
# `diskutil eraseDisk`, `unlink ~/.zshrc` ran) and that no lane read an allowed
# program's own output/exec flags (`pytest --basetemp=<dir>` runs rm -rf on it,
# `rg --pre rm` deletes every file it searches). So check_action now runs only
# programs named here, and reads the flags of each one that can write, delete or
# run something. A program not named here needs approval, which is not wired, so
# it is refused. Paths a command works in (`git -C`, a script) must be inside root.

#: Programs with no write/delete/exec flag of their own. Writers among them
#: (rm, cp, mv, tee, touch, mkdir, sed, awk) have their targets read by _segment.
_ALLOW_PLAIN = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "du", "grep", "cut", "diff", "tr",
    "which", "echo", "printf", "pwd", "true", "false", ":", "test", "[", "basename",
    "dirname", "realpath", "readlink", "cd", "pushd", "popd",
    "sleep",                     # tests/test_agent_loop.py drives the lane timeout with it
    "yes",                       # stdout only; tests/test_s_run_command_sequences.py pipes it
    "touch", "mkdir", "cp", "mv", "rm", "tee", "sed", "gsed", "awk", "gawk", "mawk", "nawk",
})
#: Wrappers whose inner command is checked like any other.
_ALLOW_WRAPPERS = frozenset({"time", "timeout", "uv run"})
#: Variables a command prefix may set. Others change what an allowed program runs
#: (GIT_EXTERNAL_DIFF, PYTEST_ADDOPTS, RUSTC_WRAPPER, PATH, LD_PRELOAD ...).
_ALLOW_ENV = frozenset({
    "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHONHASHSEED",
    "NO_COLOR", "FORCE_COLOR", "CI", "TERM", "LANG", "LC_ALL", "TZ", "COLUMNS",
})
#: `python -m` modules: test runners and compilers, nothing that evaluates a string
#: (timeit, runpy, code, pdb, cProfile, trace) or unpacks an archive (tarfile, zipfile).
_PY_SAFE_MODULES = frozenset({"pytest", "unittest", "py_compile", "compileall", "json.tool", "ruff"})
_FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint",
                           "-fprint0", "-fprintf", "-fls"})
_GIT_SUBS = frozenset({
    # read
    "status", "log", "diff", "show", "blame", "annotate", "ls-files", "ls-tree", "rev-parse",
    "rev-list", "cat-file", "describe", "shortlog", "grep", "name-rev", "merge-base",
    "show-ref", "for-each-ref", "count-objects", "version", "whatchanged", "range-diff",
    "diff-tree", "diff-index", "diff-files", "check-ignore", "check-attr", "cherry",
    "show-branch", "archive", "reflog", "worktree", "submodule",
    # read forms only (`_git_read_only`)
    "branch", "stash", "tag", "remote", "config",
    # the index writes that ran before D-322 and stay inside the repository
    "add", "mv", "rm",
})
_GIT_READ_FORMS = frozenset({"branch", "stash", "tag", "remote", "config"})
#: `git -c KEY=VALUE` keys that only change formatting. Any other key can run a
#: command (core.pager, alias.*, core.fsmonitor, core.sshCommand, diff.external,
#: *.textconv, filter.*, credential.helper, include.path ...), so it is refused.
_GIT_C_SAFE = re.compile(
    r"(?i)(color|column|advice|log|i18n)(\..+)?|core\.(quotepath|abbrev)"
    r"|diff\.(renames|noprefix|mnemonicprefix|relative)")
_GO_SUBS = frozenset({"build", "test", "vet", "list", "version", "env", "doc", "run"})
_GO_OUT = frozenset({"o", "coverprofile", "cpuprofile", "memprofile", "blockprofile",
                     "mutexprofile", "trace", "outputdir", "modfile", "pkgdir"})
_GO_EXEC = frozenset({"exec", "toolexec", "vettool", "overlay"})
#: Other go build/test flags that take a separate value (so it is not a package).
_GO_VALUED = frozenset({"p", "run", "bench", "tags", "ldflags", "gcflags", "asmflags", "count",
                        "timeout", "covermode", "coverpkg", "parallel", "benchtime", "cpu",
                        "list", "skip", "mod", "fuzz", "fuzztime", "shuffle", "overlay",
                        "installsuffix", "buildmode", "compiler", "pgo"})
_CARGO_SUBS = frozenset({"build", "b", "check", "c", "test", "t", "run", "r", "bench",
                         "clippy", "doc", "d", "metadata", "tree", "fmt", "version"})
_NPM_SUBS = frozenset({"test", "t", "run", "run-script", "ls", "list"})
_NPM_EXEC_OPTS = frozenset({"--script-shell", "--userconfig", "--globalconfig",
                            "--node-options", "--shell", "--editor", "--browser"})
_VERSION_FLAGS = frozenset({"--version", "-V", "-v", "--help", "-h"})


def _getopt(args: list[str], short_valued: str = "",
            long_valued: frozenset[str] = frozenset()) -> tuple[list[tuple[str, str | None]], list[str]]:
    """getopt-style walk: ([(option, value)], positionals). Long options accept GNU
    prefixes (`--out=x` is `--output=x`); `--` ends the options."""
    opts: list[tuple[str, str | None]] = []
    pos: list[str] = []
    k = 0
    while k < len(args):
        a = args[k]
        if a == "--":
            pos += args[k + 1:]
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            full = _long_option(name, set(long_valued)) if long_valued else None
            if full and not eq and k + 1 < len(args):
                val, k = args[k + 1], k + 1
            opts.append((full or name, val if (eq or full) else None))
        elif a.startswith("-") and a != "-":
            for j, ch in enumerate(a[1:], 1):
                if ch in short_valued:
                    val = a[j + 1:]
                    if not val and k + 1 < len(args):
                        val, k = args[k + 1], k + 1
                    opts.append(("-" + ch, val))
                    break
                opts.append(("-" + ch, None))
        else:
            pos.append(a)
        k += 1
    return opts, pos


#: Programs that run code or take arbitrary paths. EVERY argument of these (and
#: every `=`/`:` value inside one, after `--` too) that names a path must resolve
#: inside root: a per-flag table cannot list plugin and test-binary flags
#: (`pytest -o log_file=`, `node --test-reporter-destination=`, `cargo test --
#: --logfile`, `go test -args -test.cpuprofile=`), so the rule is general (rv-785 M2).
_RUNNERS = frozenset({"python", "node", "pytest", "go", "cargo", "uv", "pip", "pip3", "npm",
                      "yarn", "ruff"})
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
#: PATH directories inside the root that may hold the project's own tools (the
#: accepted "code the lane runs" residual): a virtualenv or node_modules/.bin.
_PROJECT_BIN = re.compile(r"(^|/)(\.?venv|env|node_modules)(/[^/]+)*/(bin|\.bin|Scripts)$")


def _path_parts(word: str) -> list[str]:
    """The path-shaped pieces of one argument: the word, the value after each `=`,
    and each `:`-separated part; URLs are not local paths (file:// is)."""
    cands = [word] + [word.split("=", k)[-1] for k in range(1, word.count("=") + 1)]
    if word.startswith("@"):
        cands.append(word[1:])                 # `pytest @args.txt` reads options from a file
    out: list[str] = []
    for c in cands:
        if c.startswith("file://"):
            c = c[7:]
        elif _URL.match(c):
            continue
        for part in ([c] + c.split(":") if ":" in c else [c]):
            if part and ("/" in part or part.startswith("~") or part == ".."):
                out.append(part)
    return out


_UV_INDEX_OPTS = frozenset({"--index", "--index-url", "-i", "--extra-index-url", "--default-index",
                            "--find-links", "-f", "--upgrade", "-U", "--upgrade-package", "-P",
                            "--reinstall", "--reinstall-package", "--refresh", "--refresh-package",
                            "--keyring-provider", "--config-file", "--env-file"})


def _confine_words(scan: _Scan, words: list[str], ex: dict[str, bool], what: str) -> None:
    for w in words:
        # rv-785b M2: a runner expands `$VAR` itself (pytest expandvars a quoted
        # `--junitxml=$HOME/x`), so `$` or a backtick needs approval, quoted or not.
        if ex.get(w, False) or "$" in w or "`" in w:
            scan.need(f"{what} argument built from an expansion: {w}")
            continue
        for part in _path_parts(w):
            scan.confine(part, f"{what} argument")


def _allowed_prefix(scan: _Scan, stripped: list[tuple[str, str, list[Token]]]) -> None:
    for kind, name, toks in stripped:
        if kind == "assign":
            if name not in _ALLOW_ENV:
                scan.need(f"setting {name} can change what an allowed program runs "
                          f"(allowed: {', '.join(sorted(_ALLOW_ENV))})")
            else:                               # PYTHONPATH=<outside> loads code from there
                value = toks[0][1].split("=", 1)[1]
                _confine_words(scan, [value], {value: toks[0][2]}, f"{name}=")
        elif name not in _ALLOW_WRAPPERS:
            scan.need(f"'{name}' is not on the local-action allowlist")
        elif name == "uv run":
            opts = [t[1] for t in toks]
            if any(o.split("=", 1)[0] in ("--with", "--with-editable", "--with-requirements")
                   or (o.startswith("-w") and not o.startswith("--")) for o in opts):
                scan.need("uv run --with installs packages that then run")
            uv_ex: dict[str, bool] = {}
            for t in toks:
                uv_ex[t[1]] = uv_ex.get(t[1], False) or t[2]
            if any(o.split("=", 1)[0] in _UV_INDEX_OPTS for o in opts):
                scan.need("uv run with an index, find-links, upgrade or refresh option "
                          "installs packages that then run")
            _confine_words(scan, opts, uv_ex, "uv run")


def _program_location(scan: _Scan, word: str, prog: str, in_uv: bool) -> None:
    """rv-785 S2: what file would run. A path (`./cat`, `src/find`, `/x/python3`)
    is refused outright: the basename says nothing about the file. A bare name runs
    what PATH finds; refused when PATH has a relative entry or finds a file inside
    the root (a file the model could have written), except a project venv or
    node_modules/.bin, which is the documented residual. `uv run` resolves inside
    the project's own environment, the same residual."""
    if "/" in word:
        scan.need(f"'{word}': a program given as a path is not judged by its name; "
                  "use the bare name")
        return
    if in_uv:
        return
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            scan.need(f"PATH has a relative entry ({d or 'empty'}), so '{prog}' could be a project file")
            return
        cand = Path(d) / prog
        if cand.is_file() and os.access(cand, os.X_OK):
            real, d_real = cand.resolve(), Path(d).resolve()
            in_bin = d_real.is_relative_to(scan.root) and _PROJECT_BIN.search(
                d_real.relative_to(scan.root).as_posix())
            if real.is_relative_to(scan.root) and not in_bin:
                scan.need(f"'{prog}' resolves to {real}, a file inside the project root")
            return


def _allowed_program(scan: _Scan, word: str, prog: str, args: list[str],
                     expands: dict[str, bool], in_uv: bool = False) -> None:
    """D-322: *prog* must be on the allowlist, the file that runs must be a system
    program (`_program_location`), and its own flags must stay inside root."""
    family = ("python" if re.fullmatch(r"python[\d.]*", prog) else "node" if prog == "nodejs"
              else "pytest" if prog == "py.test" else prog)
    if family not in _ALLOW_PLAIN and family not in _CHECKS:
        scan.need(f"'{prog}' is not on the local-action allowlist")
        return
    _program_location(scan, word, prog, in_uv)
    if family in _RUNNERS:
        _confine_words(scan, args, expands, prog)
    check = _CHECKS.get(family)
    if check is not None:
        check(scan, args, expands)


def _al_find(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    hit = next((a for a in args if a in _FIND_ACTIONS), None)
    if hit:
        scan.need(f"find {hit} deletes, writes or runs commands")


def _al_rg(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    if any(a.split("=", 1)[0] in ("--pre", "--pre-glob") for a in args):
        scan.need("rg --pre runs a command on every file it searches")


def _al_ag(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    if any(a.split("=", 1)[0] == "--pager" for a in args):
        scan.need("ag --pager runs a command")


def _al_sort(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    opts, _pos = _getopt(args, "kotST", frozenset({
        "--output", "--key", "--field-separator", "--buffer-size", "--temporary-directory",
        "--compress-program", "--files0-from", "--random-source", "--batch-size",
        "--parallel", "--sort"}))
    for o, v in opts:
        if o in ("-o", "--output") and v is not None:
            scan.target(v, ex.get(v, False))
        elif o == "--compress-program":
            scan.need("sort --compress-program runs a program")


def _al_uniq(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    _opts, pos = _getopt(args, "fsw", frozenset({"--skip-fields", "--skip-chars", "--check-chars"}))
    if len(pos) > 1:
        scan.target(pos[1], ex.get(pos[1], False))         # uniq INPUT OUTPUT


def _al_tree(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    opts, _pos = _getopt(args, "LPIoHT", frozenset({"--filelimit", "--timefmt", "--charset",
                                                   "--sort"}))
    for o, v in opts:
        if o == "-o" and v is not None:
            scan.target(v, ex.get(v, False))
        elif o == "-R":
            scan.need("tree -R writes a file into every directory")


def _al_git(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    k = 0
    while k < len(args) and args[k].startswith("-"):
        name, eq, val = args[k].partition("=")
        if name.startswith("-c") and len(name) > 2:            # attached `-ckey=value`
            name, eq, val = "-c", "=", args[k][2:]
        if name in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
                    "--super-prefix", "--attr-source") and not eq:
            val, k = (args[k + 1] if k + 1 < len(args) else ""), k + 1
        if name in ("-C", "--git-dir", "--work-tree"):
            scan.confine(val, f"git {name}", ex.get(val, False))
        elif name == "-c":
            key, _eq, value = val.partition("=")
            if not (_GIT_C_SAFE.fullmatch(key) or (key.lower() == "core.pager"
                                                   and _eq and value == "cat")):
                scan.need(f"git -c {key} can make git run a command")
        elif name in ("--config-env", "--super-prefix") or (name == "--exec-path" and eq):
            scan.need(f"git {name} changes what git runs")
        k += 1
    sub, rest = (args[k], args[k + 1:]) if k < len(args) else ("", [])
    if not sub:
        return
    if sub not in _GIT_SUBS:
        scan.need(f"'git {sub}' is not on the local-action allowlist")
        return
    if sub in _GIT_READ_FORMS and not _git_read_only(sub, rest):
        scan.need(f"'git {sub}' with these arguments changes state")
    opts, pos = _getopt(rest, "o" if sub == "archive" else "",
                        frozenset({"--output", "--open-files-in-pager", "--remote", "--exec"}))
    for o, v in opts:
        if o in ("--output", "-o") and v is not None:
            scan.target(v, ex.get(v, False))
        elif o == "--ext-diff":
            scan.need("git --ext-diff runs an external diff program")
        elif o in ("--open-files-in-pager", "-O") and sub == "grep":
            scan.need("git grep --open-files-in-pager runs a command")
        elif o in ("--remote", "--exec") and sub == "archive":
            scan.need(f"git archive {o} runs a command")
        elif sub == "rm" and (o in ("-r", "--recursive")):
            scan.need("'git rm -r' needs approval (recursive delete)")
    if sub == "rm" and any(c in p for p in pos for c in "*?[") or sub == "rm" and any(
            p.startswith(":") for p in pos):
        scan.need("'git rm' with a glob or pathspec magic can delete many files")
    if sub in ("add", "mv", "rm"):
        # rv-785b M4: with the root nested in a repository, `git rm ../x` deletes
        # outside it. Every operand must resolve inside the root, after `--` too.
        if any(o.startswith("--pathspec-from-file") for o, _v in opts):
            scan.need(f"git {sub} --pathspec-from-file reads paths this check cannot see")
        for p in pos:
            if p.startswith(":"):
                scan.need(f"git {sub} {p}: pathspec magic can reach outside the root")
            else:
                scan.confine(p, f"git {sub} operand", ex.get(p, False))
    first = next((a for a in rest if not a.startswith("-")), "")
    if (sub == "reflog" and first in ("expire", "delete", "drop")) or (
            sub == "worktree" and first != "list") or (
            sub == "submodule" and first not in ("", "status", "summary")):
        scan.need(f"'git {sub} {first}' changes state")


def _al_python(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    opts, rest = _interp_opts(args, _INTERP_VALUED["python"])
    module = None
    for o in opts:
        if o.startswith("--"):
            continue
        # rv-785 M1: walk the short-option cluster the way CPython does. `-W`/`-X`
        # take the rest as a value; `c` takes the rest (or the next word) as CODE,
        # attached or not (`-cprint(1)`, `-Ic...`), whatever follows it.
        for k, ch in enumerate(o[1:], 1):
            if ch in "WX":
                break
            if ch == "c":
                scan.need("python -c runs inline code")
                return
            if ch == "m":
                module = o[k + 1:] or (rest.pop(0) if rest else "")
                break
        if module is not None:
            break
    if module is not None:
        if module not in _PY_SAFE_MODULES:
            scan.need(f"python -m {module or '?'} is not on the local-action allowlist "
                      f"(allowed: {', '.join(sorted(_PY_SAFE_MODULES))})")
            return
        _PY_MODULE_CHECKS.get(module, _al_none)(scan, rest, ex)
        return
    if rest and rest[0] not in _INTERPRETERS_STDIN_PATHS:
        scan.confine(rest[0], "python script", ex.get(rest[0], False))


_NODE_LOADERS = ("--require", "--import", "--loader", "--experimental-loader", "-r")


#: node options that take a separate value, so the value is not the script.
_NODE_VALUED = frozenset({
    "-C", "--conditions", "-r", "--require", "--import", "--loader", "--experimental-loader",
    "--input-type", "--title", "--icu-data-dir", "--openssl-config", "--redirect-warnings",
    "--report-dir", "--report-directory", "--report-filename", "--secure-heap",
    "--secure-heap-min", "--test-reporter", "--test-reporter-destination",
    "--test-name-pattern", "--test-skip-pattern", "--test-concurrency", "--watch-path",
    "--env-file", "--env-file-if-exists", "--cpu-prof-dir", "--cpu-prof-name",
    "--heap-prof-dir", "--heap-prof-name", "--diagnostic-dir", "--disable-warning",
    "--dns-result-order", "--unhandled-rejections", "--trace-event-categories",
    "--trace-event-file-pattern", "--localstorage-file", "--experimental-config-file",
    "--run", "--stack-size", "--max-old-space-size", "--inspect-port", "--debug-port",
    "--experimental-sea-config", "--build-snapshot-config", "--snapshot-blob"})
_JS_SCRIPT = re.compile(r".*\.(js|mjs|cjs|ts|mts|cts|jsx|tsx)$")
_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


def _inspect_host(val: str) -> str:
    """The host in a node `--inspect*` value (`host:port`, `[v6]:port`, bare port or host)."""
    if val.startswith("["):
        return val.split("]")[0] + "]"
    if val.isdigit():
        return ""                                   # port only: node binds loopback
    if val.count(":") == 1:
        return val.split(":")[0]
    return val                                      # bare host, or a bare IPv6 address


def _al_node(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    k = 0
    while k < len(args) and args[k] != "--":
        a = args[k]
        if not a.startswith("-") or a == "-":
            # The script. An unknown option that takes a value can make its value look
            # like the script (rv-785b S1: `node -C x -e ...`), so scanning stops only at
            # a word that is clearly a script: a JS file name or a path.
            if _JS_SCRIPT.fullmatch(a) or "/" in a or a == "-":
                break
            k += 1
            continue
        name, eq, val = a.partition("=")
        if name.startswith("--"):
            name = name.replace("_", "-")          # node reads `--inspect_port` as `--inspect-port`
        # rv-785 M1: inline code in any spelling (`-e`, `-pe`, `--eval=...`, `--print`).
        if name in ("--eval", "--print") or (not a.startswith("--") and (
                "e" in a[1:] or "p" in a[1:])):
            scan.need("node -e/-p runs inline code")
            return
        if name in _NODE_VALUED and not eq:
            val, k = (args[k + 1] if k + 1 < len(args) else ""), k + 1
        if name in _NODE_LOADERS:
            # a module from data:/a URL is inline code; a path must be a project file
            if val.startswith("data:") or _URL.match(val) or "://" in val:
                scan.need(f"node {name} {val[:40]} loads code that is not a project file")
                return
            if val.startswith((".", "/", "~")):
                scan.confine(val, f"node {name}", ex.get(val, False))
        if name.startswith(("--inspect", "--debug")) and val and name != "--inspect-publish-uid":
            # rv-785c S1: every --inspect*/--debug* flag counts, including the valued
            # `--inspect-port X` (= or separate), so `--inspect-port=0.0.0.0:9229
            # --inspect` cannot open the debugger on the network.
            host = _inspect_host(val)
            if host and host not in _LOOPBACK:         # rv-785b N2: a debugger on the network
                scan.need(f"node {name} {val} opens the debugger beyond loopback")
        k += 1
    rest = args[k + 1:] if args[k:k + 1] == ["--"] else args[k:]
    if rest and rest[0] not in _INTERPRETERS_STDIN_PATHS:
        scan.confine(rest[0], "node script", ex.get(rest[0], False))


def _al_none(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    return None


def _al_json_tool(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    _opts, pos = _getopt(args, "", frozenset({"--indent"}))
    if len(pos) > 1:
        scan.target(pos[1], ex.get(pos[1], False))         # json.tool INFILE OUTFILE


def _al_compile(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    """py_compile / compileall write __pycache__ next to every source they compile."""
    opts, pos = _getopt(args, "rdspxijoe", frozenset({"--invalidation-mode"}))
    if any(o == "-i" for o, _v in opts) or not pos or "-" in pos:
        scan.need("python -m compileall/py_compile without explicit paths compiles "
                  "files it was not given (sys.path or a list)")
    for p in pos:
        if p != "-":
            scan.target(p, ex.get(p, False))


def _al_unittest(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    opts, _pos = _getopt(args, "stp", frozenset({"--start-directory", "--top-level-directory",
                                                "--pattern"}))
    for o, v in opts:
        if o in ("-s", "-t", "--start-directory", "--top-level-directory") and v:
            scan.confine(v, f"unittest {o}", ex.get(v, False))


_PYTEST_VALUED = frozenset({
    "--basetemp", "--junitxml", "--junit-xml", "--log-file", "--report-log", "--resultlog",
    "--html", "--json-report-file", "--rootdir", "--override-ini", "--cov-report",
    "--deselect", "--ignore", "--ignore-glob", "--maxfail", "--tb", "--durations",
    "--confcutdir", "--cov", "--capture", "--import-mode", "--log-level", "--config-file",
    "--timeout", "--dist", "--log-file-level", "--log-cli-level"})
_PYTEST_OUT = frozenset({"--junitxml", "--junit-xml", "--log-file", "--report-log",
                         "--resultlog", "--html", "--json-report-file", "--debug"})


def _al_pytest(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    opts, pos = _getopt(args, "kmpcoWrn", _PYTEST_VALUED)
    for o, v in opts:
        if v is None:
            continue
        if o == "--basetemp":
            n = len(scan.outside)
            scan.target(v, ex.get(v, False))
            if len(scan.outside) == n and not ex.get(v, False):
                p = scan._place(v, False, None, "pytest --basetemp")
                if p is not None and p.exists():
                    scan.need(f"pytest --basetemp={v}: pytest deletes that directory "
                              "first, and it exists")
        elif o in _PYTEST_OUT:
            scan.target(v, ex.get(v, False))
        elif o == "--rootdir":
            scan.confine(v, "pytest --rootdir", ex.get(v, False))
        elif o == "--cov-report" and ":" in v:
            dest = v.split(":", 1)[1]
            scan.target(dest, ex.get(dest, False))
        elif o in ("-o", "--override-ini"):
            key, _eq, value = v.partition("=")
            if key.strip() == "cache_dir":
                scan.target(value, False)
            elif key.strip() == "addopts":
                scan.need("pytest -o addopts= adds options this check cannot see")
    for p in pos:
        path = p.split("::", 1)[0]
        if path:
            scan.confine(path, "pytest path", ex.get(p, False))


def _al_go(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    k = 0
    if args[:1] == ["-C"] or (args and args[0].startswith("-C=")):
        val = args[0][3:] if args[0].startswith("-C=") else (args[1] if len(args) > 1 else "")
        scan.confine(val, "go -C", ex.get(val, False))
        k = 1 if args[0].startswith("-C=") else 2
    sub = args[k] if k < len(args) else ""
    if not sub or sub in _VERSION_FLAGS:
        return
    if sub not in _GO_SUBS:
        scan.need(f"'go {sub}' is not on the local-action allowlist")
        return
    rest = args[k + 1:]
    if sub == "env" and any(a.startswith("-") and a.lstrip("-").split("=", 1)[0] in ("w", "u")
                            for a in rest):                # `--w`, `-w=true` (rv-785b M5)
        scan.need("go env -w/-u writes the user's go configuration")
    i, pkgs, after_args = 0, [], False
    while i < len(rest):
        a = rest[i]
        if a in ("-args", "--args"):
            after_args = True                  # the rest goes to the test binary
        elif a.startswith("-") and a != "-":
            name, eq, val = a.lstrip("-").partition("=")
            name = name.removeprefix("test.")  # go test accepts -test.<flag> (rv-785 M2)
            if name in _GO_OUT | _GO_EXEC | _GO_VALUED and not eq:
                val, i = (rest[i + 1] if i + 1 < len(rest) else ""), i + 1
            if name in _GO_EXEC:
                scan.need(f"go -{name} runs a program or swaps in other files")
            elif name == "mod" and val == "mod":
                scan.need("go -mod=mod lets go fetch modules from the network")
            elif name in _GO_OUT:
                scan.target(val, ex.get(val, False))
        elif not after_args:
            pkgs.append(a)
        i += 1
    if sub == "run":                           # `go run PKG args...`: only PKG (or *.go files)
        n_go = next((k for k, p in enumerate(pkgs) if not p.endswith(".go")), len(pkgs))
        pkgs = pkgs[:max(n_go, 1)]
    if sub in ("run", "build", "test", "vet", "list"):
        for p in pkgs:
            # rv-785 M3: a module path (`example.com/x/cmd`, `pkg@latest`) is code
            # go downloads; only local packages (`.`, `./...`, `./cmd/x`, x.go) run.
            first = p.split("/", 1)[0]
            own = _go_module(scan.root)         # rv-785b S5: the project's own module path
            if own and "@" not in p and (p == own or p.startswith(own + "/")):
                continue
            if p in ("all", "std", "cmd") and sub != "list":
                scan.need(f"go {sub} {p} runs packages that are not the project's")
                continue
            if "@" in p or ("." in first and first not in (".", "..") and not p.endswith(".go")):
                scan.need(f"go {sub} {p}: a module path is code go downloads, not a "
                          "project package")


def _go_module(root: Path) -> str:
    """The `module` line of the root's go.mod, or ''."""
    try:
        for line in (root / "go.mod").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "module":
                return parts[1].strip('"')
    except OSError:
        return ""
    return ""


def _al_cargo(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    args = args[:args.index("--")] if "--" in args else args
    opts, pos = _getopt(args, "CZjpF", frozenset({
        "--manifest-path", "--target-dir", "--config", "--artifact-dir", "--out-dir",
        "--lockfile-path", "--color", "--jobs", "--package", "--features", "--target",
        "--bin", "--example", "--test", "--bench", "--profile", "--message-format"}))
    pos = [p for p in pos if not p.startswith("+")]                # +toolchain
    if pos and pos[0] not in _CARGO_SUBS:
        scan.need(f"'cargo {pos[0]}' is not on the local-action allowlist")
        return
    for o, v in opts:
        if o == "--config":
            scan.need("cargo --config can set a program cargo runs (build.rustc-wrapper)")
        elif o in ("-C", "--manifest-path") and v is not None:
            scan.confine(v, f"cargo {o}", ex.get(v, False))
        elif o in ("--target-dir", "--artifact-dir", "--out-dir", "--lockfile-path") and v:
            scan.target(v, ex.get(v, False))


def _al_ruff(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    opts, pos = _getopt(args, "o", frozenset({"--output-file", "--cache-dir", "--config",
                                             "--select", "--ignore", "--extend-select",
                                             "--target-version", "--line-length"}))
    if pos and pos[0] not in ("check", "format", "version"):
        scan.need(f"'ruff {pos[0]}' is not on the local-action allowlist")
        return
    for o, v in opts:
        if o in ("-o", "--output-file", "--cache-dir") and v:
            scan.target(v, ex.get(v, False))
    for p in pos[1:]:
        scan.confine(p, "ruff path", ex.get(p, False))


def _al_pip(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    opts, pos = _getopt(args, "i", frozenset({
        "--index-url", "--extra-index-url", "--proxy", "--log", "--log-file", "--cache-dir",
        "--cert", "--client-cert", "--timeout", "--retries", "--exists-action",
        "--trusted-host", "--python", "--keyring-provider"}))
    if pos and pos[0] not in ("list", "show", "freeze", "check"):
        scan.need(f"'pip {pos[0]}' is not on the local-action allowlist")
    for o, v in opts:
        if o in ("--log", "--log-file") and v:
            scan.target(v, ex.get(v, False))


def _al_npm(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    opts, pos = _getopt(args, "Cw", frozenset({"--prefix", "--workspace", "--cache",
                                              "--registry", "--loglevel"}))
    if pos and pos[0] not in _NPM_SUBS:
        scan.need(f"'npm {pos[0]}' is not on the local-action allowlist")
    for o, v in opts:
        if o in ("--prefix", "-C") and v is not None:
            scan.confine(v, f"npm {o}", ex.get(v, False))
        elif o in _NPM_EXEC_OPTS:
            scan.need(f"npm {o} changes what npm runs")


def _al_yarn(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    opts, pos = _getopt(args, "", frozenset({"--cwd"}))
    if pos and pos[0] not in ("test", "run"):
        scan.need(f"'yarn {pos[0]}' is not on the local-action allowlist")
    for o, v in opts:
        if o == "--cwd" and v is not None:
            scan.confine(v, "yarn --cwd", ex.get(v, False))


def _al_uv(scan: _Scan, args: list[str], ex: dict[str, bool]) -> None:
    """`uv run` is a wrapper (_wrapper); this is every other uv subcommand."""
    opts, pos = _getopt(args, "p", frozenset({"--directory", "--project", "--cache-dir",
                                             "--python", "--config-file", "--color"}))
    if pos and not (pos[0] == "tree" or (pos[0] == "pip" and pos[1:2] and pos[1] in (
            "list", "show", "freeze", "check", "tree"))):
        scan.need(f"'uv {' '.join(pos[:2])}' is not on the local-action allowlist")
    for o, v in opts:
        if o in ("--directory", "--project") and v is not None:
            scan.confine(v, f"uv {o}", ex.get(v, False))
        elif o == "--cache-dir" and v:
            scan.target(v, ex.get(v, False))


_PY_MODULE_CHECKS = {"pytest": _al_pytest, "json.tool": _al_json_tool, "py_compile": _al_compile,
                     "compileall": _al_compile, "unittest": _al_unittest, "ruff": _al_ruff}
_CHECKS = {
    "find": _al_find, "rg": _al_rg, "ag": _al_ag, "sort": _al_sort, "uniq": _al_uniq,
    "tree": _al_tree, "git": _al_git, "python": _al_python, "node": _al_node,
    "pytest": _al_pytest, "go": _al_go, "cargo": _al_cargo, "ruff": _al_ruff, "pip": _al_pip,
    "pip3": _al_pip, "npm": _al_npm, "yarn": _al_yarn, "uv": _al_uv,
}


def allowed_programs() -> frozenset[str]:
    """Every program name check_action can run (D-322), for docs and tests."""
    return _ALLOW_PLAIN | frozenset(_CHECKS)


# ── P2.9-a: one policy for the three local lanes ─────────────────────────────

#: Reason ``contain`` gives for a path outside the root; lanes keep their own wording.
REASON_OUTSIDE = "path escapes working directory"


def contain(raw: object, root: Path) -> tuple[Path | None, str | None]:
    """The ONE containment test: ``(resolved path, None)`` or ``(None, reason)``.

    Relative paths are joined to *root*; symlinks are followed; a path that
    resolves outside *root* is refused. Home-relative (``~``) paths are refused
    rather than expanded.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "path is missing or not a string"
    if "\x00" in raw:
        return None, "path contains a NUL byte"
    if raw.strip().startswith("~"):
        return None, "home-directory paths are not available"
    base = Path(root).resolve()
    try:
        candidate = Path(raw)
        resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"path cannot be resolved ({type(exc).__name__})"
    if not resolved.is_relative_to(base):
        return None, REASON_OUTSIDE
    return resolved, None


def check_write(path: object, root: Path) -> Verdict:
    """Verdict form of ``contain`` for a file-tool write."""
    resolved, why = contain(path, root)
    if resolved is None:
        return Verdict(REFUSE, f"{why}: {str(path)[:120]}")
    base = Path(root).resolve()
    if resolved.is_relative_to(base) and _in_git_dir(resolved.relative_to(base)):
        return Verdict(NEEDS_APPROVAL, f"write into .git ({str(path)[:120]}): git runs its "
                       "hooks and config" + _NOT_WIRED)
    return Verdict(ALLOW, "", (str(resolved),))


_ALWAYS_APPROVAL = {"mkfs", "shred", "fdisk", "sudo", "doas", "su",
                 "shutdown", "reboot", "halt", "poweroff"}
_GIT_VALUED = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_PKG_SUBS = {
    "pip": {"install", "uninstall"}, "pip3": {"install", "uninstall"},
    "pipx": {"install", "uninstall", "upgrade", "reinstall", "inject", "run"},
    "npm": {"install", "i", "ci", "add", "uninstall", "un", "remove", "rm", "publish",
            "link", "update", "up", "install-test", "it", "cit", "install-ci-test", "ic",
            "in", "isntall", "exec", "x"},
    "pnpm": {"install", "i", "add", "remove", "rm", "uninstall", "un", "update", "up", "publish",
             "dlx"},
    "yarn": {"install", "add", "remove", "publish", "upgrade", "up", "dlx"},
    "cargo": {"install", "publish", "yank"},
    "go": {"install", "get"}, "gem": {"install"}, "brew": {"install", "uninstall"},
    "apt": {"install", "remove"}, "apt-get": {"install", "remove"},
    "poetry": {"add", "install"},
}
#: Subcommands in agent_writes' table (still used by that lane's allowlist mode)
#: that this policy does NOT treat as destructive: `npm run <script>` is the same
#: risk class as `npm test`, which is allowed.
_TABLE_EXEMPT = {"npm": {"run"}}
#: State-changing git subcommands missing from agent_writes' table (D-146).
_GIT_EXTRA = frozenset({"commit", "pull"})
_SCRIPT_RUNNERS = frozenset({"run", "run-script", "test", "t", "start", "stop", "restart"})
_GIT_TAG_READ = {"-l", "--list"}
_GIT_CONFIG_READ = {"--get", "--get-all", "--get-regexp", "--list", "-l"}


def _git_read_only(sub: str, rest: list[str]) -> bool:
    """Read-only forms of the git subcommands that are otherwise state changes."""
    if sub == "branch":
        from llm_router.hooks import agent_writes
        return all(a in agent_writes._GIT_BRANCH_READ_FLAGS for a in rest)
    if sub == "stash":
        return rest[:1] in (["list"], ["show"])
    if sub == "tag":
        return not rest or rest[0] in _GIT_TAG_READ
    if sub == "remote":
        while rest[:1] and rest[0] in ("-v", "--verbose"):
            rest = rest[1:]
        return not rest or rest[0] in ("show", "get-url")
    if sub == "config":
        return bool(rest) and rest[0] in _GIT_CONFIG_READ
    return False


def _blocked_subcommands() -> dict[str, frozenset[str]]:
    """The git/cargo/go/npm table lives in agent_writes (one table, not a copy)."""
    from llm_router.hooks import agent_writes
    return agent_writes._BLOCKED_SUBCOMMANDS


def _first_sub(args: list[str], valued: set[str] = frozenset()) -> tuple[str, list[str]]:
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            i += 2 if a in valued else 1
            continue
        return a, args[i + 1:]
    return "", []


def _segment_destructive(words: list[str]) -> str | None:
    while words and (words[0] in _KEYWORDS or _ASSIGN.match(words[0])):
        words = words[1:]
    while words and os.path.basename(words[0]) in _WRAPPERS:
        words = words[1:]
        while words and (words[0].startswith("-") or words[0].replace(".", "").isdigit()
                         or _ASSIGN.match(words[0])):
            words = words[1:]
    if not words:
        return None
    prog = os.path.basename(words[0])
    args = words[1:]
    if prog.startswith("mkfs") or prog in _ALWAYS_APPROVAL:
        return f"'{prog}' needs approval"
    opts = [a for a in args if a == "--" or a.startswith("-")]
    opts = opts[:opts.index("--")] if "--" in opts else opts
    cluster = "".join(o[1:] for o in opts if not o.startswith("--"))
    longs = {o for o in opts if o.startswith("--")}
    if prog == "rm" and ("r" in cluster or "R" in cluster or "--recursive" in longs):
        return "'rm -r' needs approval (recursive delete)"
    if prog in ("chmod", "chown") and ("R" in cluster or "--recursive" in longs):
        return f"'{prog} -R' needs approval (recursive change)"
    if prog == "dd" and any(a.startswith(("if=", "of=")) for a in args):
        return "'dd' needs approval (raw device copy)"
    table = _blocked_subcommands()
    if prog == "git":
        sub, rest = _first_sub(args, _GIT_VALUED)
        if (sub in table["git"] or sub in _GIT_EXTRA) and not _git_read_only(sub, rest):
            return f"'git {sub}' changes repository state and needs approval"
        return None
    if prog.startswith("python"):
        if "-m" in args and args[args.index("-m") + 1:args.index("-m") + 2] == ["pip"]:
            return "'python -m pip' needs approval (changes the environment)"
        return None
    if prog == "uv":
        sub, rest = _first_sub(args)
        if sub in ("add", "sync", "remove") or (
                sub in ("pip", "tool") and rest[:1] and rest[0] in ("install", "uninstall")):
            return f"'uv {sub}' needs approval (changes the environment)"
        if sub == "run":                              # `uv run pip install x` runs pip
            inner = [a for a in rest if not a.startswith("-")]
            for i in range(len(inner)):
                why = _segment_destructive(inner[i:])
                if why:
                    return why
            return None
        return None
    sub, _rest = _first_sub(args)
    if prog == "yarn" and not sub and not {"--version", "-v", "--help", "-h"} & set(args):
        return "bare 'yarn' installs dependencies and needs approval"
    # Scan every argument, not just the first positional: `npm --prefix . i x`,
    # `yarn --cwd . add x`, `pip --index-url U install y` carry an option VALUE where
    # the subcommand was expected (same lesson as agent_writes' `git -C /tmp push`).
    # Script runners stop the scan: their remaining words belong to the script.
    for a in args:
        if a in _PKG_SUBS.get(prog, ()):
            return f"'{prog} {a}' needs approval (installs, removes, publishes or runs packages)"
        if a in _SCRIPT_RUNNERS:
            break
    if sub and sub in table.get(prog, ()) and sub not in _TABLE_EXEMPT.get(prog, ()):
        return f"'{prog} {sub}' changes state and needs approval"
    return None


def destructive_reason(cmd: str) -> str | None:
    """Why *cmd* needs approval as a destructive/installing command, else None."""
    try:
        toks, _subst = _tokenize(cmd)
    except ValueError:
        return None                      # check_command already says needs approval
    words: list[str] = []
    skip_next = False
    for kind, text, _exp in toks + [("op", ";", False)]:
        if kind == "op" and text in _SEPARATORS:
            if words:
                why = _segment_destructive(words)
                if why:
                    return why
            words = []
            skip_next = False
        elif kind == "op":
            skip_next = text in _WRITE_REDIRECTS or text in _READ_REDIRECTS
        elif skip_next:
            skip_next = False            # a redirect target is not a word of the command
        else:
            words.append(text)
    return None


def check_action(cmd: str, root: Path) -> Verdict:
    """``check_command`` plus the program allowlist (D-322), then the destructive
    table: refuse > needs_approval > allow."""
    verdict = _check(cmd, root, allowlist=True)
    if verdict.decision == REFUSE:
        return verdict
    why = destructive_reason(cmd)
    if why is not None:
        return Verdict(NEEDS_APPROVAL, why + _NOT_WIRED, verdict.targets)
    return verdict


@dataclass(frozen=True)
class RootChoice:
    root: Path | None
    source: str                          # "mcp" | "env" | "requested" | "none"
    reason: str = ""


def _existing_dir(raw: object) -> Path | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        resolved = Path(str(raw)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_dir() else None


def pick_root(mcp_root: object = None, requested: object = None) -> RootChoice:
    """MCP root, then ``$CLAUDE_PROJECT_DIR``, then *requested*; never the process cwd.

    Known limits: a RELATIVE *requested* resolves against the process cwd (today's
    ``llm_local_task`` behaviour, kept); on a case-insensitive filesystem a path
    spelled with different case than the root fails ``contain`` closed (a false
    refusal, never an escape)."""
    for source, raw in (("mcp", mcp_root),
                        ("env", os.environ.get("CLAUDE_PROJECT_DIR", "")),
                        ("requested", requested)):
        found = _existing_dir(raw)
        if found is not None:
            return RootChoice(found, source, f"root from {source}")
    return RootChoice(None, "none", "no project root is known")
