"""Build the input of a commit-message or PR-description text job from the real repository.

The text job is only as good as what it is shown, and the checks that judge its answer
(``llm_router.text_jobs``) look for a file name that really is in the change. So the input is
the actual diff, in the shape those checks read (``diff --git`` headers), condensed so that
every changed file is still named even when the diff is far larger than the model can read.

Every git call is read-only, bounded in time and in bytes, and never takes an optional lock. None
of them may run a command that the repository's own configuration names: the hook reads the repository
BEFORE the permission check for the commit, so a hostile ``.git/config`` must not get a turn.
``_NEUTRAL_CONFIG`` switches off the file-system monitor and the pager, every diff reads with
``--no-ext-diff --no-textconv --ignore-submodules=dirty``, every log read with ``--no-show-signature`` (and the gpg
programs pinned: a signed commit runs ``gpg.program`` when ``log.showSignature`` is on), and ``_filter_overrides``
replaces every configured clean/smudge/process filter (they run when a diff reads the working tree).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass, field

#: ``llm_router.text_jobs.MAX_INPUT_CHARS`` is 40,000. Stay under it so the job never truncates
#: the input again (a second, blind cut would drop whole files from the checked grounding).
INPUT_BUDGET_CHARS = 36_000
BLOCK_MAX_LINES = 120
LINE_MAX_CHARS = 300
LIST_MAX_FILES = 60
_GIT_BYTES_CAP = 2_000_000
_GIT_TIMEOUT_S = 20.0

_ENV_EXTRA = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_PAGER": "cat"}

#: Configuration that makes git run a program named in the repository's config, each set to a harmless
#: value for every call the hook makes. ``core.fsmonitor`` runs a hook script when git refreshes the index;
#: the pager is only started on a terminal, but costs nothing to pin; ``diff.submodule=log`` would run git
#: inside a submodule. Diff drivers and textconv are switched off by the flags every diff call passes.
_NEUTRAL_CONFIG = (
    "core.quotepath=false", "color.ui=never", "diff.noprefix=false", "diff.mnemonicprefix=false",
    "core.fsmonitor=false", "core.pager=cat", "pager.diff=false", "pager.log=false", "diff.submodule=short",
    "core.untrackedCache=false",
    # a signed commit in the log runs the configured gpg program when log.showSignature is on
    "log.showSignature=false", "gpg.program=/usr/bin/false", "gpg.openpgp.program=/usr/bin/false",
    "gpg.x509.program=/usr/bin/false", "gpg.ssh.program=/usr/bin/false",
)
_LOG_FLAGS = ("--no-show-signature",)
_DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "--ignore-submodules=dirty")
_FILTER_KEY = re.compile(r"^filter\.(.+)\.(clean|smudge|process|required)$")


class InputRefused(Exception):
    """There is nothing sound to build the job from. ``message`` tells the model what to do."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class JobInput:
    text: str
    hint: str | None
    files: int
    omitted_files: int = 0
    #: every changed file with its added/deleted counts (``None`` for a binary file); the source of the
    #: deterministic ``files`` body of a commit message
    stats: list = field(default_factory=list)


RC_TIMEOUT = 124


def _filter_overrides(repo: str) -> list[str]:
    """``-c`` pairs that replace every filter driver configured for the repository with a harmless one.

    A clean filter runs when a diff compares the working tree (``git diff HEAD``); the program is named by
    ``filter.<name>.clean`` in the repository's own config. ``cat`` keeps the content, ``process`` is emptied
    and ``required`` is cleared so a missing filter cannot abort the read."""
    rc, out, _cut = _run_git(repo, ["config", "--name-only", "--get-regexp", r"^filter\."], _GIT_BYTES_CAP, 5.0, [])
    if rc != 0:
        return []
    names = {m.group(1) for line in out.splitlines() if (m := _FILTER_KEY.match(line.strip()))}
    pairs: list[str] = []
    for n in sorted(names):
        pairs += ["-c", f"filter.{n}.clean=cat", "-c", f"filter.{n}.smudge=cat", "-c", f"filter.{n}.process=",
                  "-c", f"filter.{n}.required=false"]
    return pairs


