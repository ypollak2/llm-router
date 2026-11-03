"""Build the input of a commit-message or PR-description text job from the real repository.

The text job is only as good as what it is shown, and the checks that judge its answer
(``llm_router.text_jobs``) look for a file name that really is in the change. So the input is
the actual diff, in the shape those checks read (``diff --git`` headers), condensed so that
every changed file is still named even when the diff is far larger than the model can read.

Every git call is read-only, bounded in time and in bytes, runs with ``--no-ext-diff`` and
``--no-textconv`` (a repository's own diff or textconv driver is never executed from here) and
never takes an optional lock.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass

#: ``llm_router.text_jobs.MAX_INPUT_CHARS`` is 40,000. Stay under it so the job never truncates
#: the input again (a second, blind cut would drop whole files from the checked grounding).
INPUT_BUDGET_CHARS = 36_000
BLOCK_MAX_LINES = 120
LINE_MAX_CHARS = 300
LIST_MAX_FILES = 60
_GIT_BYTES_CAP = 2_000_000
_GIT_TIMEOUT_S = 20.0

_ENV_EXTRA = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_PAGER": "cat"}


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


RC_TIMEOUT = 124


def _git(repo: str, *args: str, cap: int = _GIT_BYTES_CAP, timeout: float = _GIT_TIMEOUT_S) -> tuple[int, str]:
    """Run ``git -C repo args``; ``(returncode, stdout)``. Output past ``cap`` bytes is cut (and counts
    as success: we asked git to stop); a git that outlives ``timeout`` is killed and returns ``RC_TIMEOUT``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_ENV_EXTRA)
    cmd = ["git", "--no-pager", "-C", repo, "-c", "core.quotepath=false", "-c", "color.ui=never",
           "-c", "diff.noprefix=false", "-c", "diff.mnemonicprefix=false", *args]
    try:
        # Its own session: on a timeout the whole process group dies, so a child that inherited the
        # pipe cannot keep a read blocked after git itself was killed.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                                start_new_session=True)
    except OSError:
        return 127, ""
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
        return RC_TIMEOUT, ""
    return rc, data.decode("utf-8", errors="replace")


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


def condense(patch: str, *, budget: int = INPUT_BUDGET_CHARS, block_lines: int = BLOCK_MAX_LINES,
             label: str = "Changed files") -> tuple[str, int, int]:
    """A unified diff cut to fit ``budget`` characters while still naming every changed file.

    Returns ``(text, files, files_without_diff)``. A file's patch is cut to ``block_lines``
    lines after its header; when the budget runs out the remaining files keep their ``diff --git``
    header and a note. A list of the changed files with their line counts comes first (it is not
    a ``diff --git`` line, so it never counts as grounding by itself).
    """
    blocks = _blocks(patch)
    stats = [(_block_path(b), *_counts(b)) for b in blocks]
    shown = stats[:LIST_MAX_FILES]
    head = [f"## {label} ({len(blocks)})"]
    head += [f"{p} (+{a}/-{d})" for p, a, d in shown]
    if len(stats) > len(shown):
        head.append(f"... and {len(stats) - len(shown)} more")
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
            omitted += 1
            if used + len(text) + 1 > budget:
                continue
        out.append(text)
        used += len(text) + 1
    return "\n".join(out).rstrip("\n") + "\n", len(blocks), omitted


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
    rc, patch = _git(repo, *args, "--no-color", "--no-ext-diff", "--no-textconv", "-U3")
    _timed_out(rc)
    if rc != 0 or not patch.strip():
        raise InputRefused("nothing_staged", empty)
    text, files, omitted = condense(patch, label="Changed files")
    rc, log = _git(repo, "log", "-5", "--format=%s")
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
    return JobInput(text, "\n\n".join(parts) or None, files, omitted)


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
    rc, log = _git(repo, "log", "-40", "--format=%h %s", f"{base_ref}..{head_ref}")
    lines = [ln[:160] for ln in log.splitlines() if ln.strip()]
    commits = "\n".join(lines) + (f"\n... and {n - len(lines)} more" if n > len(lines) else "")
    rc, patch = _git(repo, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "-U3", f"{base_ref}...{head_ref}")
    _timed_out(rc)
    if rc != 0 or not patch.strip():
        raise InputRefused("no_diff", f"The branch has no file changes against {base_ref}, so there is nothing to describe.")
    diff_text, files, omitted = condense(patch, budget=INPUT_BUDGET_CHARS - len(commits) - 200, label="Changed files")
    text = f"## Branch\n{head_ref} -> {base_ref}\n\n## Commits ({n})\n{commits}\n\n{diff_text}"
    hint = f"PR title chosen by the author: {title[:200]}" if title else None
    return JobInput(text, hint, files, omitted)
