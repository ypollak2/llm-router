"""The proxy app: pass-through to Anthropic, per-step serving, fallback.

Enable for ONE Claude Code session, never globally::

    llm-router proxy --port 8787            # terminal 1
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ENABLE_TOOL_SEARCH=true claude    # terminal 2

Auth: the client's ``authorization`` / ``x-api-key`` headers are forwarded to
Anthropic unchanged and are never written anywhere. ``accept-encoding`` is
forced to ``identity`` upstream: httpx otherwise asks for gzip, and relaying
the raw gzip bytes without their header broke Claude Code with "JSON Parse
error" in the spike.

``ENABLE_TOOL_SEARCH=true`` matters as much as ``ANTHROPIC_BASE_URL`` and is
not optional polish. Claude Code disables its Tool Search / dynamic-tool-
loading feature the moment ``ANTHROPIC_BASE_URL`` is not a first-party
Anthropic host — it cannot tell that THIS proxy forwards every ``/v1/messages``
body byte-for-byte (see ``forward()`` below). With Tool Search off, Claude
Code inlines every deferred MCP/skill tool schema into every request instead
of the handful the step actually needs, which enlarges the cacheable prefix
and, on a cache miss, is billed at the cache-write rate (2x input for the 1h
TTL Claude Code uses). Measured 2026-09-28 on 3 fixture tasks: proxied
sessions cost 3.5x a same-task baseline with no proxy, driven almost entirely
by this (docs/proxy.md "Cost parity"). Because ``forward()`` never inspects or
rewrites the tools array, it forwards the resulting ``tool_reference`` blocks
unchanged, so setting ``ENABLE_TOOL_SEARCH=true`` is safe here and restores
first-party token usage.

Safety rules, each enforced here:
  * a served reply must pass ``translate.from_ollama`` validation;
  * a backend error, a validation failure, or a miss of the per-step latency
    budget falls back to Anthropic with the original request, and the ledger
    row says why;
  * served replies are never cached (there is no cache on this path at all);
  * a request whose history holds a proxy-served turn is forwarded unchanged;
    if Anthropic rejects it over thinking blocks, it is retried once with
    thinking (and ``clear_thinking`` context edits) off.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from llm_router.local_agent import DEFAULT_MAX_PROMPT_TOKENS, LocalAgentConfig, enabled_from_env
from llm_router.local_agent import capability as la_capability
from llm_router import failopen
from llm_router.proxy import ledger, okf_context

from llm_router.proxy.backend_health import (
    DEFAULT_COOLDOWN_S,
    DEFAULT_FAIL_N,
    REASON_BACKEND_UNHEALTHY,
    BackendHealth,
)
from llm_router.proxy.backends import (
    BACKENDS,
    DEFAULT_HEDGE_S,
    DEFAULT_KEEP_ALIVE,
    HedgeTimeout,
    apply_trims,
    choose_model,
    policy_chain,
    resolve_trims,
    tool_capable,
)
from llm_router.local_context_guard import ContextOverflow
from llm_router.proxy.loop_guard import (
    DEFAULT_MAX_CONSECUTIVE,
    DEFAULT_REPEAT_WINDOW,
    REASON_LOOP_GUARD,
    LoopGuard,
)
from llm_router.proxy.cache_cost import Stickiness, conversation_key
from llm_router.proxy.steps import STEP_CLASSES, classify_text, prev_tools, session_id_of, step_class
from llm_router import session_kind
from llm_router.proxy import cost_accounting
from llm_router.proxy.tiers import REASON_DECISION_ERROR, REWRITE_HAIKU, ClaudeTierPolicy
from llm_router.proxy.translate import (
    for_haiku,
    has_served_turn,
    is_thinking_rejection,
    parse_sse_usage,
    sse_from_message,
    sse_response_model,
    without_thinking,
)

#: Upper bound on attaching repo knowledge to a local step (several git calls).
OKF_ATTACH_TIMEOUT_S = 2.0


ANTHROPIC_UPSTREAM = "https://api.anthropic.com"
DEFAULT_PORT = 8787
DEFAULT_STEP_BUDGET_S = 30.0
DEFAULT_NUM_CTX = 32768
_HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding",
        "keep-alive", "proxy-authorization", "te", "trailer", "upgrade"}

# ``--tiers`` / ``LLM_ROUTER_PROXY_TIERS`` modes.
#   off           the rewrite never runs.
#   on            per-turn (PR #215): the classifier decides on every eligible
#                 call; the conversation's first call is exempt (Key finding
#                 1.2: a mid-task switch re-writes the whole prompt cache, and
#                 Opus 5.5 / Sonnet 5.5 read cache at the same rate, so a
#                 switch on a short task loses money).
#   conversation  Phase 1.2b: the first call IS classified, and its tier holds
#                 for the whole conversation via stickiness (escalating only
#                 at a cold point or a clear rise in complexity).
TIERS_OFF = "off"
TIERS_ON = "on"
TIERS_CONVERSATION = "conversation"
TIER_MODES = (TIERS_OFF, TIERS_ON, TIERS_CONVERSATION)


def validate_upstream(url: str) -> str:
    """Only Anthropic itself or a loopback test double may receive the client's
    credentials. Anything else is refused at startup."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname == "api.anthropic.com":
        return url.rstrip("/")
    if parts.scheme in ("http", "https") and parts.hostname in ("127.0.0.1", "localhost", "::1"):
        return url.rstrip("/")
    raise ValueError(f"refusing upstream {url!r}: only {ANTHROPIC_UPSTREAM} or a loopback address")


