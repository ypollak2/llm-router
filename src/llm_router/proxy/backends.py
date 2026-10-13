"""Pluggable pieces of the serving path: model choice, request trims, backends.

Three plug points, so measured speed levers can slot in without touching the
server:

``choose_model``  The router's OWN policy decides whether a step leaves
                  Claude: the classifier (``classify_signals`` with
                  ``GATEWAY_POLICY``) and ``router._build_and_filter_chain``,
                  then the first tool-capable entry of that chain. A chain with
                  no tool-capable entry means the policy kept the call on
                  Claude. ``--model`` / ``LLM_ROUTER_PROXY_MODEL`` changes which
                  tool-capable model serves, never whether.

``TRIMS``         named request transforms applied before translation
                  (``--trim`` / ``LLM_ROUTER_PROXY_TRIM``, comma-separated). A
                  ``module:function`` spec loads a transform from outside this
                  package, so an experiment can add one without a code change
                  here. Each takes and returns an Anthropic request body and
                  must not mutate its input.

``BACKENDS``      provider prefix -> backend class. Only ``ollama/`` speaks tool
                  calls today; ``codex/`` is a subprocess CLI with no tool
                  channel and ``anthropic/`` is the pass-through path itself
                  (a valid target only for the Claude-tier rewrite).

DEFAULTS come from the local-speed spike (``docs/spikes/local-speed-2026-09-28.md``,
2026-09-28, qwen3-coder:30b, n=24 replayed continuation calls: median 2.19 s,
p90 3.59 s, 0/24 validation failures; that spike ran a tuned dedicated
``ollama serve`` and its absolute numbers depend on it):
  * trim ``fast``: a 366-char condensed system prompt, only the tools the step
    class needs (names and schemas byte-identical). The spike capped history to
    the first user turn plus the last exchange (~3k prompt tokens instead of
    ~21k). Since PLAN v16 P1.1, history comes from the context pack
    (``context_pack.build_pack(door="proxy")``): the whole conversation when it
    fits 0.8 x the local window, else the last 7 messages with tool output
    capped, plus the request verbatim. Full mode is also capped at
    ``LLM_ROUTER_PROXY_FULL_CAP`` (default 6000 estimated payload
    tokens). Prompts are larger than the spike's;
  * ``num_predict`` capped at 200 after Read/Bash/no tool, 700 otherwise; a
    reply that hits the cap is rejected (``translate.from_ollama``);
  * ``keep_alive`` (default 600 s; -1 only when asked) on every call and one warm-up call at proxy start;
  * an 8 s first-token hedge: no first token in time -> fall back to Claude.
    Cold start was still 8-110 s in that spike, which is why both the warm-up
    and the hedge exist.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import os
from typing import Callable, Protocol

from llm_router import local_models
from llm_router.local_context_guard import DEFAULT_RESERVED_OUTPUT_TOKENS, check_overflow, check_truncated, effective_window, estimate_payload_tokens
from llm_router.proxy.steps import STEP_CONTINUATION, non_system, prev_tools
from llm_router.proxy.translate import from_ollama, to_ollama

Trim = Callable[[dict], dict]

# ── trims ────────────────────────────────────────────────────────────────────


def _trim_tool_descriptions(limit: int) -> Trim:
    def trim(body: dict) -> dict:
        out = dict(body)
        tools = []
        for t in body.get("tools") or []:
            if isinstance(t, dict) and isinstance(t.get("description"), str) and len(t["description"]) > limit:
                t = dict(t, description=t["description"][:limit])
            tools.append(t)
        out["tools"] = tools
        return out
    return trim


def _trim_unused_tools(body: dict, keep: set[str] | frozenset[str] | None = None) -> dict:
    """Keep only tools the conversation has already used plus ``keep``.

    ``keep`` defaults to the core file and shell tools (the ``unused-tools``
    trim, unmeasured, off by default). ``local_agent.compact`` passes its
    embedding-retrieval result instead, which replaces ``STEP_TOOLS`` on that
    path. Kept tools are the request's own dicts: names and schemas unchanged,
    request order kept."""
    core = set(keep) if keep is not None else {"Read", "Edit", "Write", "Bash", "Glob", "Grep"}
    used = set()
    for m in body.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), list):
            used |= {b.get("name") for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"}
    keep = core | used
    out = dict(body)
    out["tools"] = [t for t in body.get("tools") or [] if isinstance(t, dict) and t.get("name") in keep]
    return out


# The local-speed spike's condensed system prompt (366 chars), verbatim.
CONDENSED_SYSTEM = (
    "You are continuing an autonomous coding-fix session. You will be shown "
    "the result of your most recent tool call. Decide the single next step: "
    "call exactly one tool if more work is needed (use the exact tool name "
    "and argument schema given), or reply with a short final text summary "
    "if the task is already done. Do not repeat a step you already "
    "completed successfully."
)

# Tools each step class needs, in the order they are offered. Only tools the
# request itself carries are kept, with their names and schemas unchanged, so
# a served call is always valid for the client that sent the request.
STEP_TOOLS: dict[str, tuple[str, ...]] = {
    STEP_CONTINUATION: ("Read", "Edit", "Write", "Bash"),
}


# Full mode (whole conversation, native) is used only when estimate_payload_tokens
# of the to_ollama payload (the guard's estimator) is at most this many tokens
# and fits window - DEFAULT_RESERVED_OUTPUT_TOKENS; above it, send the pack's
# last-7 history. Cold-cache first-token time on qwen3-coder:30b (num_ctx
# 32768) grows superlinearly with prompt size: ~4.0k real tokens 2.9 s, ~8.1k
# 9.4 s, ~15.7k 31 s, ~22.9k 64 s against the 8 s first-token hedge (P1.1 door
# 3 measurement, 2026-10-13, N=1-3 per size, Read-like code output, where this
# estimate runs ~1.36x Ollama's count). 6000 keeps such content near 4.4k real
# tokens. LLM_ROUTER_PROXY_FULL_CAP overrides this default.
DEFAULT_FULL_MAX_TOKENS = 6000


def full_max_tokens() -> int:
    """Positive full-mode payload cap, or the default for an invalid setting."""
    raw = os.environ.get("LLM_ROUTER_PROXY_FULL_CAP")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FULL_MAX_TOKENS
    return value if value > 0 else DEFAULT_FULL_MAX_TOKENS


def _pack_messages(turns: list, window: int, shell: dict) -> tuple[str, list]:
    """Keep the request verbatim and untruncated, sent natively.

    Full mode sends the whole conversation natively only when the native
    payload estimate fits both the window budget and full_max_tokens(). The
    non-system turns are conversation; Claude Code's ``role: system`` reminders
    are not. Pack mode sends capped recent history as text plus the newest
    exchange natively. It can still exceed the window and is left to the
    existing guard (PayloadPack.trim_to / check_overflow in
    OllamaBackend.complete).
    target_provider="ollama" applies the session-context privacy rule:
    LLM_ROUTER_SESSION_CONTEXT=off empties history. Project/instructions
    layers are not built here (no project_root): proxy.okf_context.attach
    already attaches repository context after the trim.
    """
    from llm_router import context_pack

    request = context_pack.extract_turn_text(
        turns[-1].get("content"), keep_tools=True, cap=False).strip()
    pack = context_pack.build_pack(
        request, turns[:-1], door="proxy", target_window=window, target_provider="ollama")
    if pack.mode == "full":
        candidate = dict(shell, messages=list(turns))
        if pack.system:
            candidate["system"] = pack.system + "\n\n" + shell["system"]
        est = estimate_payload_tokens(to_ollama(candidate, "", num_ctx=window))
        if est <= min(window - DEFAULT_RESERVED_OUTPUT_TOKENS, full_max_tokens()):
            return pack.system, list(turns)
        recent = context_pack.messages_for(
            "proxy", messages=turns[:-1], cap=True)[-context_pack.RECENT_N:]
    else:
        recent = list(pack.recent)
    tail = turns[-2:] if len(turns) >= 2 and turns[-2].get("role") == "assistant" else turns[-1:]
    if len(tail) == 2 and recent and recent[-1] == {
        "role": "assistant",
        "content": context_pack.extract_turn_text(
            turns[-2].get("content"), keep_tools=True, cap=True).strip(),
    }:
        recent.pop()
    return pack.system, recent + tail


def _trim_fast(body: dict, target_window: int | None = None) -> dict:
    """Condensed system prompt and the step class's tool subset; history now
    comes from the one context pack (PLAN v16 P1.1 door 3)."""
    out = dict(body)
    out["system"] = CONDENSED_SYSTEM
    wanted = STEP_TOOLS[STEP_CONTINUATION]
    by_name = {t.get("name"): t for t in body.get("tools") or [] if isinstance(t, dict)}
    out["tools"] = [by_name[n] for n in wanted if n in by_name]
    turns = non_system(body.get("messages") or [])
    if turns:
        window = target_window if isinstance(target_window, int) and target_window > 0 else local_models.num_ctx(None)
        system_layers, messages = _pack_messages(turns, window, out)
        out["messages"] = messages
        if system_layers:
            out["system"] = system_layers + "\n\n" + CONDENSED_SYSTEM
    return out


TRIMS: dict[str, Trim] = {
    "none": lambda body: body,
    "fast": _trim_fast,
    # The first spike's setting (6/6 golden tasks, 0/19 validation failures,
    # but a 61 s median per routed call): descriptions cut to 2,000 chars.
    "tool-desc-2000": _trim_tool_descriptions(2000),
    "unused-tools": _trim_unused_tools,
}
DEFAULT_TRIM = "fast"


def resolve_trims(spec: str | None) -> list[Trim]:
    """``"a,b"`` -> the transforms, in order. Unknown names raise ``ValueError``
    at startup rather than silently serving untrimmed requests."""
    out: list[Trim] = []
    for name in [s.strip() for s in (spec or DEFAULT_TRIM).split(",") if s.strip()]:
        if name in TRIMS:
            out.append(TRIMS[name])
        elif ":" in name:
            mod, _, attr = name.partition(":")
            fn = getattr(importlib.import_module(mod), attr, None)
            if not callable(fn):
                raise ValueError(f"trim {name!r} is not callable")
            out.append(fn)
        else:
            raise ValueError(f"unknown trim {name!r}; known: {', '.join(sorted(TRIMS))}")
    return out


def apply_trims(body: dict, trims: list[Trim], *, target_window: int | None = None) -> dict:
    out = copy.deepcopy(body)
    for trim in trims:
        out = trim(out, target_window=target_window) if trim is _trim_fast else trim(out)
    return out


# ── backends ─────────────────────────────────────────────────────────────────


class Backend(Protocol):
    async def complete(self, body: dict, timeout_s: float) -> tuple[dict | None, str | None, dict]:
        """``(anthropic_message | None, error | None, backend_usage)``."""


NUM_PREDICT_EASY = 200  # after Read / Bash / no tool: pick the obvious next action
NUM_PREDICT_HARD = 700  # anything else (e.g. after an Edit)
DEFAULT_HEDGE_S = 8.0
# N20: bounded; an explicit --keep-alive -1 is still honoured (server.parse_keep_alive).
DEFAULT_KEEP_ALIVE: int | str = 600


def num_predict_for(body: dict) -> int:
    prev = prev_tools(body)
    return NUM_PREDICT_EASY if (not prev or prev[0] in ("Read", "Bash")) else NUM_PREDICT_HARD


class HedgeTimeout(Exception):
    """No first token within the hedge deadline; the caller falls back."""


class OllamaBackend:
    """Ollama ``/api/chat`` with native tool calls, thinking off, streamed so
    the first-token hedge can fire before a cold model finishes loading."""

    def __init__(self, model: str, client, *, base_url: str, num_ctx: int | None = None,
                 hedge_s: float | None = DEFAULT_HEDGE_S,
                 keep_alive: int | str | None = DEFAULT_KEEP_ALIVE,
                 num_predict: int | None = None, offload_cpu: bool = False) -> None:
        # ``num_predict`` overrides the 200/700 per-step caps (sized for the
        # ``fast`` trim). Only ``--serve local-agent`` sets it: a full Claude Code
        # prompt makes the model emit several parallel tool calls or a whole file.
        self.num_predict = num_predict
        # Shadow mode: the conversion and size checks (json over the whole prompt) run in a
        # worker thread so they never stall the loop that is relaying Claude's reply.
        self.offload_cpu = offload_cpu
        self.model = model.split("/", 1)[1] if model.startswith("ollama/") else model
        self.client = client
        self.base_url = base_url.rstrip("/")
        # None: the per-model table value (llm_router.local_models, M3.4).
        self.num_ctx = num_ctx if num_ctx is not None else local_models.num_ctx(self.model)
        self.hedge_s = hedge_s
        self.keep_alive = keep_alive

    async def complete(self, body: dict, timeout_s: float) -> tuple[dict | None, str | None, dict]:
        def _prepare() -> dict:
            payload = to_ollama(body, self.model, num_ctx=self.num_ctx,
                                max_predict=self.num_predict or num_predict_for(body),
                                keep_alive=self.keep_alive, stream=True)
            # Refuse to send what Ollama would silently truncate from the front
            # (system prompt + tool definitions first) rather than find out from a
            # reply about the wrong thing. See local_context_guard module docstring.
            from llm_router.pack_overflow import PayloadPack
            pack = PayloadPack(payload, num_ctx=self.num_ctx, base_url=self.base_url,
                               model=self.model, site="proxy.OllamaBackend")
            payload = pack.trim_to()
            check_overflow(payload, num_ctx=pack.num_ctx, base_url=self.base_url, model=pack.model,
                           site="proxy.OllamaBackend")
            return payload

        payload = await asyncio.to_thread(_prepare) if self.offload_cpu else _prepare()
        objs: list[dict] = []
        first_token_s = None
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        async with self.client.stream("POST", self.base_url + "/api/chat", json=payload,
                                      timeout=timeout_s) as resp:
            await _raise_with_body(resp)
            lines = resp.aiter_lines()
            try:
                first = await asyncio.wait_for(lines.__anext__(), timeout=self.hedge_s)
            except asyncio.TimeoutError:
                raise HedgeTimeout(f"no first token within {self.hedge_s}s") from None
            except StopAsyncIteration:
                first = ""
            first_token_s = round(loop.time() - t0, 3)
            if first.strip():
                objs.append(json.loads(first))
            async for line in lines:
                if line.strip():
                    objs.append(json.loads(line))
        if not objs:
            return None, "empty response", {"first_token_s": first_token_s}
        stream_error = next((o["error"] for o in objs if o.get("error")), None)
        if stream_error:
            # A runner failure mid-stream arrives as an ``error`` line; read as
            # a reply it would look merely empty and hide the crash signature.
            return None, f"ollama error: {str(stream_error)[:200]}", {"first_token_s": first_token_s}
        data = merge_stream(objs)
        usage = {"prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count"),
                 "prompt_eval_s": round((data.get("prompt_eval_duration") or 0) / 1e9, 2),
                 "eval_s": round((data.get("eval_duration") or 0) / 1e9, 2),
                 "load_s": round((data.get("load_duration") or 0) / 1e9, 2),
                 "first_token_s": first_token_s}
        # Ollama's own count, checked against the estimate that cleared the
        # preflight above: if it came back truncated anyway (a window this
        # process does not know about, or a race with another resident
        # model), account for it so an operator can see it happened.
        def _truncated():
            window, _source = effective_window(num_ctx=self.num_ctx, base_url=self.base_url, model=self.model)
            return check_truncated(
                data.get("prompt_eval_count"), estimate_payload_tokens(payload), window,
                site="proxy.OllamaBackend", model=self.model,
            )

        usage["context_truncated"] = await asyncio.to_thread(_truncated) if self.offload_cpu else _truncated()
        message, err = from_ollama(data, body)
        return message, err, usage

    async def warm_up(self, timeout_s: float = 300.0) -> float:
        """Load the model (and pin it with keep_alive) before the first real
        step needs it. Returns seconds taken."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        payload = {"model": self.model, "messages": [{"role": "user", "content": "ok"}],
                   "stream": False, "think": False,
                   "options": {"num_ctx": self.num_ctx, "num_predict": 1}}
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        r = await self.client.post(self.base_url + "/api/chat", json=payload, timeout=timeout_s)
        r.raise_for_status()
        return round(loop.time() - t0, 2)


    async def probe(self, timeout_s: float) -> tuple[bool, str]:
        """The breaker's cheap health request (``proxy.backend_health``): one
        token, streamed, with the serving ``num_ctx`` so it never reloads the
        model. A crashed runner answers it with no lines at all (the 2026-09-30
        post-crash shape), an ``error`` line or an HTTP error."""
        payload = {"model": self.model, "messages": [{"role": "user", "content": "ok"}],
                   "stream": True, "think": False,
                   "options": {"num_ctx": self.num_ctx, "num_predict": 1}}
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive
        objs: list[dict] = []
        async with self.client.stream("POST", self.base_url + "/api/chat", json=payload,
                                      timeout=timeout_s) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                return False, f"HTTP {resp.status_code}: {body[:200]}"
            async for line in resp.aiter_lines():
                if line.strip():
                    objs.append(json.loads(line))
        if not objs:
            return False, "empty reply"
        err = next((o["error"] for o in objs if o.get("error")), None)
        if err:
            return False, f"ollama error: {str(err)[:200]}"
        text = "".join((o.get("message") or {}).get("content") or "" for o in objs)
        generated = objs[-1].get("eval_count")  # absent is unmeasured, not zero
        if text or (isinstance(generated, int) and generated >= 1):
            return True, "ok"
        return False, "no token generated"


