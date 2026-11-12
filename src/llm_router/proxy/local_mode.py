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

``--local-with-tiers`` (NS-L, D-447) runs this mode together with ``--tiers``: a turn ``decide_local`` allows is
served as above, every other turn takes the unchanged tier path. ``decide_local`` also refuses an exempt session
(D-448): a coordinator session listed in ``session_kind_overrides.json`` or a sub-agent thread, as far as
``LLM_ROUTER_LOCAL_EXEMPT`` says (D-455; the default exempts both).

Never silent: every step that is not served locally carries a ledger reason
and prints an egress notice; a kill switch (a file) takes effect on the next
request without a restart; the startup banner states the mode; an input the
local path cannot handle (media, a prompt over the cap) is escalated
explicitly with a reason, never replaced by a placeholder.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from llm_router import local_models
from llm_router.local_context_guard import (
    DEFAULT_RESERVED_OUTPUT_TOKENS,
    effective_window,
    estimate_payload_tokens,
)
from llm_router.proxy.steps import non_system

ENV_MODE = "LLM_ROUTER_PROXY_LOCAL_AGENT_MODE"
SERVE_OFF = "off"
SERVE_LOCAL_AGENT = "local-agent"
SERVE_MODES = (SERVE_OFF, SERVE_LOCAL_AGENT)

#: Estimated prompt tokens above which a conversation cannot continue locally.
#: Phase 1 of the probes: 13 of 14 sessions that reached ~25-27k tokens ended
#: wrong, because history compaction loses the task. Checked by ``over_prompt_cap``
#: with a calibrated estimate; ``local_context_guard`` stays the 32k window backstop
#: inside the backend (see the estimator note below for why it cannot decide this one).
PROMPT_CAP_TOKENS = 25_000


#: Share of the free window the raised cap admits (only used with LLM_ROUTER_WINDOW_STEP).
CAP_SAFETY_FACTOR = 0.9


def prompt_cap_tokens() -> int:
    """The cap in force: ``PROMPT_CAP_TOKENS`` unless ``LLM_ROUTER_WINDOW_STEP`` raises the
    window, then 0.9 x (window - reply tokens - reserved prompt overhead - the OKF block). The 0.9 keeps the
    margin the 25k cap has against the estimator (``estimate_prompt_tokens`` can under-count real
    tokens), because Ollama silently drops the head of an overflowing prompt."""
    window = local_models.raised_window()
    if window is None:
        return PROMPT_CAP_TOKENS
    from llm_router.local_context_guard import DEFAULT_RESERVED_OUTPUT_TOKENS as reserved
    from llm_router.proxy.okf_context import MAX_BLOCK_TOKENS
    return int(CAP_SAFETY_FACTOR * (window - LOCAL_NUM_PREDICT - reserved - MAX_BLOCK_TOKENS))


#: ``num_ctx`` the mode requires of the resident model (the owner's 32k decision).
MIN_NUM_CTX = 32_768
#: In this mode a step is not given up on at 8 s / 30 s: the first call of a
#: conversation pays 14-25 s of prompt evaluation on a 12k-token prompt (route
#: A, n=438: first-token median 2.4 s, p90 15.9 s, max 25.6 s).
DEFAULT_STEP_BUDGET_S = 120.0

#: Output cap per local step. The stock 200 (after Read/Bash) and 700 caps were sized for
#: the trimmed request; with the full prompt, 3 of 3 ``triple_read`` soak sessions had their
#: first reply (three parallel Read calls) rejected as "truncated at num_predict" at 200.
LOCAL_NUM_PREDICT = 2048

KILL_SWITCH_NAME = "local_agent_kill"

