"""Opt-in ``--serve local-agent`` mode: Claude Code served by a local model.

Redesign PR 1 (``~/.rsi/research/harness-parity/PROXY_REDESIGN_PLAN.md``). The
2026-10-04 route probes found that Claude Code -> proxy -> Ollama served 0 of 9
steps locally as designed, and 7 of 11 capabilities at >=19/20 only after three
in-memory patches (P1-P3) plus ``--trim none``. This module makes those patches
real, in this mode ONLY. With ``--serve off`` (the default) none of it runs and
the proxy is byte-identical to before (``tests/test_proxy_off_golden.py``).

What the mode changes, each tied to the failure it fixes:

* P1  ``is_agent_turn``: every tool-carrying main-loop call is eligible, not
  only calls whose newest turn is all ``tool_result`` blocks. Without it the
  FIRST call of every turn (where the model picks Skill / Agent / MCP / Bash)
  always went to Anthropic.
* P2  the policy (``choose_model``) is not consulted: the pinned ``--model``
  serves. The policy could return "keep on Claude" for a step the owner chose
  to run locally.
* P3  an Edit/Write reply is served: through the validated edit protocol when
  the file qualifies, else raw, and every served edit is checked AFTER it is
  applied (``check_applied``, run at the next step against the file on disk).
* whole conversations are pinned at their first call and stay put
  (``LocalModeState.pins``): moving a conversation mid-way re-writes the whole
  prompt cache on the other side, and a conversation that started on Claude
  is never moved to the local model.

Never silent: every step that is not served locally carries a ledger reason
and prints an egress notice; a kill switch (a file) takes effect on the next
request without a restart; the startup banner states the mode; an input the
local path cannot handle (media, a prompt over the cap) is escalated
explicitly with a reason, never replaced by a placeholder.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from llm_router.local_context_guard import (
    DEFAULT_RESERVED_OUTPUT_TOKENS,
    ContextOverflow,
    check_overflow,
    estimate_payload_tokens,
)
from llm_router.proxy.steps import non_system

ENV_MODE = "LLM_ROUTER_PROXY_LOCAL_AGENT_MODE"
SERVE_OFF = "off"
SERVE_LOCAL_AGENT = "local-agent"
SERVE_MODES = (SERVE_OFF, SERVE_LOCAL_AGENT)

#: Estimated prompt tokens above which a conversation cannot continue locally.
#: Phase 1 of the probes: 13 of 14 sessions that reached ~25-27k tokens ended
#: wrong, because history compaction loses the task. Enforced through
#: ``local_context_guard.check_overflow`` (see ``over_prompt_cap``).
PROMPT_CAP_TOKENS = 25_000
#: ``num_ctx`` the mode requires of the resident model (the owner's 32k decision).
MIN_NUM_CTX = 32_768
#: In this mode a step is not given up on at 8 s / 30 s: the first call of a
#: conversation pays 14-25 s of prompt evaluation on a 12k-token prompt (route
#: A, n=438: first-token median 2.4 s, p90 15.9 s, max 25.6 s).
DEFAULT_STEP_BUDGET_S = 120.0

KILL_SWITCH_NAME = "local_agent_kill"

# Ledger reasons written by this mode (in addition to the existing ones).
REASON_KILL_SWITCH = "kill_switch"
REASON_PINNED_CLAUDE = "pinned_claude"
REASON_STARTED_ELSEWHERE = "conversation_started_on_claude"
REASON_MEDIA = "media_present"
REASON_PROMPT_CAP = "prompt_over_cap"
REASON_BREAKER_OPEN = "breaker_open"
REASON_BACKEND_UNHEALTHY = "backend_unhealthy"
REASON_NOT_ELIGIBLE = "not_eligible"

PIN_LOCAL = "local"
PIN_CLAUDE = "claude"

#: Reasons that make a conversation permanently Claude's (the local path
#: cannot ever take it back). Transient ones (kill switch, backend, breaker)
#: forward the step only.
STRUCTURAL_REASONS = frozenset({REASON_MEDIA, REASON_PROMPT_CAP})


def parse_serve_mode(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in ("", "0", "off", "false", "no"):
        return SERVE_OFF
    if value in (SERVE_LOCAL_AGENT, "local_agent", "localagent"):
        return SERVE_LOCAL_AGENT
    raise ValueError(f"expected one of {SERVE_MODES}, got {raw!r}")


def kill_switch_path() -> Path:
    from llm_router import paths

    return paths.state_path(KILL_SWITCH_NAME)


# ── eligibility ──────────────────────────────────────────────────────────────


def _blocks(content) -> list:
    return content if isinstance(content, list) else []


def has_media(body: dict) -> bool:
    """An image or document anywhere in the conversation, tool results included.

    The stock path replaces older-turn media with a text placeholder and the
    model then answers confidently about an image it never saw (route A F2:
    answered "6006", truth 7351). This mode never lets that happen."""
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        for b in _blocks(m.get("content")):
            if not isinstance(b, dict):
                continue
            if b.get("type") in ("image", "document"):
                return True
            if b.get("type") == "tool_result":
                if any(isinstance(x, dict) and x.get("type") in ("image", "document")
                       for x in _blocks(b.get("content"))):
                    return True
    return False


def is_agent_turn(body: dict) -> bool:
    """P1. A main-loop call this mode may serve: client tools present, no forced
    tool choice, and the newest non-system turn is the user's (a prompt or tool
    results). Media is judged by ``decide_local`` so it gets its own reason."""
    if not any(isinstance(t, dict) and t.get("name") and "input_schema" in t for t in body.get("tools") or []):
        return False
    tc = body.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") in ("tool", "any"):
        return False
    msgs = non_system(body.get("messages") or [])
    return bool(msgs) and msgs[-1].get("role") == "user"


def digit_aware_tokens(payload: dict) -> int:
    """Qwen tokenises every digit separately, so a chars/3.5 estimate undercounts
    digit-heavy text about 2x (``ollama-v1-silent-message-drop``, 2026-10-04:
    25,247 real tokens for a log the chars/4 estimate called 12k)."""
    text = json.dumps(payload, default=str, ensure_ascii=False)
    digits = sum(c.isdigit() for c in text)
    wide = sum(ord(c) > 127 for c in text)
    return math.ceil((digits + wide + (len(text) - digits - wide) / 3) * 1.05)


def estimate_prompt_tokens(payload: dict) -> int:
    return max(estimate_payload_tokens(payload), digit_aware_tokens(payload))


def over_prompt_cap(payload: dict, cap: int = PROMPT_CAP_TOKENS) -> int | None:
    """The estimate when ``payload`` is over ``cap``, else ``None``. The check
    itself is ``local_context_guard.check_overflow`` with the cap as the window,
    so one mechanism decides "too big" everywhere; the digit-aware estimate is
    added on top because the guard's own undercounts digits."""
    estimate = estimate_prompt_tokens(payload)
    try:
        check_overflow(payload, num_ctx=cap + DEFAULT_RESERVED_OUTPUT_TOKENS, site="proxy.local_mode")
    except ContextOverflow:
        return estimate
    return estimate if estimate > cap else None