def _git(repo: str, *args: str, cap: int | None = None, timeout: float | None = None,
         report_cut: bool = False) -> tuple:
    """Run ``git -C repo args``; ``(returncode, stdout)``, or ``(returncode, stdout, cut)`` with ``report_cut``.
    Output past ``cap`` bytes is cut (and counts as success: we asked git to stop); a git that outlives
    ``timeout`` is killed and returns ``RC_TIMEOUT``. Repository configuration that would run a program is
    switched off for the call (see the module docstring)."""
    reads_tree = bool(args) and args[0] == "diff"
    extra = _filter_overrides(repo) if reads_tree else []
    rc, text, cut = _run_git(repo, list(args), _GIT_BYTES_CAP if cap is None else cap,
                             _GIT_TIMEOUT_S if timeout is None else timeout, extra)
    return (rc, text, cut) if report_cut else (rc, text)


def _run_git(repo: str, args: list[str], cap: int, timeout: float, extra_config: list[str]) -> tuple[int, str, bool]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_ENV_EXTRA)
    config: list[str] = []
    for kv in _NEUTRAL_CONFIG:
        config += ["-c", kv]
    cmd = ["git", "--no-pager", "-C", repo, *config, *extra_config, *args]
    try:
        # Its own session: on a timeout the whole process group dies, so a child that inherited the
        # pipe cannot keep a read blocked after git itself was killed.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                                start_new_session=True)
    except OSError:
        return 127, "", False
    timed_out = threading.Event()

    def _kill() -> None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _expire() -> None:
        timed_out.set()
        _kill()

    timer = threading.Timer(timeout, _expire)
    timer.start()
    try:
        data = proc.stdout.read(cap + 1) if proc.stdout else b""
        cut = len(data) > cap
        if cut:
            _kill()
            data = data[:cap]
        proc.wait()
        rc = 0 if cut else proc.returncode
    finally:
        timer.cancel()
        if proc.stdout:
            proc.stdout.close()
    if timed_out.is_set():
        return RC_TIMEOUT, "", False
    return rc, data.decode("utf-8", errors="replace"), cut


def _require_repo(repo: str) -> None:
    rc, _ = _git(repo, "rev-parse", "--git-dir")
    if rc == RC_TIMEOUT:
        raise InputRefused("git_timeout", "git did not answer in time.")
    if rc != 0:
        raise InputRefused("not_a_repo", "The command does not run inside a git repository, so there is nothing to describe.")


def _timed_out(rc: int) -> None:
    if rc == RC_TIMEOUT:
        raise InputRefused("git_timeout", "git did not answer in time, so the diff could not be read.")


def _blocks(patch: str) -> list[str]:
    return [b for b in re.split(r"(?m)^(?=diff --git )", patch) if b.strip()]


def _block_path(block: str) -> str:
    m = re.match(r"diff --git a/(\S+) b/(\S+)", block)
    if m:
        return m.group(2)
    m = re.search(r"(?m)^\+\+\+ b/(\S+)", block)
    return m.group(1) if m else "(unnamed)"


def _counts(block: str) -> tuple[int, int]:
    add = dele = 0
    in_hunk = False
    for line in block.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            if line.startswith("+"):
                add += 1
            elif line.startswith("-"):
                dele += 1
    return add, dele


def _cap_line(line: str) -> str:
    """One diff line cut to ``LINE_MAX_CHARS``: a minified bundle is one line of megabytes."""
    return line if len(line) <= LINE_MAX_CHARS else line[:LINE_MAX_CHARS] + " ...[line cut]"


