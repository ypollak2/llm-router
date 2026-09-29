"""May a local model serve this proxy step, and by which path?

``can_serve_locally(step)`` runs BEFORE any local call; ``check_reply`` runs on
the local model's reply BEFORE it could be served. Both return a ``Decision``
whose ``reason`` the proxy writes to its ledger row, served or not.

Routes:
  ``local_loop``     the raw tool-call loop (Ollama native tool calls on the
                     compacted request) may answer this step;
  ``edit_protocol``  the step is edit-shaped; ``llm_router.edit``'s validated
                     protocol (JSON edit instructions, exact-once match, syntax
                     gate, retry with the rejection fed back) produces the Edit;
  ``claude``         forward to Anthropic.

THE HARD RULE. An edit-shaped reply from the raw tool-call loop (a tool call
to Edit, Write, MultiEdit or NotebookEdit) is NEVER served. Through that loop
edits passed 0/20 on this machine's fixtures (``~/.rsi/runs/routing-golden-v1/
20260928T094008Z-baseline/results.jsonl``, task_class=edit), against 19/20
through the edit protocol (``.../20260928T103518Z-postdeploy-e893c8d/
results_edit_scoped.jsonl``). ``check_reply`` enforces this for every served
reply, with or without ``LLM_ROUTER_LOCAL_AGENT``.

What this generalises (and the tests pin it to):
  * ``proxy.steps.step_class`` — the step-shape gate is the first check;
  * ``hooks/agent-route.py:_is_codex_suitable`` — the task-type allowlist and
    the multi-file-write signals are the same values, applied to the proxy's
    policy view and the session's original ask
    (``test_local_agent.py`` asserts they equal the hook's constants);
  * ``quality_breaker.should_route("proxy", task_type)`` — the NS4 breaker on
    the lever northstar already records proxy-served turns under;
  * ``zero_claude_edit.dirty_files`` — the edit protocol never targets a file
    with uncommitted changes, the rule that lever already follows.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from llm_router.local_agent.compact import (
    EDIT_TOOLS,
    newest_results_text,
    original_ask,
    session_cwd,
)
from llm_router.proxy.steps import non_system, step_class
from llm_router.proxy.translate import new_msg_id, new_tool_id, schema_error

ROUTE_LOCAL = "local_loop"
ROUTE_EDIT = "edit_protocol"
ROUTE_CLAUDE = "claude"

BREAKER_LEVER = "proxy"

# Same values as hooks/agent-route.py (_CODEX_SUITABLE_TASK_TYPES,
# _MULTI_FILE_WRITE_SIGNALS); the hook is a standalone script, so the values
# are mirrored and a test fails if they drift.
SUITABLE_TASK_TYPES = frozenset({"research", "analyze", "code", "query"})
MULTI_FILE_WRITE_SIGNALS = re.compile(
    r"\b(?:across (?:multiple|all|every|several) files?|multi-file|"
    r"refactor (?:the )?(?:entire|whole|full)(?:\s+(?:codebase|repo|repository|project))?|"
    r"rewrite (?:the )?(?:entire|whole|full)|"
    r"implement (?:this |it )?across|"
    r"edit (?:multiple|several|many) files|"
    r"large[- ]scale (?:refactor|migration)|"
    r"migrate (?:the )?(?:entire|whole|full)?\s*codebase)\b",
    re.IGNORECASE,
)

_BREAKER_TTL_S = 600.0
_EDIT_NUM_PREDICT = 1500
_MIN_EDIT_ATTEMPT_S = 4.0


@dataclass(frozen=True)
class Decision:
    route: str
    reason: str
    detail: str = ""
    targets: tuple[str, ...] = ()

    @property
    def local(self) -> bool:
        return self.route != ROUTE_CLAUDE

    def as_row(self) -> dict:
        row = {"route": self.route, "reason": self.reason}
        if self.detail:
            row["detail"] = self.detail[:200]
        return row


@dataclass
class Step:
    """One proxy call as capability sees it: the request plus the policy's view."""

    body: dict
    enabled_steps: frozenset | set
    task_type: str | None = None
    model: str | None = None


# ── breaker (cached: should_route scans northstar units, ~2 s a call) ────────

BreakerFn = Callable[[str, str | None], object]