@dataclass(frozen=True)
class LocalSession:
    """What ``decide_local`` knows besides the request: a snapshot, so the
    decision itself is a pure function (the resolver, redesign PR 9, replaces
    the rules but keeps this shape)."""

    key: str = ""
    pinned: str | None = None
    kill_switch: bool = False
    backend_healthy: bool = True
    breaker_closed: bool = True
    breaker_detail: str = ""
    payload: dict | None = None  # the Ollama payload, for the size check


@dataclass(frozen=True)
class LocalDecision:
    local: bool
    reason: str
    detail: str = ""
    est_tokens: int | None = None

    def as_row(self) -> dict:
        row = {"local": self.local, "reason": self.reason}
        if self.detail:
            row["detail"] = self.detail
        if self.est_tokens is not None:
            row["est_prompt_tokens"] = self.est_tokens
        return row


def decide_local(body: dict, session: LocalSession) -> LocalDecision:
    """Rule stub (redesign PR 1). May this call be served locally?

    Order matters: the kill switch beats everything; a conversation already on
    Claude stays there; then the input rules (media, prompt size), then the
    live state (backend, quality breaker)."""
    if session.kill_switch:
        return LocalDecision(False, REASON_KILL_SWITCH)
    if session.pinned == PIN_CLAUDE:
        return LocalDecision(False, REASON_PINNED_CLAUDE)
    if has_media(body):
        return LocalDecision(False, REASON_MEDIA, "image or document in the conversation")
    est = None
    if session.payload is not None:
        est = over_prompt_cap(session.payload)
        if est is not None:
            return LocalDecision(False, REASON_PROMPT_CAP, f"~{est} tokens > {PROMPT_CAP_TOKENS}", est)
        est = estimate_prompt_tokens(session.payload)
    if not session.backend_healthy:
        return LocalDecision(False, REASON_BACKEND_UNHEALTHY, est_tokens=est)
    if not session.breaker_closed:
        return LocalDecision(False, REASON_BREAKER_OPEN, session.breaker_detail, est)
    return LocalDecision(True, "local", est_tokens=est)