def _numstat(repo: str, *diff_args: str) -> tuple[list[tuple[str, int | None, int | None]], bool]:
    """Every changed file with its added/deleted line counts (``None`` for a binary file), and whether the list is
    complete. A second, small read: the patch itself is cut at ``_GIT_BYTES_CAP`` bytes, which must never make
    files disappear from the count."""
    rc, out, cut = _git(repo, "diff", "--numstat", "-z", "--ignore-submodules=dirty", *diff_args, report_cut=True)
    _timed_out(rc)
    if rc != 0:
        return [], False
    rows: list[tuple[str, int | None, int | None]] = []
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        parts = fields[i].split("\t", 2)
        if len(parts) != 3:
            i += 1
            continue
        a, d, path = parts
        if not path:  # a rename or copy: the old and the new path are the next two fields
            if i + 2 >= len(fields):
                break
            path, i = fields[i + 2], i + 3
        else:
            i += 1
        rows.append((path, int(a) if a.isdigit() else None, int(d) if d.isdigit() else None))
    if cut and rows:
        rows.pop()  # the last record may be half a path
    return rows, not cut


def _stats_of(patch: str) -> list[tuple[str, int | None, int | None]]:
    return [(_block_path(b), *_counts(b)) for b in _blocks(patch)]


def files_body(stats: list, limit: int | None = None) -> str:
    """The body of a commit message that holds nothing a model wrote: the changed files and their line counts,
    straight from git. The first ``limit`` files are listed, the rest counted."""
    from llm_router.text_job_bind import limits

    cap = limits.FILES_BODY_MAX if limit is None else limit
    lines = ["Changed files:"] + [f"- {_stat_line(p, a, d)}" for p, a, d in stats[:cap]]
    if len(stats) > cap:
        lines.append(f"- ... and {len(stats) - cap} more files")
    return "\n".join(lines)


_ESCAPES = {"\n": "\\n", "\t": "\\t", "\r": "\\r", '"': '\\"', "\\": "\\\\"}


def quote_name(path: str) -> str:
    """A file name as it may appear in a line of text: unchanged when it holds nothing that could change what the
    line means, else in double quotes with C escapes (the way git itself prints such a name). A newline in a name
    would end the line and start another that looks like a trailer (``Signed-off-by: ...``); an ESC, a bidi control or a
    U+2028 would do the same to a terminal or a reader."""
    if not any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or c in '"\\\u2028\u2029\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069'
               for c in path):
        return path
    out = []
    for c in path:
        if c in _ESCAPES:
            out.append(_ESCAPES[c])
        elif ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or c in "\u2028\u2029\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069":
            out.append("".join(f"\\{b:03o}" for b in c.encode("utf-8")))
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _stat_line(path: str, a: int | None, d: int | None) -> str:
    name = quote_name(path)
    return f"{name} (binary)" if a is None or d is None else f"{name} (+{a}/-{d})"