# Ledger reasons written by this mode (in addition to the existing ones).
REASON_KILL_SWITCH = "kill_switch"
REASON_PINNED_CLAUDE = "pinned_claude"
REASON_CLAUDE_MARKER = "claude_marker"  # PR-A: the conversation's first user message carries CLAUDE_MARKER
REASON_STARTED_ELSEWHERE = "conversation_started_on_claude"
REASON_MEDIA = "media_present"
REASON_PROMPT_CAP = "prompt_over_cap"
#: LLM_ROUTER_CAP_GUARD_RECONCILE: under the cap, but the backend's window guard would refuse it.
REASON_PROMPT_GUARD = "prompt_over_guard"
REASON_PROMPT_UNMEASURABLE = "prompt_unmeasurable"  # ESTREC-1: nested past the recursion limit; no token count exists
REASON_BREAKER_OPEN = "breaker_open"
REASON_BACKEND_UNHEALTHY = "backend_unhealthy"
REASON_NOT_ELIGIBLE = "not_eligible"
#: NS-A2: the per-turn classifier vetoed this turn (detail: veto_tier / veto_lane). The turn goes to
#: Claude; the conversation pin is NOT touched.
REASON_CLASSIFIER_VETO = "classifier_veto"
#: NS-L (D-448): the session is a coordinator / non-organic session listed in
#: ``session_kind_overrides.json`` (detail ``coordinator_session``), or the call comes from a sub-agent
#: thread (detail ``subagent_thread``). Never served locally; the pin goes to Claude like any refusal.
COMBINED_FIRST_TOKEN_S = 30.0  # combined mode default first-token deadline (--hedge-s overrides)
REASON_EXEMPT_SESSION = "exempt_session"
REASON_COLD_OVER_DEADLINE = "cold_over_deadline"  # P22: this turn only, never pins
REASON_PREFLIGHT_REFUSED = "preflight_refused"  # combined mode only: local serving off for the process lifetime
EXEMPT_COORDINATOR = "coordinator_session"
EXEMPT_SUBAGENT = "subagent_thread"
EXEMPT_COORDINATOR_MAIN = "exempt_coordinator_main"  # PR-D: a coordinator session's main thread only

#: D-455: which exempt classes refuse local serving. Read per request from this variable.
EXEMPT_POLICY_ENV = "LLM_ROUTER_LOCAL_EXEMPT"
EXEMPT_POLICY_DEFAULT = "coordinator,subagent"


def exempt_policy(raw: str | None = None) -> tuple[str, bool, bool, str | None]:
    """``(effective, coordinator_exempt, subagent_exempt, invalid)`` for ``raw`` (default: the env var).

    Case-insensitive, whitespace tolerated. Unset or empty is the default (both exempt). A value that is
    not ``none`` or a comma list of ``coordinator`` / ``coordinator_main`` / ``subagent`` falls back to the
    default and is returned in ``invalid`` so the row can record it. ``coordinator_main`` (PR-D) exempts only
    the main thread of a coordinator session; it cannot be combined with ``coordinator`` (that would be
    ambiguous: invalid). The tuple shape is unchanged: ``coordinator_exempt`` is True only for the whole-session
    ``coordinator`` token; ``coordinator_main`` shows in ``effective`` (``coordinator_main_exempt(effective)``).
    KPI classification does not read this."""
    if raw is None:
        raw = os.environ.get(EXEMPT_POLICY_ENV)
    text = (raw or "").strip().lower()
    if not text:
        return EXEMPT_POLICY_DEFAULT, True, True, None
    tokens = [t.strip() for t in text.split(",")]
    if tokens == ["none"]:
        return "none", False, False, None
    if (tokens and all(t in ("coordinator", "coordinator_main", "subagent") for t in tokens)
            and not ("coordinator" in tokens and "coordinator_main" in tokens)):
        coord, main, sub = "coordinator" in tokens, "coordinator_main" in tokens, "subagent" in tokens
        return (",".join(n for n, on in (("coordinator", coord), ("coordinator_main", main), ("subagent", sub)) if on),
                coord, sub, None)
    return EXEMPT_POLICY_DEFAULT, True, True, (raw or "").strip()[:80]


def coordinator_main_exempt(effective: str) -> bool:
    """True when ``exempt_policy()[0]`` holds the ``coordinator_main`` token (PR-D)."""
    return "coordinator_main" in effective.split(",")