# ── per-process state ────────────────────────────────────────────────────────


class LocalModeState:
    """Conversation pins, the kill switch and the post-apply edit checks. One
    per proxy process: the proxy is one long-lived process, so memory is the
    whole truth (a restart forgets pins; a conversation seen again mid-way is
    then pinned to Claude, never moved local)."""

    def __init__(self, kill_path: Path | None = None, *, max_pins: int = 4096,
                 max_pending: int = 256) -> None:
        self.kill_path = kill_path if kill_path is not None else kill_switch_path()
        self.max_pins = max_pins
        self.max_pending = max_pending
        self.pins: OrderedDict[str, str] = OrderedDict()
        self.pending: OrderedDict[str, dict] = OrderedDict()

    def killed(self) -> bool:
        return self.kill_path.exists()

    def pin_of(self, key: str) -> str | None:
        return self.pins.get(key)

    def set_pin(self, key: str, pin: str) -> None:
        self.pins[key] = pin
        self.pins.move_to_end(key)
        while len(self.pins) > self.max_pins:
            self.pins.popitem(last=False)

    def remember_edits(self, message: dict) -> None:
        for b in message.get("content") or []:
            if b.get("type") == "tool_use" and b.get("name") in ("Edit", "Write", "MultiEdit"):
                self.pending[b["id"]] = {"name": b["name"], "input": b.get("input") or {}}
        while len(self.pending) > self.max_pending:
            self.pending.popitem(last=False)


# ── post-apply check of a served edit ────────────────────────────────────────


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    return " ".join(x.get("text", "") for x in _blocks(c) if isinstance(x, dict))


def check_applied(pending: dict, result_block: dict, cwd: str | None) -> str | None:
    """``None`` when the served edit is in place and the file still parses, else
    a short reason. Run at the step AFTER the edit, against the file on disk:
    that is the first moment the client has applied it."""
    from llm_router.edit import check_syntax

    if result_block.get("is_error"):
        return None  # the client rejected it; nothing was applied, the model sees the error
    inp = pending["input"]
    path = inp.get("file_path")
    if not isinstance(path, str) or not path:
        return "edit without a file_path"
    p = Path(path).expanduser()
    if not p.is_absolute() and cwd:
        p = Path(cwd) / p
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"{path} unreadable after the edit ({type(exc).__name__})"
    name = pending["name"]
    if name == "Write":
        if text != str(inp.get("content", "")):
            return f"{path} does not hold the content that was written"
    else:
        pairs = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
        for e in pairs:
            new = (e or {}).get("new_string") if isinstance(e, dict) else None
            if isinstance(new, str) and new and new not in text:
                return f"{path} does not contain the new text after the edit"
    err = check_syntax(str(p), text)
    return err