def condense(patch: str, *, budget: int = INPUT_BUDGET_CHARS, block_lines: int = BLOCK_MAX_LINES,
             label: str = "Changed files", stats: list[tuple[str, int | None, int | None]] | None = None,
             stats_complete: bool = True, patch_cut: bool = False) -> tuple[str, int, int]:
    """A unified diff cut to fit ``budget`` characters; the files it could not show are counted, never dropped.

    Returns ``(text, files, files_without_diff)``. ``stats`` is the true list of changed files (``_numstat``);
    without it the list is read from the patch. The list comes first (the first ``LIST_MAX_FILES`` names and a
    counted tail; it is not a ``diff --git`` line, so it never counts as grounding by itself). Each file's patch
    is cut to ``block_lines`` lines after its header; a file whose patch does not fit keeps its ``diff --git``
    header and a note while the budget allows, and every file left over is counted in a final line. A patch that
    git was stopped from printing in full (``patch_cut``) says so.
    """
    blocks = _blocks(patch)
    in_patch = {_block_path(b) for b in blocks}
    all_stats = list(stats) if stats else [(_block_path(b), *_counts(b)) for b in blocks]
    total = len(all_stats)
    shown = all_stats[:LIST_MAX_FILES]
    head = [f"## {label} ({total}{'' if stats_complete else '+'})"]
    head += [_stat_line(p, a, d) for p, a, d in shown]
    if total > len(shown):
        head.append(f"... and {total - len(shown)}{'' if stats_complete else '+'} more")
    if patch_cut:
        head.append(f"Note: git stopped printing the diff after {_GIT_BYTES_CAP} bytes; files after that point are "
                    "listed by name only.")
    out = ["\n".join(head), ""]
    used = len(out[0]) + 1
    omitted = 0
    for b in blocks:
        lines = [_cap_line(ln) for ln in b.rstrip("\n").splitlines()]
        cut = next((i for i, ln in enumerate(lines) if ln.startswith("@@")), len(lines))
        header, body = lines[:cut], lines[cut:]
        keep = body[:block_lines]
        text = "\n".join(header + keep)
        if len(body) > len(keep):
            text += f"\n... [{len(body) - len(keep)} more lines of this file omitted]"
        if used + len(text) + 1 > budget:
            text = "\n".join(header[:1]) + "\n... [diff omitted: size budget]"
            if used + len(text) + 1 > budget:
                continue  # not even its header fits: counted below with the other files that have no text
            omitted += 1
        out.append(text)
        used += len(text) + 1
    # files git never printed (the byte cap) or whose header no longer fit: counted, not silently gone
    for p in (p for p, _a, _d in all_stats if p not in in_patch):
        text = f"diff --git a/{p} b/{p}\n... [diff omitted: not printed]"
        if used + len(text) + 1 > budget:
            break
        out.append(text)
        used += len(text) + 1
        omitted += 1
    named = sum(1 for t in out[2:] if t.startswith("diff --git"))
    without_header = max(0, total - named)
    if without_header:
        out.append(f"... [{without_header} more changed files have no diff text here; they are in the list above "
                   "or counted in its tail]")
    return "\n".join(out).rstrip("\n") + "\n", total, omitted + without_header


_PR_REF_RE = re.compile(r"\s*\(#\d+\)")
_PLAIN_STEM_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{2,}")
NAME_EXAMPLES = 3


def _subject_for_hint(subject: str) -> str:
    """A recent subject without its pull-request number. A model told to match the style copies the
    ``(#123)`` suffix and makes up a number, so the numbers are not shown."""
    return _PR_REF_RE.sub("", subject).strip()[:120]


def _name_examples(patch: str) -> list[str]:
    """Up to ``NAME_EXAMPLES`` changed files' names without the extension, the largest change first.

    The router's check of a commit message wants it to name a changed file (``text_jobs.check_commit_message``:
    the file's name without the extension, as a whole word). Without being told, the model fails that at its
    first attempt more often than not and the retry loop has to ask again; the names come from the same
    ``diff --git`` headers the check reads, so a message that uses one of them always passes it.
    """
    ranked = sorted(((sum(_counts(b)), _block_path(b)) for b in _blocks(patch)), key=lambda t: -t[0])
    out: list[str] = []
    for _, path in ranked:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if _PLAIN_STEM_RE.fullmatch(stem) and stem.lower() not in {x.lower() for x in out}:
            out.append(stem)
        if len(out) == NAME_EXAMPLES:
            break
    return out