PIN_LOCAL = "local"
PIN_CLAUDE = "claude"

#: Reasons that make a conversation permanently Claude's (the local path
#: cannot ever take it back). Transient ones (kill switch, backend, breaker)
#: forward the step only for a conversation already pinned local. A NEW
#: conversation (no pin yet) refused for ANY reason except a veto is pinned
#: to Claude, because its prompt cache starts there (proxy/server.py,
#: ``pinned is None`` branch of the refusal path).
STRUCTURAL_REASONS = frozenset({REASON_MEDIA, REASON_PROMPT_CAP, REASON_PROMPT_GUARD})


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


# Estimator: ``token_estimate.est_tokens`` (constants, calibration and evidence live there).
# ``local_context_guard``'s chars/3.04 was 1.4x high on JSON tool schemas (it would refuse the
# 18.5k-token MCP body at ~26k) and 0.90x LOW on a 450-line log, so it cannot decide the cap;
# it stays the hard window backstop inside the backend.
from llm_router.token_estimate import est_tokens  # noqa: E402  (one formula, shared)


#: PR-A: with ``LLM_ROUTER_CLAUDE_MARKER`` on, a conversation whose FIRST user message contains this exact token
#: is never served locally (reason ``claude_marker``). Per conversation: only that message is looked at.
CLAUDE_MARKER = "[[llm-router:claude-only]]"


def claude_marker_on() -> bool:
    return os.environ.get("LLM_ROUTER_CLAUDE_MARKER", "").strip().lower() in ("1", "true", "yes", "on")


def has_claude_marker(body: dict) -> bool:
    """True when the first user message (string or text blocks) contains :data:`CLAUDE_MARKER`."""
    for m in body.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return CLAUDE_MARKER in c
        return any(isinstance(b, dict) and b.get("type") == "text" and CLAUDE_MARKER in (b.get("text") or "")
                   for b in _blocks(c))
    return False


def estimate_prompt_tokens(payload: dict) -> int:
    return est_tokens(payload)


def over_prompt_cap(payload: dict, cap: int | None = None) -> int | None:
    """The estimate when ``payload`` is over ``cap`` (default :func:`prompt_cap_tokens`), else ``None``."""
    if cap is None:
        cap = prompt_cap_tokens()
    estimate = estimate_prompt_tokens(payload)
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
    #: NS-A2: the classifier's veto for THIS turn (``cls_decide.veto_of``: "veto_tier" /
    #: "veto_lane"), or None. Per turn, never stored in the pin.
    verdict: str | None = None
    #: NS-L (D-448): ``exempt_reason(...)``'s detail (coordinator_session / subagent_thread), or None.
    exempt: str | None = None
    #: PR-B (LLM_ROUTER_CLASSIFIER_AUTHORITY=positive): a decided ``local`` verdict waives the conversation pin
    #: (the one SOFT rule) for this turn. Every other rule still applies. Default False: unchanged.
    cls_local: bool = False


