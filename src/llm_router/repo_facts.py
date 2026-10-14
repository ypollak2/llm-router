"""Current repository state, as facts rather than as prose.

N11. The three context sources all carry what was SAID or what is WRITTEN: OKF
holds indexed documents, session_store holds the conversation, history relay holds
the last turns. None answers "what is true right now" — which branch, which
commit, did the last command succeed.

That is what a continuation points at. "merge once CI is green", "check if windows
was failing on main", "what do we have left to do" all need the state of the
world, and a model given only the conversation invents it. Measured 2026-09-14: 10
of 144 drafts claimed an action that never happened, and the session-replay smoke
test produced "what do we have left to do?" answered with a fabricated status.

WHAT THIS MUST NEVER BECOME
---------------------------
A fabrication amplifier. A draft's invented "63.2% complete (5,309/8,400)" was
once written into session memory and came back as the next turn's ground truth,
escalating to "78.5% complete (6,600/8,400)" for a project that does not exist.

Two rules keep this different in kind from that:

1. Every field is read from a COMMAND, never from model output. `git` is the only
   author. A routed model cannot write here, and nothing derived from one may.
2. Every field is OVERWRITTEN from a fresh read, never appended. There is no
   history to compound — a wrong value is corrected by the next call, not carried.

It is also small on purpose: a few dozen tokens against a payload measured at ~457.

COST AND OPT-IN REUSE (PLAN v16 P1.1)
-------------------------------------
A read used five git subprocesses (~30 ms on a laptop), most of the context
pack's 50 ms budget. `collect` now uses two (`status --porcelain --branch` and
one `log` line) and is never cached. It needs git >= 2.17 for
`--no-ahead-behind`; on an older git the status call fails and `collect`
returns {} (no repo state), where the five-call version still answered.

`render(root, reuse=True)` may return the previous block for the same root
while `.git/HEAD`, `.git/index` and `.git/logs/HEAD` are unchanged (stat mtime
and size) and for at most `_REUSE_TTL_S` seconds. Only the context pack opts in.
The default (`reuse=False`) always reads git, so existing callers see every
edit at once. Under reuse a branch switch, commit, reset or staging change is
seen on the next call; an unstaged edit or new untracked file can lag by up to
the TTL. Rule 2 still holds: the block is replaced, never appended to.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

_TIMEOUT = 1.5
_REUSE_TTL_S = 10.0
_REUSE_MAX = 64
_reuse: dict[str, tuple[tuple, float, str]] = {}


def _git(args: list[str], root: str | None) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=root or None, capture_output=True,
                           text=True, timeout=_TIMEOUT)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:                                        # noqa: BLE001
        return ""


def _branch(header: str) -> str:
    """Branch name from a ``git status --branch`` header line ("" if detached)."""
    rest = header[3:].strip()
    for prefix in ("No commits yet on ", "Initial commit on "):
        if rest.startswith(prefix):
            return rest[len(prefix):].strip()
    if rest.startswith("HEAD (no branch)"):
        return ""
    return rest.split("...", 1)[0].split(" ", 1)[0]


def collect(root: str | None = None) -> dict[str, str]:
    """Facts about the working tree right now. Empty dict outside a repo.

    Two git calls (status with its branch header, and one log line), not five:
    each subprocess costs ~6 ms and this runs on the context pack's 50 ms path.
    """
    if root and not (Path(root) / ".git").exists():
        return {}
    # `-uall`, not the default: plain porcelain collapses an untracked
    # directory to "src/", and "which file did I just touch" is the question
    # a continuation actually asks.
    status = _git(["status", "--porcelain", "--branch", "--no-ahead-behind",
                   "--untracked-files=all"], root)
    lines = status.splitlines()
    if not lines or not lines[0].startswith("## "):
        return {}  # not a repository, or git failed: say nothing rather than guess

    facts: dict[str, str] = {}
    branch = _branch(lines[0])
    if branch:
        facts["branch"] = branch
    log = _git(["log", "-1", "--format=%h%x00%s"], root)
    if log and "\x00" in log:
        head, subject = log.split("\x00", 1)
        if head:
            facts["head"] = head
        if subject:
            facts["last_commit"] = subject[:100]

    dirty = "\n".join(lines[1:])
    if dirty:
        lines = dirty.splitlines()
        facts["uncommitted"] = str(len(lines))
        # The first few names, because "which file did I just change" is the
        # question a continuation most often asks.
        # `git status --porcelain` is "XY<space>path", but a renamed entry is
        # "R  old -> new" and a quoted path carries quotes. Splitting on the
        # first run of whitespace after the two status columns is what survives
        # both; a fixed slice produced "rc/llm_router/..." for "src/...".
        names = []
        for ln in lines[:5]:
            rest = ln[2:].strip()
            if " -> " in rest:
                rest = rest.split(" -> ", 1)[1]
            rest = rest.strip('"')
            if rest:
                names.append(rest)
        if names:
            facts["changed"] = ", ".join(names)
    else:
        facts["uncommitted"] = "0"
    return facts


def _git_dir(root: Path) -> Path | None:
    dot = root / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():  # a worktree: ".git" is "gitdir: <path>"
        text = dot.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            gd = Path(text[len("gitdir:"):].strip())
            return gd if gd.is_absolute() else root / gd
    return None


def _state_key(root: str) -> tuple | None:
    """Stat of the files any branch switch, commit, reset or staging rewrites."""
    gd = _git_dir(Path(root))
    if gd is None:
        return None
    key = []
    for name in ("HEAD", "index", "logs/HEAD"):
        try:
            st = (gd / name).stat()
            key.append((st.st_mtime_ns, st.st_size))
        except OSError:
            key.append(None)
    return tuple(key)


def render(root: str | None = None, *, reuse: bool = False) -> str:
    """One compact block, or "" when there is nothing to say.

    Deliberately labelled as observed state so a model cannot mistake it for an
    instruction, and so a reader of a draft can tell which parts were grounded.
    With ``reuse=True`` it may return the previous block for ``root`` while git
    state is unchanged and the block is younger than ``_REUSE_TTL_S`` (module
    docstring). ``reuse=False`` (default) and ``root=None`` always read git.
    """
    real = key = None
    if root and reuse:
        try:
            real = str(Path(root).resolve())
            key = _state_key(real)
        except OSError:
            key = None
        if key is not None:
            hit = _reuse.get(real)
            if hit and hit[0] == key and time.monotonic() - hit[1] < _REUSE_TTL_S:
                return hit[2]
    block = _render_fresh(root)
    if key is not None and real is not None:
        try:
            # Keyed AFTER the read: `git status` may refresh the index itself.
            # Known ms-scale TOCTOU: a commit or staging change landing between
            # the read and this stat is stored under the new key, so the old
            # block can be reused for up to the TTL. Accepted for the pack only.
            after = _state_key(real)
        except OSError:
            after = None
        if after is not None:
            if len(_reuse) >= _REUSE_MAX:
                _reuse.clear()
            _reuse[real] = (after, time.monotonic(), block)
    return block


def _render_fresh(root: str | None) -> str:
    facts = collect(root)
    if not facts:
        return ""
    order = ("branch", "head", "last_commit", "uncommitted", "changed")
    body = "\n".join(f"  {k}: {facts[k]}" for k in order if k in facts)
    return f"<repo_state>  (observed just now, not model output)\n{body}\n</repo_state>"
