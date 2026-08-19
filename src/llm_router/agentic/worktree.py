"""Reversibility gate + git-worktree isolation.

Irreversible milestones (push / merge / delete / external-send) must not
auto-freeze on a bare acceptance pass. The agent runs its edits in an ISOLATED
git worktree; the gate merges that worktree back only if the milestone verified
there, and discards it otherwise. Worktree ops are injected behind ``WorktreeOps``
so unit tests drive the gate with a fake — no real git in tests.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Protocol

from llm_router.agentic.engine import AgentRunResult, Gate
from llm_router.agentic.ledger import Milestone


class WorktreeOps(Protocol):
    def create(self, name: str) -> str: ...       # returns worktree path
    def merge(self, name: str) -> bool: ...        # apply changes back; True on success
    def discard(self, name: str) -> None: ...      # remove without merging


@dataclass
class FakeWorktreeOps:
    """In-memory worktree ops for tests. ``merge_ok`` scripts merge success."""
    merge_ok: bool = True
    created: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)

    def create(self, name: str) -> str:
        self.created.append(name)
        return f"/tmp/wt/{name}"

    def merge(self, name: str) -> bool:
        self.merged.append(name)
        return self.merge_ok

    def discard(self, name: str) -> None:
        self.discarded.append(name)


@dataclass
class GitWorktreeOps:
    """Real git-backed worktree ops (not exercised in unit tests)."""
    repo: str
    base_ref: str = "HEAD"

    def create(self, name: str) -> str:
        path = f"{self.repo}/.llm_router-worktrees/{name}"
        subprocess.run(
            ["git", "-C", self.repo, "worktree", "add", "--detach", path, self.base_ref],
            capture_output=True, text=True, check=False,
        )
        return path

    def merge(self, name: str) -> bool:
        path = f"{self.repo}/.llm_router-worktrees/{name}"
        # apply the worktree's diff back onto the main tree
        diff = subprocess.run(
            ["git", "-C", path, "diff", self.base_ref], capture_output=True, text=True, check=False,
        )
        if diff.returncode != 0:
            return False
        apply = subprocess.run(
            ["git", "-C", self.repo, "apply", "--3way"], input=diff.stdout,
            capture_output=True, text=True, check=False,
        )
        ok = apply.returncode == 0
        self.discard(name)
        return ok

    def discard(self, name: str) -> None:
        path = f"{self.repo}/.llm_router-worktrees/{name}"
        subprocess.run(
            ["git", "-C", self.repo, "worktree", "remove", "--force", path],
            capture_output=True, text=True, check=False,
        )


def reversibility_gate(ops: WorktreeOps) -> Gate:
    """A gate() for MGEEEngine. Reversible milestones freeze normally. Irreversible
    ones freeze only if their worktree (``artifacts['worktree']``) merges cleanly;
    a missing worktree or a failed merge blocks the freeze (→ surfaced)."""
    def gate(milestone: Milestone, result: AgentRunResult) -> bool:
        if milestone.reversible:
            return True
        name = result.artifacts.get("worktree")
        if not name:
            return False  # irreversible work that wasn't isolated → refuse to freeze
        if ops.merge(name):
            return True
        ops.discard(name)
        return False
    return gate