@dataclass
class ProxyConfig:
    steps: frozenset = field(default_factory=lambda: frozenset({"continuation"}))
    step_budget_s: float = DEFAULT_STEP_BUDGET_S
    model: str | None = None
    trim: str | None = None
    num_ctx: int = DEFAULT_NUM_CTX
    upstream: str = ANTHROPIC_UPSTREAM
    ollama_url: str | None = None
    ledger_path: Path | None = None
    hedge_s: float | None = DEFAULT_HEDGE_S
    keep_alive: int | str | None = DEFAULT_KEEP_ALIVE
    warm_up: bool = True
    loop_max_consecutive: int = DEFAULT_MAX_CONSECUTIVE
    loop_repeat_window: int = DEFAULT_REPEAT_WINDOW
    tiers: str = TIERS_OFF
    tier_policy: str | None = None
    # Capability gating + compaction (llm_router.local_agent); None = off.
    local_agent: LocalAgentConfig | None = None
    backend_fail_n: int = DEFAULT_FAIL_N
    backend_cooldown_s: float = DEFAULT_COOLDOWN_S

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        steps_raw = os.environ.get("LLM_ROUTER_PROXY_STEPS", "continuation")
        return cls(
            steps=parse_steps(steps_raw),
            step_budget_s=float(os.environ.get("LLM_ROUTER_PROXY_STEP_BUDGET_S", DEFAULT_STEP_BUDGET_S)),
            model=os.environ.get("LLM_ROUTER_PROXY_MODEL") or None,
            trim=os.environ.get("LLM_ROUTER_PROXY_TRIM") or None,
            num_ctx=int(os.environ.get("LLM_ROUTER_PROXY_NUM_CTX", DEFAULT_NUM_CTX)),
            upstream=os.environ.get("LLM_ROUTER_PROXY_UPSTREAM") or ANTHROPIC_UPSTREAM,
            hedge_s=parse_hedge(os.environ.get("LLM_ROUTER_PROXY_HEDGE_S", str(DEFAULT_HEDGE_S))),
            loop_max_consecutive=int(os.environ.get("LLM_ROUTER_PROXY_LOOP_MAX_CONSECUTIVE",
                                                      DEFAULT_MAX_CONSECUTIVE)),
            loop_repeat_window=int(os.environ.get("LLM_ROUTER_PROXY_LOOP_REPEAT_WINDOW",
                                                    DEFAULT_REPEAT_WINDOW)),
            tiers=parse_tiers_mode(os.environ.get("LLM_ROUTER_PROXY_TIERS", "off")),
            tier_policy=os.environ.get("LLM_ROUTER_PROXY_TIER_POLICY") or None,
            local_agent=LocalAgentConfig.from_env() if enabled_from_env() else None,
            backend_fail_n=int(os.environ.get("LLM_ROUTER_PROXY_BACKEND_FAIL_N", DEFAULT_FAIL_N)),
            backend_cooldown_s=float(os.environ.get("LLM_ROUTER_PROXY_BACKEND_COOLDOWN_S",
                                                    DEFAULT_COOLDOWN_S)),
        )


def parse_on_off(raw: str) -> bool:
    value = str(raw or "").strip().lower()
    if value in ("1", "on", "true", "yes"):
        return True
    if value in ("", "0", "off", "false", "no"):
        return False
    raise ValueError(f"expected on/off, got {raw!r}")


def parse_tiers_mode(raw: str) -> str:
    """``off`` / ``on`` (per-turn) / ``conversation`` (Phase 1.2b). Accepts the
    old boolean spellings for ``off``/``on`` so a bare ``LLM_ROUTER_PROXY_TIERS=1``
    from before this flag had a third value keeps meaning per-turn."""
    value = str(raw or "").strip().lower()
    if value in ("", "0", "off", "false", "no"):
        return TIERS_OFF
    if value in ("1", "on", "true", "yes"):
        return TIERS_ON
    if value == TIERS_CONVERSATION:
        return TIERS_CONVERSATION
    raise ValueError(f"expected one of {TIER_MODES}, got {raw!r}")


