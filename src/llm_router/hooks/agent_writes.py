"""The gate between a local model's proposed edit and your working tree.

`_resolve_path` already contains every path to the project root, so the loop
cannot escape the repo. What it could do, until this module existed, is rewrite
any file inside it with no record and nothing to compare against: `write_file`
called `path.write_text(...)` directly.

Three modes, via `LLM_ROUTER_AGENT_WRITES`:

    propose   (default) compute the diff, change nothing, hand the patch back
    apply               write, but journal the pre-image first
    off                 refuse writes; the loop is read-only

`propose` is the default because it is the mode where a wrong edit costs
nothing: the loop still does the work and still produces a patch, and a human
decides whether it lands. `apply` is a deliberate choice, not a fallback.

The journal is not a nicety. An edit applied by a model at 3am is only safe if
it can be undone at 4am by someone who does not know what it was.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import json
import os
from pathlib import Path

# ── Commands ────────────────────────────────────────────────────────────────
#
# The write gate above says nothing about run_command, and a loop that cannot
# edit a file but can run any program is not gated. `_BLOCKED_COMMANDS` in
# agent_loop stops the catastrophic shapes (rm -rf /, mkfs, dd) and the executor
# avoids the shell, but neither constrains what a program DOES — `git push`,
# `pip install`, `curl | sh` as separate argv all pass.
#
# Default is an allowlist of inspection commands, chosen by one rule: it reads
# state, it does not change it, and it does not reach the network. Test runners
# are included deliberately — they execute the repo's own code, which is the
# point of running them, and refusing them makes the loop useless for the task
# it is best at.
CMD_ALLOWLIST = "allowlist"
CMD_ALL = "all"
CMD_OFF = "off"
_VALID_CMD = (CMD_ALLOWLIST, CMD_ALL, CMD_OFF)

_ALLOWED_PROGRAMS = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "du", "find",
    "grep", "rg", "ag", "sed", "awk", "sort", "uniq", "cut", "diff", "tree",
    "git", "python", "python3", "pytest", "node", "go", "cargo", "which", "echo",
})

# Subcommands of an allowed program that are NOT read-only. `git` is on the list
# for status/log/diff; it is also how you push, reset and delete a branch.
_BLOCKED_SUBCOMMANDS = {
    "git": frozenset({"push", "reset", "clean", "checkout", "switch", "restore",
                      "rebase", "merge", "cherry-pick", "revert", "gc", "prune",
                      "remote", "config", "tag", "branch", "stash", "am", "apply"}),
    "cargo": frozenset({"publish", "install", "yank"}),
    "go": frozenset({"install", "get"}),
    "npm": frozenset({"install", "publish", "run"}),
}

# pip/uv install reaches the network and mutates the environment, and `python -m`
# is the usual way in.
_BLOCKED_MODULES = frozenset({"pip", "ensurepip", "venv", "http.server"})


def command_mode() -> str:
    raw = os.environ.get("LLM_ROUTER_AGENT_COMMANDS", "").strip().lower()
    return raw if raw in _VALID_CMD else CMD_ALLOWLIST


def guard_command(argv: list[str]) -> tuple[bool, str]:
    """Decide whether this command runs. Returns ``(run_it, refusal_message)``.

    The message is empty when the command is allowed.
    """
    current = command_mode()
    if current == CMD_OFF:
        return False, ("REFUSED: running commands is disabled "
                       "(LLM_ROUTER_AGENT_COMMANDS=off). Nothing was executed.")
    if current == CMD_ALL:
        return True, ""
    if not argv:
        return False, "REFUSED: empty command."

    program = Path(argv[0]).name
    if program not in _ALLOWED_PROGRAMS:
        return False, (
            f"REFUSED: '{program}' is not in the inspection allowlist, so it was "
            f"NOT executed. Allowed: {', '.join(sorted(_ALLOWED_PROGRAMS))}. "
            f"Set LLM_ROUTER_AGENT_COMMANDS=all to lift this. Do not retry — "
            f"use the file tools, or say what you needed to run and why."
        )

    # Scan EVERY argument, not just the first non-flag one: `git -C /tmp push`
    # puts the flag's VALUE where the subcommand was expected, and `push` lands
    # at position two. Being strict costs a false refusal on a branch literally
    # named `push`; being positional costs an unreviewed force-push.
    blocked = _BLOCKED_SUBCOMMANDS.get(program)
    if blocked:
        hit = next((a for a in argv[1:] if a in blocked), None)
        if hit:
            return False, (
                f"REFUSED: '{program} {hit}' changes state rather than reading it, "
                f"so it was NOT executed. Do not retry."
            )
    if program.startswith("python") and "-m" in argv:
        try:
            module = argv[argv.index("-m") + 1]
        except IndexError:
            module = ""
        # Both the full dotted name and its root package: the blocklist holds
        # `http.server` (dotted) and `pip` (a root whose submodules are equally
        # unwanted), so neither form alone catches both.
        if module in _BLOCKED_MODULES or module.split(".")[0] in _BLOCKED_MODULES:
            return False, (
                f"REFUSED: 'python -m {module}' mutates the environment or opens "
                f"the network, so it was NOT executed. Do not retry."
            )
    return True, ""


MODE_PROPOSE = "propose"
MODE_APPLY = "apply"
MODE_OFF = "off"

_VALID = (MODE_PROPOSE, MODE_APPLY, MODE_OFF)

# A diff is fed back to the model as a tool result and then carried in its final
# answer, so an unbounded one can blow the context window on a single edit.
_MAX_DIFF_LINES = 200


def mode() -> str:
    """Resolved write mode. Anything unrecognised means `propose`.

    Fails SAFE rather than loud: a typo in the env var must not silently grant
    write access, and raising here would break a loop mid-run over a setting.
    """
    raw = os.environ.get("LLM_ROUTER_AGENT_WRITES", "").strip().lower()
    return raw if raw in _VALID else MODE_PROPOSE


def journal_root() -> Path:
    """Resolved per call — a module-level constant freezes $HOME at import time,
    which has been the cause of four separate path defects in this tree."""
    base = os.environ.get("LLM_ROUTER_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".llm-router"
    return root / "agent_edits"


def unified_diff(before: str, after: str, rel_path: str) -> str:
    """A truncated unified diff, or a plain note when the content is unchanged."""
    lines = list(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        n=3,
    ))
    if not lines:
        return "(no change — the new content is identical to the old)"
    if len(lines) > _MAX_DIFF_LINES:
        kept = lines[:_MAX_DIFF_LINES]
        kept.append(f"... ({len(lines) - _MAX_DIFF_LINES} more diff lines truncated)\n")
        lines = kept
    return "".join(lines)


def journal(path: Path, before: str | None, after: str, project_root: Path) -> Path | None:
    """Record the pre-image so an applied edit can be reverted. Never raises.

    Returns the pre-image path, or None when the journal could not be written —
    in which case `guard` refuses the edit rather than applying one it cannot
    undo.
    """
    rel = _rel(path, project_root)
    try:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        slot = journal_root() / stamp
        slot.mkdir(parents=True, exist_ok=True)
        pre = slot / "before"
        pre.parent.mkdir(parents=True, exist_ok=True)
        pre.write_text(before if before is not None else "", encoding="utf-8")
        (slot / "manifest.json").write_text(json.dumps({
            "target": str(path),
            "relative": str(rel),
            "created": before is None,      # a new file: reverting means deleting it
            "bytes_before": len(before or ""),
            "bytes_after": len(after),
            "restore": f"cp {slot / 'before'} {path}",
        }, indent=2), encoding="utf-8")
        return pre
    except Exception:  # noqa: BLE001 — journalling must not raise inside a tool call
        return None


def _rel(path: Path, project_root: Path) -> str:
    """Path as the user would name it. `_resolve_path` returns a RESOLVED path,
    so the root must be resolved too or every message prints an absolute path
    on any machine where the root traverses a symlink (/var -> /private/var on
    macOS, which is every temp dir)."""
    for root in (project_root.resolve(), project_root):
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    return str(path)


def guard(path: Path, before: str | None, after: str, project_root: Path) -> tuple[bool, str]:
    """Decide whether this write lands, and what the model is told.

    Returns ``(apply_it, message)``. The message is the tool result, so it must
    tell the model plainly that a proposed edit was NOT applied — otherwise it
    reports success in its final answer and you believe a file changed that did
    not.
    """
    rel = _rel(path, project_root)
    current = mode()
    if current == MODE_OFF:
        return False, (
            f"REFUSED: writing is disabled (LLM_ROUTER_AGENT_WRITES=off). "
            f"{rel} was NOT modified. Do not retry this edit — report what you "
            f"would have changed and why, and stop."
        )

    diff = unified_diff(before or "", after, rel)

    if current == MODE_PROPOSE:
        return False, (
            f"PROPOSED — NOT APPLIED. {rel} is unchanged on disk.\n"
            f"The patch below is the deliverable; a human applies it.\n"
            f"Do not retry this edit and do not claim the file was modified.\n"
            f"(Set LLM_ROUTER_AGENT_WRITES=apply to let edits land.)\n\n{diff}"
        )

    pre = journal(path, before, after, project_root)
    if pre is None:
        return False, (
            f"REFUSED: could not journal the previous contents of {rel}, so this "
            f"edit cannot be undone. Nothing was written. Do not retry."
        )
    return True, (
        f"APPLIED to {rel}. Previous contents saved at {pre} "
        f"(restore with: cp {pre} {path}).\n\n{diff}"
    )
