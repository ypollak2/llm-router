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
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_TIMEOUT = 1.5


def _git(args: list[str], root: str | None) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=root or None, capture_output=True,
                           text=True, timeout=_TIMEOUT)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:                                        # noqa: BLE001
        return ""


def collect(root: str | None = None) -> dict[str, str]:
    """Facts about the working tree right now. Empty dict outside a repo."""
    if root and not (Path(root) / ".git").exists():
        return {}
    branch = _git(["branch", "--show-current"], root)
    if not branch and not _git(["rev-parse", "--git-dir"], root):
        return {}

    facts: dict[str, str] = {}
    if branch:
        facts["branch"] = branch
    head = _git(["rev-parse", "--short", "HEAD"], root)
    if head:
        facts["head"] = head
    subject = _git(["log", "-1", "--format=%s"], root)
    if subject:
        facts["last_commit"] = subject[:100]

    # `-uall`, not the default: plain porcelain collapses an untracked
    # directory to "src/", and "which file did I just touch" is the question
    # a continuation actually asks.
    dirty = _git(["status", "--porcelain", "--untracked-files=all"], root)
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


def render(root: str | None = None) -> str:
    """One compact block, or "" when there is nothing to say.

    Deliberately labelled as observed state so a model cannot mistake it for an
    instruction, and so a reader of a draft can tell which parts were grounded.
    """
    facts = collect(root)
    if not facts:
        return ""
    order = ("branch", "head", "last_commit", "uncommitted", "changed")
    body = "\n".join(f"  {k}: {facts[k]}" for k in order if k in facts)
    return f"<repo_state>  (observed just now, not model output)\n{body}\n</repo_state>"