def build_commit_input(repo: str, *, all_tracked: bool = False) -> JobInput:
    """The input for a commit message: the staged diff (``-a``: tracked changes against HEAD)."""
    _require_repo(repo)
    if all_tracked:
        rc, _ = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
        if rc != 0:
            raise InputRefused("no_head", "`git commit -a` has no HEAD to compare with; stage with git add first.")
        args = ["diff", "HEAD"]
        empty = "No tracked file has changes, so there is nothing to describe."
    else:
        args = ["diff", "--cached"]
        empty = ("Nothing is staged, so there is nothing to describe: run `git add <paths>` as its own "
                 "command first, then the commit.")
    rc, patch, patch_cut = _git(repo, *args, *_DIFF_FLAGS, "-U3", report_cut=True)
    _timed_out(rc)
    if rc != 0 or not patch.strip():
        raise InputRefused("nothing_staged", empty)
    stats, complete = _numstat(repo, *args[1:])
    text, files, omitted = condense(patch, label="Changed files", stats=stats or None, stats_complete=complete,
                                    patch_cut=patch_cut)
    rc, log = _git(repo, "log", "-5", *_LOG_FLAGS, "--format=%s")
    subjects = [t for t in (_subject_for_hint(s) for s in log.splitlines() if s.strip()) if t] if rc == 0 else []
    parts: list[str] = []
    if subjects:
        parts.append(
            "Recent commit subjects in this repository (match their style and tense; leave out pull-request "
            "numbers such as (#123) and never make one up):\n" + "\n".join(f"- {s}" for s in subjects)
        )
    names = _name_examples(patch)
    if names:
        parts.append("Name at least one changed file in the message, by its file name without the extension, "
                     "for example: " + ", ".join(names) + ".")
    return JobInput(text, "\n\n".join(parts) or None, files, omitted, stats or _stats_of(patch))


def _resolve_ref(repo: str, candidates: list[str]) -> str | None:
    for ref in candidates:
        rc, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if rc == 0:
            return ref
    return None


def build_pr_input(repo: str, *, base: str | None = None, head: str | None = None,
                   title: str | None = None) -> JobInput:
    """The input for a PR description: the branch's commits and its diff against the base."""
    _require_repo(repo)
    if base:
        base_candidates = [f"origin/{base}", base]
    else:
        rc, out = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        default = out.strip() if rc == 0 and out.strip() else ""
        base_candidates = [c for c in (default, "origin/main", "origin/master", "main", "master") if c]
    base_ref = _resolve_ref(repo, base_candidates)
    if base_ref is None:
        raise InputRefused("no_base", "Could not find the base branch to compare with; pass --base with a branch that exists.")
    head_candidates = [head, f"origin/{head}"] if head else ["HEAD"]
    head_ref = _resolve_ref(repo, [h for h in head_candidates if h])
    if head_ref is None:
        raise InputRefused("no_head", "Could not find the head branch; push it or pass --head with a branch that exists.")
    rc, count = _git(repo, "rev-list", "--count", f"{base_ref}..{head_ref}")
    if rc != 0 or not count.strip().isdigit() or int(count.strip()) == 0:
        raise InputRefused("no_commits", f"The branch has no commits ahead of {base_ref}, so there is nothing to describe.")
    n = int(count.strip())
    rc, log = _git(repo, "log", "-40", *_LOG_FLAGS, "--format=%h %s", f"{base_ref}..{head_ref}")
    lines = [ln[:160] for ln in log.splitlines() if ln.strip()]
    commits = "\n".join(lines) + (f"\n... and {n - len(lines)} more" if n > len(lines) else "")
    span = f"{base_ref}...{head_ref}"
    rc, patch, patch_cut = _git(repo, "diff", *_DIFF_FLAGS, "-U3", span, report_cut=True)
    _timed_out(rc)
    if rc != 0 or not patch.strip():
        raise InputRefused("no_diff", f"The branch has no file changes against {base_ref}, so there is nothing to describe.")
    stats, complete = _numstat(repo, span)
    diff_text, files, omitted = condense(patch, budget=INPUT_BUDGET_CHARS - len(commits) - 200, label="Changed files",
                                         stats=stats or None, stats_complete=complete, patch_cut=patch_cut)
    text = f"## Branch\n{head_ref} -> {base_ref}\n\n## Commits ({n})\n{commits}\n\n{diff_text}"
    hint = f"PR title chosen by the author: {title[:200]}" if title else None
    return JobInput(text, hint, files, omitted, stats or _stats_of(patch))
