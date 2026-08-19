"""Real-backend adapters implementing the MGEE ``Agent`` protocol.

Adapters shell out to agent CLIs (Codex now; Gemini/Antigravity later) but take
an injected ``runner`` so unit tests drive them with a fake subprocess — the
deterministic engine guarantees never depend on a live model. The adapter only
*runs* the agent and captures what it produced; whether the milestone is DONE is
decided by the milestone's own objective acceptance check, never the CLI's
self-report.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from llm_router.agentic.engine import AgentRunResult
from llm_router.agentic.ledger import Milestone
from llm_router.safe_subprocess import get_delegated_env


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


# runner(argv, input_text) -> ProcResult. Injected so tests never spawn a model.
Runner = Callable[[list[str], str], ProcResult]

#: Non-secret configuration an agent CLI needs, passed through explicitly.
#:
#: No credential belongs here. The Codex CLI authenticates from ``~/.codex/auth.json``
#: (reachable via HOME, which the allowlist carries), so it does not need a key
#: in its environment — and it must not have one, because it is the process that
#: runs model-authored commands.
_AGENT_CLI_PASSTHROUGH: tuple[str, ...] = (
    "CODEX_PATH",             # a filesystem path to the binary, not a secret
    "LLM_ROUTER_CODEX_MODELS",
    "LLM_ROUTER_CODEX_TIMEOUT",
)


def _agent_cli_env() -> dict[str, str]:
    import os

    return {k: os.environ[k] for k in _AGENT_CLI_PASSTHROUGH if k in os.environ}


def subprocess_runner(
    argv: list[str], input_text: str, *, cwd: str | None = None, timeout: float = 300.0
) -> ProcResult:
    """Real subprocess runner. A missing binary / timeout is a captured result,
    not an exception — the flow must never hang on a backend.

    RED6-01 (P0): the child gets an allowlisted environment. This runner spawns
    an agent CLI that executes model-authored work, and it inherited the parent's
    entire environment — every provider key the router holds. It also bypassed
    the codebase's own ``codex_agent.run_codex()``, the guarded path, so the
    protection that already existed was routed around rather than missing.
    """
    try:
        proc = subprocess.run(
            argv, input=input_text, capture_output=True, text=True,
            cwd=cwd, timeout=timeout, check=False,
            env=get_delegated_env(_agent_cli_env()),
        )
    except FileNotFoundError:
        return ProcResult(127, "", f"binary not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return ProcResult(124, "", f"timed out after {timeout}s")
    return ProcResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def pack_prompt(milestone: Milestone, frozen_context: list[dict[str, Any]]) -> str:
    """Build the delegated prompt: the current milestone + the frozen done-work
    so the agent resumes at the frontier instead of redoing completed milestones."""
    lines = [f"TASK: {milestone.description or milestone.id}"]
    _CONTEXT_IDS = {"SESSION_CONTEXT", "RELEVANT_CONTEXT"}
    relevant = [c for c in frozen_context if c.get("id") == "RELEVANT_CONTEXT"]
    context = [c for c in frozen_context if c.get("id") == "SESSION_CONTEXT"]
    completed = [c for c in frozen_context if c.get("id") not in _CONTEXT_IDS]
    if relevant:
        # CF-2: capability-provisioned repo context. Rendered first (already a
        # bounded, structured block); it is context, NOT a completed milestone.
        lines += [""]
        lines += [str(c.get("description")) for c in relevant]
    if context:
        lines += ["", "CONVERSATION CONTEXT (from the calling session — use it, don't echo it back):"]
        lines += [f"  {c.get('description')}" for c in context]
    if completed:
        lines += ["", "ALREADY COMPLETED — build on these, do NOT redo:"]
        for c in completed:
            lines.append(f"  - [{c.get('id')}] {c.get('description') or c.get('id')}")
            # RED3-06: forward what the milestone PRODUCED, not just that it ran.
            # Without this a milestone that semantically depends on an earlier
            # one's output was told "M1: done" and nothing more, so it could only
            # guess or redo the work. Already neutralised as untrusted by
            # TaskLedger.frozen_context() — do not re-wrap or unwrap it here.
            rendered = c.get("artifacts_rendered")
            if rendered:
                lines += [f"    {line}" for line in str(rendered).splitlines()]
    lines += ["", "An objective check will verify your work — make real, correct changes."]
    return "\n".join(lines)


@dataclass
class CodexAdapter:
    """Delegate a milestone to the Codex CLI. Fake-testable via ``runner``.

    ChatGPT-subscription Codex is metered at $0, so ``cost_per_call_usd``
    defaults to 0.0; savings are computed against a premium baseline separately.
    """

    tier: int
    runner: Runner | None = None
    binary: str | None = None       # None → resolve via find_codex_binary() (fallback 'codex')
    model: str | None = None        # e.g. 'gpt-5.5'; omitted → Codex CLI default
    cwd: str | None = None          # working dir codex edits in (also where diff is captured)
    capture_diff: bool = True       # capture `git diff` after the run as the produced artifact
    # Codex defaults to a READ-ONLY sandbox in exec mode, which rejects every
    # patch ("writing is blocked by read-only sandbox") — a delegated agent that
    # cannot write is useless. workspace-write confines edits to the workspace
    # (cwd) without granting full-disk/network access. Overridable per adapter.
    sandbox_mode: str = "workspace-write"
    cost_per_call_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.runner is None:
            self.runner = subprocess_runner

    def _resolve_binary(self) -> str:
        if self.binary:
            return self.binary
        try:
            from llm_router.codex_agent import find_codex_binary
            return find_codex_binary() or "codex"
        except Exception:  # noqa: BLE001 — resolver failure falls back to PATH lookup
            return "codex"

    def _codex_argv(self, binary: str, prompt: str) -> list[str]:
        argv = [binary, "exec", "--json", "--color", "never", "--skip-git-repo-check"]
        if self.sandbox_mode:
            argv += ["--sandbox", self.sandbox_mode]
        if self.model:
            argv += ["-m", self.model, "-c", "model_provider=openai"]
        if self.cwd:
            argv += ["-C", self.cwd]
        argv.append(prompt)
        return argv

    def run(
        self, milestone: Milestone, frozen_context: list[dict[str, Any]], budget_left: float
    ) -> AgentRunResult:
        prompt = pack_prompt(milestone, frozen_context)
        assert self.runner is not None  # set in __post_init__
        binary = self._resolve_binary()
        proc = self.runner(self._codex_argv(binary, prompt), "")

        diff, files = "", []
        if self.capture_diff and self.cwd:
            # `git diff HEAD` captures staged + unstaged work. Codex often COMMITS
            # its changes, in which case that's empty — fall back to the last
            # commit's diff so produced files are still captured.
            d = self.runner(["git", "-C", self.cwd, "diff", "HEAD"], "")
            if d.returncode == 0 and not d.stdout.strip():
                d = self.runner(["git", "-C", self.cwd, "diff", "HEAD~1", "HEAD"], "")
            if d.returncode == 0 and d.stdout:
                diff = d.stdout
                files = [
                    ln[6:].strip() for ln in diff.splitlines() if ln.startswith("+++ b/")
                ]

        artifacts: dict[str, Any] = {
            "provider": "codex",
            "tier": self.tier,
            "mid": milestone.id,
            "returncode": proc.returncode,
            "output": proc.stdout,
            "stderr": proc.stderr,
            "diff": diff,
            "files": files,
            "prompt_sent": prompt,
        }
        confidence = 1.0 if proc.returncode == 0 else 0.3
        return AgentRunResult(artifacts, cost_usd=self.cost_per_call_usd, confidence=confidence)