def parse_hedge(raw: str) -> float | None:
    """Seconds to the first token before falling back; ``0``/``off`` disables."""
    if str(raw).strip().lower() in ("0", "off", "none", ""):
        return None
    return float(raw)


def parse_keep_alive(raw: str) -> int | str:
    """Ollama rejects a bare numeric STRING, so integers go as JSON numbers."""
    try:
        return int(raw)
    except ValueError:
        return raw


def parse_steps(raw: str) -> frozenset:
    names = {s.strip() for s in (raw or "").split(",") if s.strip()}
    if names <= {"off", "none"}:
        return frozenset()
    unknown = names - set(STEP_CLASSES)
    if unknown:
        raise ValueError(f"unknown step class(es) {sorted(unknown)}; known: {sorted(STEP_CLASSES)}")
    return frozenset(names)


def _error_json(message: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}).encode()


def build_app(cfg: ProxyConfig, *, client=None, backend_factory=None, health_clock=None):
    """The Starlette app. ``client`` (an ``httpx.AsyncClient``),
    ``backend_factory(model) -> Backend`` and the backend-health breaker's
    ``health_clock`` are injectable for tests."""
    import httpx
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response, StreamingResponse
    from starlette.routing import Route

    upstream = validate_upstream(cfg.upstream)
    trims = resolve_trims(cfg.trim)
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    guard = LoopGuard(cfg.loop_max_consecutive, cfg.loop_repeat_window)
    # Stops sending steps to a crashed local backend (proxy.backend_health).
    health = BackendHealth(cfg.backend_fail_n, cfg.backend_cooldown_s, clock=health_clock)
    # Claude-tier rewrite (opt-in). A bad policy file fails here, at startup,
    # never per call.
    tier_policy = (ClaudeTierPolicy.load(cfg.tier_policy, conversation_level=(cfg.tiers == TIERS_CONVERSATION))
                   if cfg.tiers != TIERS_OFF else None)
    sticky = Stickiness(tier_policy.cold_gap_s) if tier_policy is not None else None

    def _ollama_url() -> str:
        from llm_router.config import get_config, validate_ollama_url

        if cfg.ollama_url:
            return validate_ollama_url(cfg.ollama_url)
        return get_config().effective_ollama_base_url or "http://localhost:11434"

    def make_backend(model: str):
        if backend_factory is not None:
            return backend_factory(model)
        for prefix, cls in BACKENDS.items():
            if model.startswith(prefix):
                return cls(model, http, base_url=_ollama_url(), num_ctx=cfg.num_ctx,
                           hedge_s=cfg.hedge_s, keep_alive=cfg.keep_alive)
        raise ValueError(f"no backend for {model}")

    async def warm_up() -> dict:
        """One throwaway call so the first real step does not pay the cold
        start (8-110 s in the local-speed spike). Never raises."""
        t0 = time.monotonic()
        try:
            model = cfg.model
            if not model:
                _task, _cx, chain = await policy_chain("continue the coding task after a tool result")
                model = next((m for m in chain if tool_capable(m)), None)
            if not model:
                return {"warm_up": "skipped", "reason": "no tool-capable model in chain"}
            backend = make_backend(model)
            seconds = await backend.warm_up() if hasattr(backend, "warm_up") else 0.0
            return {"warm_up": "ok", "model": model, "seconds": seconds}
        except Exception as exc:  # noqa: BLE001 - a failed warm-up only costs latency
            return {"warm_up": "failed", "error": ledger.scrub_detail(f"{type(exc).__name__}: {exc}"),
                    "seconds": round(time.monotonic() - t0, 2)}

    local_agent = None
    if cfg.local_agent is not None:
        from llm_router.local_agent.proxy_step import LocalAgent

        local_agent = LocalAgent(cfg.local_agent, http=http, ollama_url=_ollama_url)

    prefixes = cost_accounting.PrefixTracker()

    def write(row: dict) -> None:
        # Real Anthropic spend per call (PR 8). Never lets accounting cost a row.
        try:
            cost_accounting.annotate(row, prefixes.get(row.get("session_id")))
            prefixes.update(row)
        except Exception as exc:  # noqa: BLE001 - the ledger row matters more than its cost fields
            failopen.record("LR-FO-PROXY-LEDGER-COST", exc)
        ledger.write_row(row, cfg.ledger_path)

    async def try_serve(body: dict, row: dict) -> dict | None:
        t0 = time.monotonic()
        session_id = row.get("session_id")
        exhausted = guard.exhausted(session_id)
        if exhausted is not None:
            # Forced fallback: this session already hit the consecutive-served
            # cap, so the streak is broken WITHOUT trying the backend at all.
            guard.reset(session_id)
            row.update(decision=ledger.DECISION_FALLBACK, reason=REASON_LOOP_GUARD,
                       detail=exhausted, added_latency_s=round(time.monotonic() - t0, 3))
            return None
        try:
            choice = await choose_model(classify_text(body), cfg.model)
        except Exception as exc:  # noqa: BLE001 - any policy failure forwards the call
            guard.reset(session_id)
            row.update(decision=ledger.DECISION_FALLBACK, reason="policy_error",
                       detail=ledger.scrub_detail(f"{type(exc).__name__}: {exc}"))
            row["added_latency_s"] = round(time.monotonic() - t0, 3)
            return None
        row.update(task_type=choice["task_type"], complexity=choice["complexity"],
                   chain_head=choice["chain_head"], model=choice["model"])
        if choice["model"] is None:
            guard.reset(session_id)
            row.update(decision=ledger.DECISION_FORWARDED, reason="policy_kept")
            row["added_latency_s"] = round(time.monotonic() - t0, 3)
            return None
        if local_agent is not None:
            gate = await local_agent.gate(body, cfg.steps, choice)
            row["local_agent"] = gate.as_row()
            if not gate.local:
                guard.reset(session_id)
                row.update(decision=ledger.DECISION_FORWARDED, reason=gate.reason)
                row["added_latency_s"] = round(time.monotonic() - t0, 3)
                return None
        message, err, reason = None, None, None
        backend_t0, backend_outcome = None, None
        try:
            backend = make_backend(choice["model"])
            admitted, health_info = await health.admit(choice["model"], backend)
            if health_info is not None:
                row["backend_health"] = health_info
            if not admitted:
                # Skipped without an attempt (and before compaction's embedding
                # call): the backend is known to be broken.
                guard.reset(session_id)
                row.update(decision=ledger.DECISION_FORWARDED, reason=REASON_BACKEND_UNHEALTHY,
                           added_latency_s=round(time.monotonic() - t0, 3))
                return None
            if local_agent is not None:
                send, row["compaction"] = await local_agent.prepare(body)
            else:
                send = apply_trims(body, trims)
            # Repo knowledge for the local model (proxy.okf_context): the trimmed
            # request is all it sees. Local path only, never the pass-through;
            # fail-open; in a thread because retrieval and the repo-state line
            # do file and git I/O. ``body`` (the client's request) is untouched.
            # Bounded: repo_facts can run several git calls; a slow attach must
            # not eat the step budget. On timeout the step goes ahead without it
            # (the worker thread finishes on its own and its result is dropped).
            try:
                send, row["okf"] = await asyncio.wait_for(asyncio.to_thread(
                    okf_context.attach, send, body,
                    ceiling_tokens=cfg.local_agent.max_prompt_tokens if cfg.local_agent else DEFAULT_MAX_PROMPT_TOKENS),
                    timeout=OKF_ATTACH_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                failopen.record("LR-FO-PROXY-OKF-ATTACH-TIMEOUT", exc)
                row["okf"] = {"status": "timeout"}
            backend_t0 = time.monotonic()
            message, err, backend_usage = await asyncio.wait_for(
                backend.complete(send, cfg.step_budget_s), timeout=cfg.step_budget_s)
            row["backend_usage"] = backend_usage
            if err:
                reason = "validation"
            # The backend's own outcome and time, captured before the capability
            # check below reuses ``err`` and the edit protocol adds its own calls.
            backend_outcome = (err, reason, time.monotonic() - backend_t0)
            if not err and message is not None:
                # The hard rule: an edit from the raw tool-call loop is never
                # served; it goes to the validated edit protocol or to Claude.
                verdict = la_capability.check_reply(
                    message, body, edit_mode=cfg.local_agent.edit_mode if local_agent else "claude")
                if local_agent is not None:
                    row["local_agent"] = dict(row.get("local_agent") or {}, reply=verdict.as_row())
                if verdict.route == la_capability.ROUTE_CLAUDE:
                    message, err, reason = None, verdict.detail or verdict.reason, verdict.reason
                elif verdict.route == la_capability.ROUTE_EDIT:
                    message, err, row["edit_protocol"] = await local_agent.edit_step(
                        verdict, message, body, backend, t0 + cfg.step_budget_s)
                    if err:
                        reason = "edit_protocol_failed"
                    else:
                        row["served_via"] = la_capability.ROUTE_EDIT
        except HedgeTimeout as exc:
            err, reason = str(exc), "hedge_timeout"
        except ContextOverflow as exc:
            # The backend refused to send a prompt Ollama would have silently
            # truncated. Distinct reason so an operator can tell this apart
            # from a genuine backend failure — see local_context_guard.
            err, reason = str(exc), "local_context_overflow"
        except (asyncio.TimeoutError, httpx.TimeoutException):
            err, reason = f"exceeded step budget {cfg.step_budget_s}s", "budget_exceeded"
        except Exception as exc:  # noqa: BLE001 - any backend failure is a counted fallback
            err, reason = f"{type(exc).__name__}: {exc}", "backend_error"
        if backend_t0 is not None:
            tripped = health.record(choice["model"], *(backend_outcome
                                                       or (err, reason, time.monotonic() - backend_t0)))
            if tripped is not None:
                row["backend_health"] = dict(row.get("backend_health") or {}, **tripped)
        elapsed = round(time.monotonic() - t0, 3)
        row["route_latency_s"] = elapsed
        if err or message is None:
            guard.reset(session_id)
            row.update(decision=ledger.DECISION_FALLBACK, reason=reason or "validation",
                       detail=ledger.scrub_detail(err or "no message"), added_latency_s=elapsed)
            return None
        repeat = guard.repeat_reason(session_id, message)
        if repeat is not None:
            # The candidate is real (passed validation) but repeats a recent
            # served step: served/avoided metrics must never see it.
            guard.reset(session_id)
            row.update(decision=ledger.DECISION_FALLBACK, reason=REASON_LOOP_GUARD,
                       detail=repeat, added_latency_s=elapsed)
            return None
        guard.record_served(session_id, message)
        row.update(decision=ledger.DECISION_SERVED, reason=None, msg_id=message["id"],
                   added_latency_s=0.0,
                   served_blocks=[b["type"] + (":" + b["name"] if b.get("name") else "")
                                  for b in message["content"]],
                   served_stop_reason=message["stop_reason"])
        return message

    async def decide_tier(body: dict, row: dict):
        """The tier rewrite's decision, written onto ``row``. Any error forwards
        the call unchanged and says why: this path must never cost a call."""
        t0 = time.monotonic()
        try:
            decision = await tier_policy.decide(body, row.get("session_id"), sticky)
        except Exception as exc:  # noqa: BLE001 - fail-safe: forward unchanged
            row.update(served_model=body.get("model"), tier=None, tier_reason=REASON_DECISION_ERROR,
                       tier_proposed=None,
                       tier_switch=False, tier_switch_cost_usd=None, tier_complexity_score=None,
                       tier_detail=ledger.scrub_detail(f"{type(exc).__name__}: {exc}"),
                       tier_decision_s=round(time.monotonic() - t0, 3))
            return None
        row.update(served_model=decision.served_model, tier=decision.tier, tier_reason=decision.reason,
                   tier_proposed=decision.proposed_tier,
                   tier_switch=decision.switched, tier_switch_cost_usd=decision.switch_cost_usd,
                   tier_task_type=decision.task_type, tier_complexity=decision.complexity,
                   tier_complexity_score=decision.complexity_score,
                   # decision.detail is always one of our own fixed reason
                   # strings (escalation.REASON_*), never free text off the
                   # request, so it needs no scrubbing — unlike the error
                   # path above, which logs an exception message.
                   tier_detail=decision.detail,
                   tier_quota_pressure=decision.quota_pressure, tier_quota_state=decision.quota_state,
                   tier_decision_s=round(time.monotonic() - t0, 3))
        return decision

    async def forward(request: Request, raw: bytes, body: dict | None, row: dict | None, *,
                      original: tuple[bytes, dict] | None = None, on_retry=None, on_usage=None):
        """``original``: when ``raw`` is a tier-rewritten body, the client's own
        (bytes, body). A 4xx on the rewritten call (a parameter the target tier
        does not take, a model the account cannot use) is retried once with
        it, so a rewrite can never turn a working call into a failed one."""
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        headers["accept-encoding"] = "identity"
        url = upstream + request.url.path + (("?" + request.url.query) if request.url.query else "")
        t0 = time.monotonic()

        async def send(content: bytes):
            req = http.build_request(request.method, url, headers=headers, content=content)
            return await http.send(req, stream=True)

        try:
            up = await send(raw)
            if original is not None and 400 <= up.status_code < 500 and up.status_code not in (401, 413):
                text = (await up.aread()).decode("utf-8", "replace")
                await up.aclose()
                if row is not None:
                    row["tier_retry"] = {"status": up.status_code, "detail": ledger.scrub_detail(text)}
                    # The rewrite never ran, so neither did its switch.
                    row.update(served_model=row.get("requested_model"), tier_switch=False,
                               tier_switch_cost_usd=None)
                    row.pop("tier_body_rewrite", None)
                if on_retry is not None:
                    on_retry()
                raw, body = original
                up = await send(raw)
            if (row is not None and body is not None and up.status_code == 400
                    and row.get("mixed_history") and "thinking" in body):
                text = (await up.aread()).decode("utf-8", "replace")
                if is_thinking_rejection(up.status_code, text):
                    await up.aclose()
                    row["thinking_retry"] = True
                    up = await send(json.dumps(without_thinking(body)).encode())
                else:
                    await up.aclose()
                    return _finish_error(up.status_code, text.encode(), up.headers, row, t0)
        except httpx.HTTPError as exc:
            if row is not None:
                row.update(upstream_status=502,
                           detail=row.get("detail") or ledger.scrub_detail(f"upstream: {type(exc).__name__}"))
                row["upstream_latency_s"] = round(time.monotonic() - t0, 3)
                write(row)
            return Response(_error_json("llm-router proxy: Anthropic upstream unreachable"),
                            status_code=502, media_type="application/json")

        resp_headers = {k: v for k, v in up.headers.items()
                        if k.lower() not in _HOP | {"content-encoding", "content-length"}}
        if row is not None:
            row["upstream_status"] = up.status_code

        async def relay():
            buf = bytearray()
            try:
                async for chunk in up.aiter_raw():
                    if row is not None:
                        buf.extend(chunk)
                    yield chunk
            finally:
                await up.aclose()
                if row is not None:
                    row["upstream_latency_s"] = round(time.monotonic() - t0, 3)
                    _record_usage(row, bytes(buf), up.headers.get("content-type", ""))
                    if on_usage is not None and up.status_code == 200:
                        on_usage(row.get("usage"))
                    write(row)

        return StreamingResponse(relay(), status_code=up.status_code, headers=resp_headers)

    def _finish_error(status: int, content: bytes, headers, row: dict, t0: float):
        row["upstream_status"] = status
        row["upstream_latency_s"] = round(time.monotonic() - t0, 3)
        write(row)
        keep = {k: v for k, v in headers.items() if k.lower() not in _HOP | {"content-encoding", "content-length"}}
        return Response(content, status_code=status, headers=keep)

    def _record_usage(row: dict, buf: bytes, ctype: str) -> None:
        if "event-stream" in ctype:
            usage, stop, msg_id = parse_sse_usage(buf)
            response_model = sse_response_model(buf)
        else:
            try:
                data = json.loads(buf or b"{}")
            except ValueError:
                data = {}
            usage = data.get("usage") if isinstance(data, dict) else None
            stop = data.get("stop_reason") if isinstance(data, dict) else None
            msg_id = data.get("id") if isinstance(data, dict) else None
            response_model = data.get("model") if isinstance(data, dict) else None
        if isinstance(response_model, str):
            row["response_model"] = response_model
        row["stop_reason"] = stop
        status = row.get("upstream_status")
        if isinstance(status, int) and 200 <= status < 300 and (not usage or stop is None):
            # A 2xx reply whose usage never arrived (no usage in the body or
            # SSE) or whose stream ended before the terminal message_delta (cut,
            # aborted, upstream read error): Anthropic billed SOMETHING, and what
            # the proxy saw is a placeholder (message_start carries output_tokens=1).
            # Unknown is null, never zeros (normalize_usage(None) is all zeros),
            # so cost accounting cannot report it as a known $0.
            row["usage"] = None
            row["usage_unknown"] = "no_usage" if not usage else "truncated"
        else:
            row["usage"] = ledger.normalize_usage(usage)
        if msg_id and not row.get("msg_id"):
            row["msg_id"] = msg_id

    async def handle(request: Request):
        from llm_router.route_server import is_forbidden_cross_origin

        if is_forbidden_cross_origin(request.headers):
            return Response(_error_json("forbidden: cross-origin request rejected"), status_code=403,
                            media_type="application/json")
        raw = await request.body()
        is_messages = request.method == "POST" and request.url.path == "/v1/messages"
        if not is_messages:
            return await forward(request, raw, None, None)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return await forward(request, raw, None, None)

        row: dict = {
            "ts": time.time(), "session_id": session_id_of(body), "msg_id": None,
            "stream": bool(body.get("stream")), "requested_model": body.get("model"),
            "auth": ledger.auth_kind(request.headers),
            "mixed_history": has_served_turn(body), "thinking_retry": False,
            "step_class": step_class(body, set(STEP_CLASSES)),
            "prev_tools": prev_tools(body),
            "decision": ledger.DECISION_FORWARDED, "added_latency_s": 0.0,
            "tier_mode": cfg.tiers,
            # KPI instrumentation (G3): these keys exist on EVERY forwarded/served
            # row, with an honest null when not computed, so "absent" never has to
            # be read as 0. tier_proposed is filled by decide_tier.
            "session_kind": session_kind.kind_of(session_id_of(body)),
            "tier_policy_version": tier_policy.policy_version if tier_policy is not None else None,
            "tier_proposed": None, "tier_retry": None,
        }
        if not cfg.steps:
            row["reason"] = "routing_off"
        elif row["step_class"] is None or row["step_class"] not in cfg.steps:
            row["reason"] = "not_eligible"
        else:
            message = await try_serve(body, row)
            if message is not None:
                write(row)
                if body.get("stream"):
                    return Response(sse_from_message(message), media_type="text/event-stream")
                return Response(json.dumps(message), media_type="application/json")
        if tier_policy is None:
            return await forward(request, raw, body, row)
        decision = await decide_tier(body, row)
        if decision is None:
            return await forward(request, raw, body, row)
        key = conversation_key(body, row.get("session_id"))

        def _on_usage(usage) -> None:
            sticky.record_usage(key, usage)

        if not decision.rewritten and decision.body_rewrite is None:
            return await forward(request, raw, body, row, on_usage=_on_usage)
        sent = dict(body, model=decision.served_model)
        if decision.body_rewrite == REWRITE_HAIKU:
            # Haiku 4.5 400s on `thinking.type: adaptive` and has no effort
            # parameter: serve it a body it accepts (translate.for_haiku).
            sent = for_haiku(sent)
            row["tier_body_rewrite"] = REWRITE_HAIKU

        def _on_retry() -> None:
            sticky.record(key, body.get("model"), decision.complexity, "tier_rejected")

        return await forward(request, json.dumps(sent).encode(), sent, row, original=(raw, body),
                             on_retry=_on_retry, on_usage=_on_usage)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = None
        if cfg.warm_up and cfg.steps:
            async def _run() -> None:
                result = await warm_up()
                print(f"llm-router proxy warm-up: {result}", flush=True)
            # In the background: the proxy serves (and hedges to Claude) while
            # the model loads.
            task = asyncio.create_task(_run())
        yield
        if task is not None and not task.done():
            task.cancel()

    methods = ["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH", "OPTIONS"]
    app = Starlette(routes=[Route("/{path:path}", handle, methods=methods)], lifespan=lifespan)
    app.state.http = http
    app.state.warm_up = warm_up
    return app


# ── CLI ──────────────────────────────────────────────────────────────────────


def enable_hint(host: str, port: int | str) -> str:
    """The one-liner a session runs to use the proxy, WITH the Tool Search
    override. Without ``ENABLE_TOOL_SEARCH=true``, Claude Code sees a
    non-first-party ``ANTHROPIC_BASE_URL`` and inlines every deferred
    MCP/skill tool schema into every request instead of the few a step needs
    -- a measured 3.5x Anthropic-side cost on 2026-09-28 (docs/proxy.md "Cost
    parity"). This proxy forwards ``/v1/messages`` bodies unchanged, so the
    resulting ``tool_reference`` blocks reach Anthropic exactly as Claude Code
    made them and the override is safe."""
    return f"ANTHROPIC_BASE_URL=http://{host}:{port} ENABLE_TOOL_SEARCH=true claude"


USAGE = f"""\
llm-router proxy [--port N] [--steps continuation|off] [--step-budget-s S]
                 [--hedge-s S|off] [--model ollama/TAG] [--trim NAME[,NAME]]
                 [--num-ctx N] [--ollama-url URL] [--keep-alive -1|5m] [--no-warm-up]
                 [--loop-max-consecutive N] [--loop-repeat-window N]
                 [--tiers off|on|conversation] [--tier-policy FILE.yaml] [--local-agent]
                 [--backend-fail-n N] [--backend-cooldown-s S]
llm-router proxy stats [--days N] [--json]

Opt-in, per session. Nothing is enabled until you point a session at it:
    {enable_hint("127.0.0.1", DEFAULT_PORT)}
"""


def cmd_proxy(argv: list[str]) -> int:
    from llm_router.env_loader import load_dotenv_files
    load_dotenv_files()  # LLM_ROUTER_PROXY_* etc. from .env; real env wins
    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv and argv[0] == "stats":
        ap = argparse.ArgumentParser(prog="llm-router proxy stats")
        ap.add_argument("--days", type=float, default=None)
        ap.add_argument("--json", action="store_true")
        a = ap.parse_args(argv[1:])
        s = ledger.stats(ledger.read_rows(days=a.days))
        print(json.dumps(s, indent=2) if a.json else ledger.format_stats(s))
        return 0

    try:
        env = ProxyConfig.from_env()
    except ValueError as exc:
        sys.stderr.write(f"llm-router proxy: bad LLM_ROUTER_PROXY_* setting: {exc}\n")
        return 2
    ap = argparse.ArgumentParser(prog="llm-router proxy", usage=USAGE)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("LLM_ROUTER_PROXY_PORT", DEFAULT_PORT)))
    ap.add_argument("--steps", default=",".join(sorted(env.steps)) or "off")
    ap.add_argument("--step-budget-s", type=float, default=env.step_budget_s)
    ap.add_argument("--model", default=env.model)
    ap.add_argument("--trim", default=env.trim)
    ap.add_argument("--num-ctx", type=int, default=env.num_ctx)
    ap.add_argument("--hedge-s", default=None, help="first-token deadline in seconds, or 'off'")
    ap.add_argument("--ollama-url", default=None, help="e.g. a dedicated tuned `ollama serve` (docs/proxy.md)")
    ap.add_argument("--keep-alive", default=str(DEFAULT_KEEP_ALIVE))
    ap.add_argument("--no-warm-up", action="store_true")
    ap.add_argument("--ledger", default=None, help="write rows here instead of the state dir")
    ap.add_argument("--loop-max-consecutive", type=int, default=env.loop_max_consecutive,
                     help="force a step to Anthropic after this many served-in-a-row for a session (0 disables)")
    ap.add_argument("--loop-repeat-window", type=int, default=env.loop_repeat_window,
                     help="reject a served tool call that repeats one of this many recent steps (0 disables)")
    ap.add_argument("--tiers", default=env.tiers,
                    help="Claude-tier rewrite (opt-in): off, on (per-turn, PR #215) or "
                         "conversation (classify once at the conversation's start, Phase 1.2b)")
    ap.add_argument("--tier-policy", default=env.tier_policy,
                    help="tier policy YAML (default: the bundled proxy/claude_tiers.yaml)")
    ap.add_argument("--local-agent", action="store_true",
                    help="capability gating + tool retrieval/compaction for local steps "
                         "(llm_router.local_agent; also LLM_ROUTER_LOCAL_AGENT=on). Off by default")
    ap.add_argument("--backend-fail-n", type=int, default=env.backend_fail_n,
                    help="stop sending steps to the local backend after this many consecutive empty "
                         "or sub-second invalid replies (a crash signature stops it at once; 0 disables)")
    ap.add_argument("--backend-cooldown-s", type=float, default=env.backend_cooldown_s,
                    help="seconds before a one-token probe checks whether the backend recovered")
    a = ap.parse_args(argv)

    from llm_router.net_bind import refuse_public_bind_or_exit

    refuse_public_bind_or_exit(a.host, component="proxy")
    try:
        cfg = ProxyConfig(steps=parse_steps(a.steps), step_budget_s=a.step_budget_s, model=a.model,
                          trim=a.trim, num_ctx=a.num_ctx, upstream=env.upstream,
                          ledger_path=Path(a.ledger) if a.ledger else None,
                          hedge_s=parse_hedge(a.hedge_s) if a.hedge_s is not None else env.hedge_s,
                          keep_alive=parse_keep_alive(a.keep_alive), warm_up=not a.no_warm_up,
                          ollama_url=a.ollama_url, loop_max_consecutive=a.loop_max_consecutive,
                          loop_repeat_window=a.loop_repeat_window, tiers=parse_tiers_mode(a.tiers),
                          tier_policy=a.tier_policy,
                          local_agent=(LocalAgentConfig.from_env() if a.local_agent else env.local_agent),
                          backend_fail_n=a.backend_fail_n, backend_cooldown_s=a.backend_cooldown_s)
        app = build_app(cfg)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"llm-router proxy: {exc}\n")
        return 2

    import uvicorn

    steps = ",".join(sorted(cfg.steps)) or "off (pass-through only)"
    print(f"llm-router proxy -> http://{a.host}:{a.port}  steps={steps}  "
          f"hedge={cfg.hedge_s}s  budget={cfg.step_budget_s}s  "
          f"trim={'compact (local agent)' if cfg.local_agent else (cfg.trim or 'fast')}  "
          f"loop_guard(max_consecutive={cfg.loop_max_consecutive}, repeat_window={cfg.loop_repeat_window})  "
          f"tiers={cfg.tiers + ' (' + (cfg.tier_policy or 'bundled policy') + ')' if cfg.tiers != TIERS_OFF else 'off'}  "
          f"local_agent={'on' if cfg.local_agent else 'off'}  "
          f"backend_health(fail_n={cfg.backend_fail_n}, cooldown={cfg.backend_cooldown_s:g}s)")
    print(f"  enable per session: {enable_hint(a.host, a.port)}")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning", access_log=False)
    return 0