def exempt_reason(body: dict, session_id: str | None,
                  parent_main: bool | None = None,
                  policy: tuple[str, bool, bool, str | None] | None = None) -> tuple[str | None, str | None]:
    """``(detail, overrides_problem)`` for D-448. ``detail`` is ``coordinator_session`` when the
    session is listed in ``session_kind_overrides.json`` with a kind other than ``organic`` (the same
    reader the KPI code uses: ``session_kind.override_of``), else ``subagent_thread`` when the request
    carries the proxy's own sub-agent evidence (``steps.is_main_thread`` is False, i.e. no Agent launcher or ``steps.is_subagent_request``: the sub-agent system-prompt
    markers Claude Code appends to every spawned agent, or a sub-agent-only tool), else None.

    ``overrides_problem`` is ``missing`` / ``unreadable`` when the overrides file could not be read as an
    object. That case FAILS CLOSED (coordinator decision): every session is refused with ``exempt_session``,
    detail ``missing`` or ``overrides_unreadable``, and the row records ``local_mode.overrides_unreadable``.
    Nothing is served locally while the file cannot be read; organic sessions are unaffected while it can.

    D-455: ``policy`` (``exempt_policy()``, read from ``LLM_ROUTER_LOCAL_EXEMPT`` when None) switches each
    class off. PR-D: the ``coordinator_main`` token returns ``EXEMPT_COORDINATOR_MAIN`` for a coordinator
    session's non-sub-agent call; its sub-agent calls fall to the ``subagent`` token (served locally without it).
    The overrides file is read, and the fail-closed rule applies, only while coordinators are
    exempt (``coordinator`` or ``coordinator_main``); with ``subagent`` or ``none`` an unreadable file refuses nothing."""
    from llm_router import session_kind
    from llm_router.proxy.steps import is_main_thread, is_subagent_request

    effective, coord_exempt, sub_exempt, _ = policy if policy is not None else exempt_policy()
    main_exempt = coordinator_main_exempt(effective)  # PR-D
    problem = None
    if coord_exempt or main_exempt:
        try:
            problem = session_kind.overrides_problem()
            kind = session_kind.override_of(session_id)
        except Exception:  # noqa: BLE001 - never costs a call; recorded as unreadable
            kind, problem = None, "unreadable"
        if problem:
            # Coordinator decision (fail closed): an overrides file that cannot be read means no session's
            # status can be established, so NO session is served locally.
            return (problem if problem == "missing" else "overrides_unreadable"), problem
        if kind is not None and kind != session_kind.KIND_ORGANIC:
            if coord_exempt:
                return EXEMPT_COORDINATOR, problem
            # PR-D: coordinator_main exempts the session's MAIN thread only. Same evidence as the sub-agent
            # branch below; an unknown thread (None) counts as main and stays exempt (fail closed toward Claude).
            # Known leak (accepted, fails open toward local): a main thread with no Agent/Task launcher in its tools,
            # or a sub-agent marker in its system prompt, reads as not-main (steps.is_main_thread) and is NOT exempt.
            try:
                is_sub = is_subagent_request(body) or is_main_thread(body, parent_main) is False
            except Exception:  # noqa: BLE001 - unreadable evidence: treat as main, stay on Claude
                is_sub = False
            if not is_sub:
                return EXEMPT_COORDINATOR_MAIN, problem
    if not sub_exempt:
        return None, problem
    try:
        if is_subagent_request(body):
            return EXEMPT_SUBAGENT, problem
        # rv40: the proxy's own thread evidence (the signal behind the row's is_main_thread and
        # turn_origin=subagent_brief) says this is not the main thread. ``None`` (unknown) is not exempt.
        if is_main_thread(body, parent_main) is False:
            return EXEMPT_SUBAGENT, problem
    except Exception:  # noqa: BLE001
        pass
    return None, problem


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

    Order matters: the kill switch beats everything; an exempt session (D-448)
    is not served (as far as ``LLM_ROUTER_LOCAL_EXEMPT`` exempts that class, D-455); a conversation already on Claude stays there; then the input rules (media, prompt size), then the
    live state (backend, quality breaker)."""
    if session.kill_switch:
        return LocalDecision(False, REASON_KILL_SWITCH)
    if claude_marker_on() and has_claude_marker(body):
        return LocalDecision(False, REASON_CLAUDE_MARKER)
    if session.exempt:
        return LocalDecision(False, REASON_EXEMPT_SESSION, session.exempt)
    if session.pinned == PIN_CLAUDE and not session.cls_local:
        return LocalDecision(False, REASON_PINNED_CLAUDE)
    if has_media(body):
        return LocalDecision(False, REASON_MEDIA, "image or document in the conversation")
    est = None
    if session.payload is not None:
        est = over_prompt_cap(session.payload)
        if est is not None:
            return LocalDecision(False, REASON_PROMPT_CAP, f"~{est} tokens > {prompt_cap_tokens()}", est)
        est = estimate_prompt_tokens(session.payload)
    if session.verdict:
        return LocalDecision(False, REASON_CLASSIFIER_VETO, session.verdict, est)
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
                 max_pending: int = 256, disabled: str | None = None) -> None:
        self.disabled = disabled  # preflight refusals joined; set => every turn takes the tier path
        self.kill_path = kill_path if kill_path is not None else kill_switch_path()
        self.max_pins = max_pins
        self.max_pending = max_pending
        self.pins: OrderedDict[str, str] = OrderedDict()
        # P7: why a claude pin was stored, only for structural reasons (prompt_over_cap / media_present).
        # ``unpinned_once``: sessions whose prompt_over_cap pin was lifted; never lifted a second time.
        # Memory only, like the pins: a restart forgets both (a conversation seen again mid-way is then
        # pinned to Claude by ``started_elsewhere``, so no marker is needed across a restart).
        self.pin_reasons: dict[str, str] = {}
        self.unpinned_once: OrderedDict[str, bool] = OrderedDict()
        self.pending: OrderedDict[str, dict] = OrderedDict()
        # NS-A2: conversations whose first turn this process saw and the classifier vetoed.
        # A veto stores no pin, so without this the next call would read ``started_elsewhere``.
        self.seen_keys: OrderedDict[str, bool] = OrderedDict()

    def mark_seen(self, key: str) -> None:
        self.seen_keys[key] = True
        self.seen_keys.move_to_end(key)
        while len(self.seen_keys) > self.max_pins:
            self.seen_keys.popitem(last=False)

    def seen(self, key: str) -> bool:
        return key in self.seen_keys

    def killed(self) -> bool:
        return self.kill_path.exists()

    def pin_of(self, key: str) -> str | None:
        return self.pins.get(key)

    def set_pin(self, key: str, pin: str, reason: str | None = None) -> None:
        self.pins[key] = pin
        self.pins.move_to_end(key)
        if reason is not None:
            self.pin_reasons[key] = reason
        else:
            self.pin_reasons.pop(key, None)
        while len(self.pins) > self.max_pins:
            old, _ = self.pins.popitem(last=False)
            self.pin_reasons.pop(old, None)

    def pin_reason_of(self, key: str) -> str | None:
        return self.pin_reasons.get(key)

    def unpin_over_cap(self, key: str) -> bool:
        """P7: lift a ``prompt_over_cap`` claude pin, at most once per session. False (nothing changed)
        for any other pin, no pin, or a session already unpinned once."""
        if (self.pins.get(key) != PIN_CLAUDE or self.pin_reasons.get(key) != REASON_PROMPT_CAP
                or key in self.unpinned_once):
            return False
        del self.pins[key]
        self.pin_reasons.pop(key, None)
        self.unpinned_once[key] = True
        while len(self.unpinned_once) > self.max_pins:
            self.unpinned_once.popitem(last=False)
        self.mark_seen(key)
        return True

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


def check_applied(pending: dict, result_block: dict, cwd: str | None = None) -> str | None:
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
    if not p.is_absolute():
        # Claude Code's Write/Edit take absolute paths. A relative one is the
        # local model dropping the leading "/" ("private/tmp/..."): the client
        # resolves it under the working directory and reports success, so the
        # file exists but not where the model meant (route A write probe, 2026-10-05).
        return (f"file_path {path!r} is not absolute; it was resolved under the working directory, "
                f"which is probably not the file you meant. Use the full absolute path")
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


def banner_lines(*, model: str | None, num_ctx: int | None, kill_path: Path, killed: bool,
                 with_tiers: bool = False) -> list[str]:
    num_ctx = resolve_num_ctx(model, num_ctx)
    effective, _, _, invalid = exempt_policy()
    lines = [
        f"llm-router proxy: SERVE MODE = local-agent (opt-in). Claude Code steps are answered by "
        f"{model} on this machine; conversations are pinned local at their first call.",
        f"  egress: a step goes to Anthropic only when it is not eligible (reason in the ledger, notice on "
        f"stderr): media, prompt > {prompt_cap_tokens()} tokens, backend unhealthy, quality breaker open, a "
        f"failed local step, a conversation that began on Claude, or an exempt session (coordinator in "
        f"session_kind_overrides.json, or a sub-agent thread; which of the two is set by {EXEMPT_POLICY_ENV}, "
        f"default both, see the line below). Never a placeholder, never silent.",
        f"  exempt policy ({EXEMPT_POLICY_ENV}): {effective}"
        + (f" (invalid value {invalid!r}, default applied)" if invalid else "")
        + "; a class not listed is served locally.",
        f"  kill switch: touch {kill_path} (remove to re-enable); takes effect on the next request.",
    ]
    if with_tiers:
        lines.append("  COMBINED with --tiers (--local-with-tiers): a step that is not served locally follows the "
                     "Claude-tier policy exactly as under --tiers alone (it may be rewritten to a cheaper Claude tier).")
    if killed:
        lines.append("  KILL SWITCH IS ENGAGED: every step is going to Anthropic until it is removed.")
    return lines


# ── startup preflight ────────────────────────────────────────────────────────

Runner = Callable[[list[str]], str]


def _run(cmd: list[str]) -> str:
    from llm_router.safe_subprocess import get_delegated_env

    try:
        # Fixed argv (lsof / ps), but it still gets the allowlisted environment:
        # nothing here needs a credential, and the R4 scan counts every site
        # that inherits the whole environment.
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5, env=get_delegated_env()).stdout
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


def resolve_num_ctx(model: str | None, num_ctx: int | None) -> int:
    """An explicit window wins; None (no --num-ctx, no env) is the table value
    for *model* (M3.4). ``ProxyConfig.num_ctx`` stays None when no model is
    pinned, and the startup checks run before that is refused."""
    return num_ctx if num_ctx is not None else local_models.num_ctx(model)


def overflow_guard_active(num_ctx: int) -> str | None:
    """``None`` when the local-context guard really refuses an oversized prompt
    on this proxy's backend, else the reason it does not. A self-test, not a
    flag read: it sends the guard a payload over the window and expects the
    refusal, and checks the backend calls the guard."""
    import inspect

    from llm_router.proxy import backends

    if "check_overflow" not in inspect.getsource(backends.OllamaBackend.complete):
        return "OllamaBackend.complete does not call the overflow guard"
    # The same two numbers check_overflow compares, without calling it: a call
    # that refuses records a fail-open event, and a startup self-test must not
    # leave a phantom overflow in the operator's counters.
    probe = {"model": "x", "messages": [{"role": "user", "content": "a" * (num_ctx * 5)}]}
    window, _source = effective_window(num_ctx=num_ctx)
    if estimate_payload_tokens(probe) <= window - DEFAULT_RESERVED_OUTPUT_TOKENS:
        return "the overflow guard's estimate did not exceed the window for a payload larger than it"
    return None


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


def preflight(*, model: str | None, trim: str | None, ollama_url: str, num_ctx: int | None, ledger_path: Path,
              kill_path: Path, tiers_off: bool, compaction_off: bool,
              get: Callable[[str], dict], runner: Runner = _run, with_tiers: bool = False) -> list[str]:
    """Every reason local-agent mode must not start; empty means go. Each reason
    names what to change. ``llm-router proxy`` prints them and exits non-zero."""
    num_ctx = resolve_num_ctx(model, num_ctx)
    problems: list[str] = []
    if not model or not model.startswith("ollama/"):
        problems.append("--model ollama/<tag> is required (the policy is not consulted in this mode)")
    if (trim or "").strip().lower() != "none":
        problems.append(f"trim must be 'none' (got {trim!r}): the 'fast' trim swaps Claude Code's system "
                        f"prompt for 366 characters and keeps only 4 tools")
    if with_tiers:
        # NS-L combined mode (--local-with-tiers): the tier policy serves every turn that is not local,
        # so tiers must be ON; this is the only place the tiers-off requirement is dropped.
        if tiers_off:
            problems.append("--local-with-tiers needs --tiers on or conversation (there is no tier path to fall to)")
    elif not tiers_off:
        problems.append("--tiers must be off in local-agent mode (or pass --local-with-tiers to combine them)")
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