@dataclass
class BreakerCache:
    fn: BreakerFn | None = None
    ttl_s: float = _BREAKER_TTL_S
    _memo: dict = field(default_factory=dict)

    def _call(self, task_type: str | None):
        fn = self.fn
        if fn is None:
            from llm_router.quality_breaker import should_route
            fn = should_route
        return fn(BREAKER_LEVER, task_type)

    async def allows(self, task_type: str | None) -> tuple[bool, str]:
        hit = self._memo.get(task_type)
        now = time.monotonic()
        if hit is not None and now - hit[0] < self.ttl_s:
            return hit[1], hit[2]
        try:
            d = await asyncio.to_thread(self._call, task_type)
            allowed, reason = bool(getattr(d, "allowed", True)), str(getattr(d, "reason", ""))
        except Exception as exc:  # noqa: BLE001 - fail-open, like quality_breaker itself
            allowed, reason = True, f"breaker error (fail-open): {type(exc).__name__}"
        self._memo[task_type] = (now, allowed, reason)
        return allowed, reason


# ── before the local call ────────────────────────────────────────────────────


async def can_serve_locally(step: Step, breaker: BreakerCache) -> Decision:
    """Pre-call gate. Shape, then task shape, then the quality breaker."""
    if step_class(step.body, step.enabled_steps) is None:
        return Decision(ROUTE_CLAUDE, "not_eligible")
    if step.model is None:
        return Decision(ROUTE_CLAUDE, "policy_kept")
    if step.task_type not in SUITABLE_TASK_TYPES:
        return Decision(ROUTE_CLAUDE, "task_type_unsuitable", f"task_type={step.task_type}")
    m = MULTI_FILE_WRITE_SIGNALS.search(original_ask(step.body))
    if m:
        return Decision(ROUTE_CLAUDE, "multi_file_write", m.group(0))
    allowed, why = await breaker.allows(step.task_type)
    if not allowed:
        return Decision(ROUTE_CLAUDE, "breaker_open", why)
    return Decision(ROUTE_LOCAL, "capable", why)


# ── on the local reply ───────────────────────────────────────────────────────


def _edit_calls(message: dict) -> list[dict]:
    return [b for b in message.get("content") or []
            if b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS]


def _abs(path: str, cwd: str | None) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute() and cwd:
        p = Path(cwd) / p
    return os.path.realpath(str(p))


def _seen_paths(body: dict, cwd: str | None) -> set[str]:
    """Files this session already Read (or wrote): Claude Code refuses an Edit
    to a file it has not read, so the protocol only targets these."""
    seen = set()
    for m in non_system(body.get("messages") or []):
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("name") in ("Read", "Edit", "Write", "MultiEdit")):
                fp = (b.get("input") or {}).get("file_path")
                if isinstance(fp, str) and fp:
                    seen.add(_abs(fp, cwd))
    return seen


def check_reply(message: dict, body: dict, *, edit_mode: str = "protocol",
                dirty_fn: Callable[[Path, list[str]], list[str]] | None = None) -> Decision:
    """Post-call gate: never serve an edit from the raw loop (see module doc)."""
    calls = _edit_calls(message)
    if not calls:
        return Decision(ROUTE_LOCAL, "non_edit_reply")
    names = sorted({c["name"] for c in calls})
    if edit_mode != "protocol":
        return Decision(ROUTE_CLAUDE, "edit_to_claude", f"edit-shaped ({','.join(names)}); edit mode claude")
    if any(n not in ("Edit", "MultiEdit") for n in names):
        return Decision(ROUTE_CLAUDE, "edit_to_claude", f"edit-shaped ({','.join(names)}): not a replacement edit")
    if not any(isinstance(t, dict) and t.get("name") == "Edit" and "input_schema" in t
               for t in body.get("tools") or []):
        return Decision(ROUTE_CLAUDE, "edit_to_claude", "request carries no Edit tool")
    cwd = session_cwd(body)
    targets: list[str] = []
    for c in calls:
        fp = (c.get("input") or {}).get("file_path")
        if not isinstance(fp, str) or not fp:
            return Decision(ROUTE_CLAUDE, "edit_to_claude", "edit call without file_path")
        path = _abs(fp, cwd)
        if path not in targets:
            targets.append(path)
    if len(targets) > 1:
        return Decision(ROUTE_CLAUDE, "edit_to_claude", f"{len(targets)} target files; protocol takes one")
    target = targets[0]
    if not Path(target).is_file():
        return Decision(ROUTE_CLAUDE, "edit_to_claude", "target is not an existing file")
    if target not in _seen_paths(body, cwd):
        return Decision(ROUTE_CLAUDE, "edit_to_claude", "target not read in this session")
    from llm_router import zero_claude_edit

    root = zero_claude_edit._repo_root(str(Path(target).parent))
    if root is None:
        return Decision(ROUTE_CLAUDE, "edit_to_claude", "target not in a git repo (cleanliness unverifiable)")
    rel = os.path.relpath(target, os.path.realpath(str(root)))
    dirty = (dirty_fn or zero_claude_edit.dirty_files)(root, [rel])
    if dirty:
        return Decision(ROUTE_CLAUDE, "edit_to_claude", "target has uncommitted changes")
    return Decision(ROUTE_EDIT, "edit_shaped", ",".join(names), targets=(target,))