async def _raise_with_body(resp) -> None:
    """``raise_for_status`` with the start of the error body in the message:
    Ollama puts the runner's failure text there, and the backend-health
    breaker matches crash signatures in it."""
    if resp.status_code < 400:
        return
    body = (await resp.aread()).decode("utf-8", "replace")
    raise RuntimeError(f"HTTP {resp.status_code}: {body[:200]}")


def merge_stream(objs: list[dict]) -> dict:
    """Fold Ollama's streamed NDJSON chunks into one non-streamed reply."""
    content = "".join((o.get("message") or {}).get("content") or "" for o in objs)
    calls: list = []
    for o in objs:
        calls.extend((o.get("message") or {}).get("tool_calls") or [])
    return dict(objs[-1], message={"role": "assistant", "content": content, "tool_calls": calls})


BACKENDS: dict[str, type] = {"ollama/": OllamaBackend}

# Claude tiers are served by the pass-through itself, with ``body["model"]``
# rewritten (``proxy.tiers``): every tier speaks the same Anthropic schema, so
# client tools stay native. They are valid targets only where a caller asks
# for them (``anthropic=True``); the local-serving path never gets one, since
# it has no backend object to hand them to.
ANTHROPIC_PREFIX = "anthropic/"


def tool_capable(model: str, *, anthropic: bool = False) -> bool:
    if anthropic and model.startswith(ANTHROPIC_PREFIX):
        return True
    return any(model.startswith(prefix) for prefix in BACKENDS)