def annotate_failed_checks(body: dict, state: LocalModeState, cwd: str | None) -> tuple[dict, list[dict]]:
    """Run the pending post-apply checks against the tool results in ``body``;
    return a copy whose failing results carry the failure (so the local model
    can repair the file) and the check records for the ledger. ``body`` itself
    (what Anthropic would get on a fallback) is never modified."""
    records: list[dict] = []
    msgs = body.get("messages") or []
    out = None
    for mi, m in enumerate(msgs):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        for bi, b in enumerate(_blocks(m.get("content"))):
            if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                continue
            pend = state.pending.get(b.get("tool_use_id"))
            if pend is None:
                continue
            state.pending.pop(b["tool_use_id"], None)
            failure = check_applied(pend, b, cwd)
            records.append({"tool": pend["name"], "ok": failure is None,
                            **({"failure": failure[:160]} if failure else {})})
            if failure:
                if out is None:
                    import copy

                    out = copy.deepcopy(body)
                tr = out["messages"][mi]["content"][bi]
                note = f"[llm-router post-apply check FAILED: {failure}. Re-read the file and fix it.]"
                if isinstance(tr.get("content"), list):
                    tr["content"].append({"type": "text", "text": note})
                else:
                    tr["content"] = (tr.get("content") or "") + "\n" + note
    return (out if out is not None else body), records


# ── egress notice ────────────────────────────────────────────────────────────


def egress_notice(reason: str, session_id: str | None) -> str:
    sid = (session_id or "-")[:8]
    return (f"llm-router proxy [local-agent]: a step of session {sid} is being SENT TO ANTHROPIC "
            f"(reason: {reason}). Its prompt, files and tool output leave this machine.")


def banner_lines(*, model: str | None, num_ctx: int, kill_path: Path, killed: bool) -> list[str]:
    lines = [
        f"llm-router proxy: SERVE MODE = local-agent (opt-in). Claude Code steps are answered by "
        f"{model} on this machine; conversations are pinned local at their first call.",
        f"  egress: a step goes to Anthropic only when it is not eligible (reason in the ledger, notice on "
        f"stderr): media, prompt > {PROMPT_CAP_TOKENS} tokens, backend unhealthy, quality breaker open, a "
        f"failed local step, or a conversation that began on Claude. Never a placeholder, never silent.",
        f"  kill switch: touch {kill_path} (remove to re-enable); takes effect on the next request.",
    ]
    if killed:
        lines.append("  KILL SWITCH IS ENGAGED: every step is going to Anthropic until it is removed.")
    return lines


# ── startup preflight ────────────────────────────────────────────────────────

Runner = Callable[[list[str]], str]


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _port_of(url: str) -> tuple[str, int]:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)


_NP_RE = re.compile(r"(?:^|\s)-np\s+(\d+)\b")
_ENV_NP_RE = re.compile(r"(?:^|\s)OLLAMA_NUM_PARALLEL=(\d+)(?=\s|$)")


def server_num_parallel(base_url: str, runner: Runner = _run) -> tuple[int | None, str]:
    """``OLLAMA_NUM_PARALLEL`` of the ``ollama serve`` process listening at
    ``base_url`` (read from its environment with ``ps eww``), and a note. Only a
    loopback server can be checked; anything else is ``(None, why)``."""
    host, port = _port_of(base_url)
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None, f"{host} is not loopback: cannot read the server's environment"
    pids = runner(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]).split()
    if not pids:
        return None, f"no process found listening on port {port}"
    env = runner(["ps", "eww", "-o", "command=", "-p", pids[0]])
    m = _ENV_NP_RE.search(env)
    if m is None:
        return None, f"OLLAMA_NUM_PARALLEL not set in the environment of pid {pids[0]} (server default applies)"
    return int(m.group(1)), f"pid {pids[0]}"


def runner_np_values(runner: Runner = _run) -> list[int]:
    """``-np`` of every running ``llama-server`` (Ollama's runner processes)."""
    out = []
    for line in runner(["ps", "-axo", "command="]).splitlines():
        if "llama-server" in line:
            m = _NP_RE.search(line)
            if m:
                out.append(int(m.group(1)))
    return out