# ── the edit protocol, as a proxy step ───────────────────────────────────────

GenerateFn = Callable[[str, str, int, float], Awaitable[tuple[str | None, dict]]]


def _edit_task(body: dict, draft_calls: list[dict]) -> str:
    draft = []
    for c in draft_calls:
        inp = c.get("input") or {}
        pairs = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
        for e in pairs:
            if isinstance(e, dict):
                draft.append(f"- replace:\n{str(e.get('old_string', ''))[:600]}\n  with:\n"
                             f"{str(e.get('new_string', ''))[:600]}")
    task = original_ask(body)[:3000]
    out = newest_results_text(body, 3000)
    text = f"{task}\n\n## Most recent tool output\n{out}"
    if draft:
        text += ("\n\n## A draft change for this step (unverified; it may be wrong, "
                 "produce your own exact edit)\n" + "\n".join(draft)[:2000])
    return text


async def run_edit_protocol(decision: Decision, message: dict, body: dict, generate: GenerateFn,
                            deadline: float) -> tuple[dict | None, str | None, dict]:
    """``(anthropic_message | None, error | None, info)``.

    ``llm_router.edit``: build_edit_prompt -> parse_edit_response ->
    apply_edits (exact-once match on the file as it is on disk now, syntax
    gate), up to ``edit.MAX_EDIT_ATTEMPTS`` tries with the rejection fed back,
    within ``deadline`` (a ``time.monotonic()`` instant). A validated result is
    served as Edit tool calls with the request's own schema."""
    from llm_router.edit import (
        MAX_EDIT_ATTEMPTS,
        apply_edits,
        build_edit_prompt,
        parse_edit_response,
        read_file_for_edit,
    )
    from llm_router.zero_claude_edit import _EDIT_SYSTEM_PROMPT

    target = decision.targets[0]
    content, truncated = read_file_for_edit(target)
    info: dict = {"attempts": 0, "rejections": []}
    if truncated:
        return None, "target larger than the edit protocol's 32 KB read cap", info
    contents = {target: content}
    task = _edit_task(body, _edit_calls(message))
    schema = next((t.get("input_schema") or {} for t in body.get("tools") or []
                   if isinstance(t, dict) and t.get("name") == "Edit"), {})
    feedback = None
    usage_total = {"prompt_tokens": 0, "output_tokens": 0}
    for attempt in range(1, MAX_EDIT_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining < _MIN_EDIT_ATTEMPT_S:
            info["rejections"].append(f"attempt {attempt}: out of step budget")
            break
        info["attempts"] = attempt
        raw, usage = await generate(_EDIT_SYSTEM_PROMPT, build_edit_prompt(task, contents, feedback=feedback),
                                    _EDIT_NUM_PREDICT, remaining)
        for k in usage_total:
            usage_total[k] += int((usage or {}).get(k) or 0)
        if not raw:
            feedback = "the model returned no response"
            info["rejections"].append(f"attempt {attempt}: empty")
            continue
        instructions, warnings = parse_edit_response(raw)
        if not instructions:
            feedback = "; ".join(warnings) or "no edit instructions in response"
            info["rejections"].append(f"attempt {attempt}: parse")
            continue
        new_contents, reasons = apply_edits(contents, instructions)
        if new_contents is None:
            feedback = "; ".join(reasons)
            info["rejections"].append(f"attempt {attempt}: validation")
            continue
        blocks = []
        for ins in instructions:
            args = {"file_path": target, "old_string": ins.old_string, "new_string": ins.new_string}
            err = schema_error(args, schema)
            if err:
                return None, f"Edit schema: {err}", info
            blocks.append({"type": "tool_use", "id": new_tool_id(), "name": "Edit", "input": args})
        info["edits"] = len(blocks)
        return {
            "id": new_msg_id(), "type": "message", "role": "assistant", "model": body.get("model"),
            "content": blocks, "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": usage_total["prompt_tokens"], "output_tokens": usage_total["output_tokens"],
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        }, None, info
    return None, "edit protocol: " + "; ".join(info["rejections"] or ["no attempt"]), info
