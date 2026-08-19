"""Local ReAct harness — a tier-0 Agent that runs a bounded tool-loop over a
local model (Ollama's native tool-calling API) with a sandboxed tool executor.

This is what turns a bare local text model into an *agent* for milestone
delegation. Both the model client and the tool executor are injected, so unit
tests drive the whole loop with fakes — no live model, no real shell — while the
default wiring talks to a real Ollama server + a bounded shell/file/gh executor.

Anti-stuck: the loop is hard-bounded by ``max_steps``; if the model never emits a
final answer it stops and returns what it has (the milestone's objective
acceptance check then decides pass/fail — the model's own claim is never trusted).
Local-model reliability is best-effort by nature; confidence is reported low.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm_router.agentic.adapters import pack_prompt
from llm_router.agentic.engine import AgentRunResult
from llm_router.agentic.ledger import Milestone
from llm_router.safe_subprocess import get_delegated_env


@dataclass
class ToolCall:
    # NOT frozen: ``args`` is a dict, so a frozen dataclass would generate an
    # unhashable __hash__ that raises only when hashed — a latent footgun.
    name: str
    args: dict[str, Any]


@dataclass
class ChatTurn:
    """One model turn: a final ``content`` answer, ``tool_calls``, or an
    ``error`` (client-level failure — kept DISTINCT from a legitimate empty
    answer so the engine can surface an honest reason instead of a blank)."""
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    error: str = ""


# client(messages, tools) -> ChatTurn ; executor(name, args) -> result string.
OllamaClient = Callable[[list[dict[str, Any]], list[dict[str, Any]]], ChatTurn]
ToolExecutor = Callable[[str, dict[str, Any]], str]

_SYSTEM = (
    "You are a local coding agent. Use the provided tools to accomplish the task. "
    "When done, reply with a short final message (no tool call). Be concrete — an "
    "objective check verifies your work, so make real changes."
)

# Ollama tool schemas advertised to the model.
DEFAULT_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command and return its output.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a file's contents.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write content to a file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                       "content": {"type": "string"}}, "required": ["path", "content"]}}},
]


# P1-S1 (audit R1): bash sandbox for the delegated agentic executor. Routing an
# *execution* to a cheap model means it runs bash in the user's repo, so refuse the
# obviously-dangerous classes. Defense-in-depth (a denylist + the path containment
# below), NOT a complete OS sandbox — an obfuscated command can still slip a
# denylist, so irreversible delegated work belongs behind the MGEE worktree gate.
# 🥷 Backslash-Security: using vibe-coding rules for Command Injection Prevention
_BASH_DESTRUCTIVE_RE = re.compile(
    r"(?xi)"
    r"\brm\s+-\w*[rf]\w*\s |"                         # rm -rf / -fr / -r -f
    r"\b(mkfs|shred|fdisk)\b | \bdd\s+if= |"
    r"\b(shutdown|reboot|halt|poweroff)\b |"
    r"\bchmod\s+-R\b | \bchown\s+-R\b |"
    r"\bsudo\b | \bsu\s+- | \bdoas\b |"
    r":\s*\(\s*\)\s*\{"                               # fork bomb
)
_BASH_SENSITIVE_RE = re.compile(
    r"(?xi)"
    r"\.ssh(?:/|\b) | \.aws(?:/|\b) | \.gnupg(?:/|\b) |"
    r"id_rsa | id_ed25519 | id_dsa |"
    r"/etc/(?:shadow|sudoers) |"
    r"\bcredentials\b | \.netrc\b | \.pypirc\b | \.npmrc\b | \.env\b"
)
_BASH_NET_TOOL_RE = re.compile(r"(?i)\b(curl|wget|httpie|http|nc|ncat|telnet|ssh|scp|sftp|rsync)\b")
_BASH_LOCALHOST_RE = re.compile(r"(?i)(localhost|127\.0\.0\.1|::1|0\.0\.0\.0)")
# RED6-01: environment reads. DEFENCE IN DEPTH ONLY — see _bash_block_reason.
_BASH_ENV_DUMP_RE = re.compile(
    r"(?xi)"
    r"\b(env|printenv|set|export|declare|typeset)\b |"
    r"/proc/[^/\s]+/environ |"                        # cat /proc/self/environ
    r"os\.environ |"                                  # python -c '...os.environ...'
    # A shell expansion of anything credential-shaped: $FOO_KEY, ${FOO_TOKEN}.
    r"\$\{?[A-Za-z_]*(KEY|TOKEN|SECRET|PASSWORD|CRED|PAT)\b"
)
_HEX_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")


def _normalize_command(command: str) -> str:
    """Undo the cheapest ways to hide a keyword from a regex.

    ``e''nv``, ``e""nv`` and ``$'\\x65nv'`` are all ``env``. A matcher that only
    sees the raw string is defeated by a shell feature, not by cleverness, so it
    is worth spending five lines to close. This does NOT make the blocklist
    complete — nothing does — it just stops the trivial cases from being free.
    """
    c = _HEX_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), command or "")
    return c.replace("'", "").replace('"', "").replace("\\", "")


def _bash_block_reason(command: str) -> str | None:
    """Return a reason if the shell command is disallowed by the sandbox, else None.

    **This function is not the security boundary and must never be treated as
    one.** It pattern-matches a string that a language model wrote, and a model
    that can emit arbitrary shell can defeat any pattern list — ``$'\\x65nv'``,
    ``e''nv``, ``cat /proc/self/environ``, a base64'd payload, a Python
    one-liner. Every entry here is a speed bump.

    The boundary for credential exposure is the allowlisted ``env=`` passed to
    the subprocess in :func:`default_tool_executor`: the secrets are not present
    in the child, so there is nothing for a bypass to read. If you find yourself
    adding a pattern here to close a hole, the hole is somewhere else.
    """
    raw = command or ""
    # Match against the raw string AND a de-quoted/de-escaped form, so `e''nv`
    # is not a free bypass. Both, not just the normalized one: normalization
    # strips quotes, which could in principle join two harmless tokens.
    forms = (raw, _normalize_command(raw))

    def _hit(rx: re.Pattern[str]) -> bool:
        return any(rx.search(f) for f in forms)

    if _hit(_BASH_DESTRUCTIVE_RE):
        return "destructive or privilege-escalating command"
    if _hit(_BASH_SENSITIVE_RE):
        return "access to a sensitive credential path"
    if _hit(_BASH_ENV_DUMP_RE):
        return "environment inspection"
    if _hit(_BASH_NET_TOOL_RE) and not _hit(_BASH_LOCALHOST_RE):
        return "external network egress"
    return None


def default_tool_executor(cwd: str | None = None, timeout: float = 30.0) -> ToolExecutor:
    """A bounded shell/file executor. Not a security sandbox — it caps time and
    output; run delegated irreversible work behind the MGEE worktree gate."""
    base = Path(cwd).resolve() if cwd else Path.cwd()

    def _resolve(path_arg: str) -> Path:
        # 🥷 Backslash-Security: using vibe-coding rules for Path Traversal & Directory Access
        # Relative paths resolve UNDER the working dir (a relative "marker.txt"
        # must land in cwd, not the process dir), and the result must stay within
        # base — a model-supplied "../../etc/passwd" is rejected, not followed.
        p = Path(path_arg)
        resolved = (p if p.is_absolute() else base / p).resolve()
        if base not in resolved.parents and resolved != base:
            raise ValueError(f"path escapes working directory: {path_arg}")
        return resolved

    def execute(name: str, args: dict[str, Any]) -> str:
        try:
            if name == "bash":
                command = str(args.get("command", ""))
                reason = _bash_block_reason(command)
                if reason is not None:
                    return f"tool error: blocked ({reason}) — refusing: {command[:120]}"
                # RED6-01 (P0): an explicit allowlisted env. Without `env=` the
                # child inherited every provider key in the parent process, so a
                # single injected `env` / `printenv` in a model-authored command
                # exfiltrated the user's credentials. The blocklist above is not
                # what stops that — this is. The keys are not in the child to be
                # read in the first place.
                proc = subprocess.run(
                    ["/bin/sh", "-c", command], cwd=str(base),
                    env=get_delegated_env(),
                    capture_output=True, text=True, timeout=timeout, check=False,
                )
                out = (proc.stdout or "") + (proc.stderr or "")
                return f"[exit {proc.returncode}]\n{out[:4000]}"
            if name == "read_file":
                return _resolve(args["path"]).read_text(encoding="utf-8", errors="ignore")[:4000]
            if name == "write_file":
                p = _resolve(args["path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(str(args.get("content", "")), encoding="utf-8")
                return f"wrote {p}"
            return f"unknown tool: {name}"
        except Exception as exc:  # noqa: BLE001 — a tool error is returned to the model, never fatal
            return f"tool error: {exc}"
    return execute


@dataclass
class ReActAgent:
    """A local ReAct agent (tier 0). ``client`` + ``executor`` are injected."""

    tier: int = 0
    client: OllamaClient | None = None
    executor: ToolExecutor | None = None
    # Default to a model that is actually installed AND supports Ollama native
    # tool-calling. qwen2.5-coder:7b is frequently absent -> 404 -> silent empty
    # runs; qwen2.5:7b is the warm local default.
    model: str = "qwen2.5:7b"
    max_steps: int = 8
    cwd: str | None = None
    cost_per_call_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.executor is None:
            self.executor = default_tool_executor(cwd=self.cwd)
        if self.client is None:
            self.client = _default_ollama_client(self.model)

    def run(
        self, milestone: Milestone, frozen_context: list[dict[str, Any]], budget_left: float
    ) -> AgentRunResult:
        assert self.client is not None and self.executor is not None
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": pack_prompt(milestone, frozen_context)},
        ]
        actions: list[dict[str, Any]] = []
        final = ""
        error = ""
        steps = 0
        for steps in range(1, self.max_steps + 1):
            turn = self.client(messages, DEFAULT_TOOLS)
            if turn.error:
                # Client-level failure (model missing, unreachable, bad JSON) —
                # record it and stop; do NOT treat it as an empty final answer.
                error = turn.error
                break
            if not turn.tool_calls:
                final = turn.content
                break
            messages.append({"role": "assistant", "content": turn.content})
            for tc in turn.tool_calls:
                result = self.executor(tc.name, tc.args)
                actions.append({"tool": tc.name, "args": tc.args, "result": result[:500]})
                messages.append({"role": "tool", "name": tc.name, "content": result})

        artifacts: dict[str, Any] = {
            "provider": "ollama-react",
            "tier": self.tier,
            "mid": milestone.id,
            "output": final,
            "actions": actions,
            "steps": steps,
            "hit_step_cap": (steps >= self.max_steps and not final),
            "error": error,
        }
        # local models are best-effort; confidence stays low even on a clean
        # finish, and lowest when the run errored out with nothing.
        confidence = 0.6 if final else 0.2
        return AgentRunResult(artifacts, cost_usd=self.cost_per_call_usd, confidence=confidence)



def _validated_ollama_env_url(raw: str) -> str:
    """CHZ-SEC-06: never hand an unvalidated env URL to urlopen.

    Imported, not reimplemented — three earlier copies of this reader diverged
    and bypassed the fix. Fails CLOSED: an unavailable validator falls back to
    localhost rather than honouring an unchecked URL.
    """
    default = "http://localhost:11434"
    try:
        from llm_router.config import validate_ollama_url
    except Exception:
        return raw if raw == default else default
    return validate_ollama_url(raw) or default

def _default_ollama_client(model: str, base_url: str | None = None) -> OllamaClient:
    """Real client hitting Ollama's /api/chat with tools. Lazy/deferred; unit
    tests inject a fake and never reach this."""
    import os
    import urllib.request

    url = _validated_ollama_env_url(
        base_url or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434"
    ).rstrip("/")

    def client(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatTurn:
        body = json.dumps({"model": model, "messages": messages, "tools": tools,
                           "stream": False}).encode()
        req = urllib.request.Request(  # noqa: S310 — fixed localhost Ollama URL from config
            f"{url}/api/chat", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
                data = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001 — surface the failure, don't fake an empty answer
            return ChatTurn(error=f"ollama call failed ({model}): {exc}")
        msg = data.get("message", {}) or {}
        calls = [
            ToolCall(tc["function"]["name"], tc["function"].get("arguments", {}) or {})
            for tc in (msg.get("tool_calls") or [])
        ]
        return ChatTurn(content=msg.get("content", "") or "", tool_calls=calls)

    return client