# ── model choice ─────────────────────────────────────────────────────────────

_chain_cache: dict[tuple, list[str]] = {}
_chain_cache_token: tuple | None = None


def _classify_class(text: str, *, capabilities: bool = True) -> tuple[str, str]:
    """``(task_type, complexity)`` the proxy routes on.

    PLAN v16 P1.6 / D-72: ``classify_signals(GATEWAY_POLICY)`` decides, exactly as before the
    wiring; the one engine only runs after it, in shadow. ``LLM_ROUTER_ENGINE_MODE=decide`` makes
    the engine decide, reusing the hook's Decision for this turn by ``text_sha`` (P1.6-h)."""
    from llm_router import engine_door

    d = engine_door.decision(text, door="proxy", proxy=True)
    if d is not None:
        return d.task_type, d.complexity
    from llm_router.classify import GATEWAY_POLICY, classify_signals

    sig = classify_signals(text, GATEWAY_POLICY, capabilities=capabilities)
    out = sig.task_type.value, sig.complexity.value
    engine_door.shadow(text, door="proxy", rules_task=out[0], rules_complexity=out[1])
    return out


async def policy_chain(text: str) -> tuple[str, str, list[str]]:
    """``(task_type, complexity, chain)`` from the router's own policy."""
    from llm_router.config import get_config
    from llm_router.profiles import complexity_to_profile
    from llm_router import map_reader
    from llm_router.router import _build_and_filter_chain
    from llm_router.types import Complexity, TaskType

    key = _classify_class(text)
    # P1.8: the chain is cached with the map file's mtime, so a rewritten map is
    # re-read; None (no map file) is the pre-map behaviour.
    global _chain_cache_token
    tok = map_reader.token()
    if tok != _chain_cache_token:      # map rewritten, aged out or deleted: drop old chains
        _chain_cache.clear()
        _chain_cache_token = tok
    ckey = (*key, tok)
    if ckey not in _chain_cache:
        c = Complexity(key[1])
        _chain_cache[ckey] = await _build_and_filter_chain(
            TaskType(key[0]), complexity_to_profile(c), None, c, c, get_config())
    return key[0], key[1], _chain_cache[ckey]