def overflow_guard_active(num_ctx: int) -> str | None:
    """``None`` when the local-context guard really refuses an oversized prompt
    on this proxy's backend, else the reason it does not. A self-test, not a
    flag read: it sends the guard a payload over the window and expects the
    refusal, and checks the backend calls the guard."""
    import inspect

    from llm_router.proxy import backends

    if "check_overflow" not in inspect.getsource(backends.OllamaBackend.complete):
        return "OllamaBackend.complete does not call the overflow guard"
    probe = {"model": "x", "messages": [{"role": "user", "content": "a" * (num_ctx * 5)}]}
    try:
        check_overflow(probe, num_ctx=num_ctx, site="proxy.local_mode.preflight")
    except ContextOverflow:
        return None
    return "the overflow guard did not refuse a payload larger than the window"


def writable(path: Path) -> str | None:
    """``None`` when ``path``'s directory accepts a write (created, then removed)."""
    probe = path.parent / f".preflight-{os.getpid()}-{path.name}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return f"{path.parent} is not writable ({type(exc).__name__}: {exc})"
    return None


def resident_context(base_url: str, model: str, get: Callable[[str], dict]) -> tuple[int | None, str]:
    """``(context_length, note)`` of ``model`` in the server's ``/api/ps``."""
    from llm_router.warm import model_matches

    try:
        data = get(base_url.rstrip("/") + "/api/ps")
    except Exception as exc:  # noqa: BLE001 - reported to the operator as a refusal
        return None, f"cannot reach {base_url} ({type(exc).__name__})"
    bare = model.split("/", 1)[1] if model.startswith("ollama/") else model
    for m in data.get("models") or []:
        name = str(m.get("name") or m.get("model") or "")
        if model_matches(bare, name):
            ctx = m.get("context_length")
            return (ctx if isinstance(ctx, int) else None), name
    return None, f"{bare} is not resident on {base_url}"


def preflight(*, model: str | None, trim: str | None, ollama_url: str, num_ctx: int, ledger_path: Path,
              kill_path: Path, tiers_off: bool, compaction_off: bool,
              get: Callable[[str], dict], runner: Runner = _run) -> list[str]:
    """Every reason local-agent mode must not start; empty means go. Each reason
    names what to change. ``llm-router proxy`` prints them and exits non-zero."""
    problems: list[str] = []
    if not model or not model.startswith("ollama/"):
        problems.append("--model ollama/<tag> is required (the policy is not consulted in this mode)")
    if (trim or "").strip().lower() != "none":
        problems.append(f"trim must be 'none' (got {trim!r}): the 'fast' trim swaps Claude Code's system "
                        f"prompt for 366 characters and keeps only 4 tools")
    if not tiers_off:
        problems.append("--tiers must be off in local-agent mode")
    if not compaction_off:
        problems.append("--local-agent / LLM_ROUTER_LOCAL_AGENT compaction must be off in local-agent mode "
                        "(history compaction loses the task, probes 2026-10-04)")
    if num_ctx < MIN_NUM_CTX:
        problems.append(f"--num-ctx {num_ctx} < {MIN_NUM_CTX}")
    if model and model.startswith("ollama/"):
        ctx, note = resident_context(ollama_url, model, get)
        if ctx is None:
            problems.append(f"model not resident with a known context: {note}")
        elif ctx < MIN_NUM_CTX:
            problems.append(f"{note} is resident with num_ctx {ctx} < {MIN_NUM_CTX}")
    np_server, note = server_num_parallel(ollama_url, runner)
    if np_server is None:
        problems.append(f"OLLAMA_NUM_PARALLEL=1 not verifiable: {note}")
    elif np_server != 1:
        problems.append(f"OLLAMA_NUM_PARALLEL={np_server} on the Ollama server ({note}); must be 1")
    bad_np = sorted({n for n in runner_np_values(runner) if n != 1})
    if bad_np:
        problems.append(f"a running llama-server has -np {bad_np[0]} (must be 1)")
    guard = overflow_guard_active(num_ctx)
    if guard:
        problems.append(f"overflow guard not active: {guard}")
    for what, path in (("ledger", ledger_path), ("kill switch", kill_path)):
        err = writable(path)
        if err:
            problems.append(f"{what} not writable: {err}")
    return problems