async def tier_classify(text: str, pinned: str | None = None, *, anthropic: bool = True) -> dict:
    """The Claude tier decision's classifier (``proxy.tiers``): the same
    ``(task_type, complexity)`` as ``choose_model`` -- both are
    ``classify_signals(text, GATEWAY_POLICY)`` -- without building a provider chain.

    PLAN v16 P0.9-e. The tier decision reads only the class; the chain is
    informational there (``chain_head``) and its build (``_build_and_filter_chain``:
    three usage.db queries, dynamic routing, the Ollama model list) ran inside the
    timed decision on every (task_type, complexity) cache miss: 1.2-2.2 s on 3 of
    6 live turn-first rows [PL], 67-107 ms per new key and 21.6 s for the first
    one on a copy of the live usage.db (2026-10-07). The chain head and model
    now come from the cache only, ``[]`` / None until another caller has built
    it. Same signature and return shape as ``choose_model`` so it is a drop-in
    seam; ``pinned`` is accepted and ignored (no model is chosen here)."""
    del pinned
    from llm_router import map_reader

    task, cx = _classify_class(text, capabilities=False)  # the class only (P0.9-e)
    chain = _chain_cache.get((task, cx, map_reader.token())) or []
    model = next((m for m in chain if tool_capable(m, anthropic=anthropic)), None)
    return {"task_type": task, "complexity": cx, "chain_head": chain[:4], "model": model}


async def choose_model(text: str, pinned: str | None, *, anthropic: bool = False) -> dict:
    """``{"task_type", "complexity", "chain_head", "model"}``; ``model`` is None
    when the policy keeps the call on Claude. With ``anthropic=True`` an
    ``anthropic/*`` chain entry is a valid target too (the tier rewrite)."""
    task, cx, chain = await policy_chain(text)
    model = next((m for m in chain if tool_capable(m, anthropic=anthropic)), None)
    # A pin changes WHICH model serves, never WHETHER: the policy's decision to
    # keep a step on Claude (no tool-capable entry) stands.
    if model is not None and pinned:
        model = pinned if tool_capable(pinned, anthropic=anthropic) else None
    return {"task_type": task, "complexity": cx, "chain_head": chain[:4], "model": model}
