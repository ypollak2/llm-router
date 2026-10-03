"""Stop Ollama from silently truncating an oversized local prompt.

OWNER-APPROVED FIX, 2026-10-03. Ollama/llama.cpp runs with `--context-shift
--keep 4` by default: a prompt that exceeds the loaded context window is NOT
rejected -- the OLDEST tokens are discarded to make room, which is the system
prompt and the start of the task, and the model answers about whatever
survived with no indication anything was cut. This is not hypothetical:
`hooks/agent_loop.py` already measured it once (see `agent_loop._num_ctx`'s
docstring) -- a 33k-token prompt came back with `prompt_eval_count=16386` and
a canary planted in the system prompt gone.

Five local call paths build a prompt and send it straight to Ollama:
`proxy/backends.py` (`OllamaBackend`), `providers.py` (the MCP `llm()` tool,
via litellm's `ollama/` provider), `hooks/agent_loop.py` (the tool-calling
loop), and `hooks/direct_executor.py` (`call_ollama`, which
`zero_claude_edit.py` calls directly and so is covered transitively). All of
them call this module's `check_overflow()` with the payload right before they
would otherwise send it, and treat the resulting `ContextOverflow` exactly
like any other failed call to the local model -- the fallback machinery each
of them already has (chain failover in `router.py`, `return None` /
`_call_failure` in the hooks, a `reason` in the proxy's step row) is what
actually does the escalating. This module only decides NOT to send.

It does not change Ollama server settings, and it does not replace
`hooks/context_budget.py` (which prunes OLD TOOL RESULTS out of an
agent-loop conversation to keep it small). This module is the backstop for
everything that pruning does not reach: tool-definition JSON, an oversized
system prompt, a request nobody pruned at all.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.request

__all__ = [
    "ContextOverflow",
    "CHARS_PER_TOKEN",
    "ESTIMATE_SAFETY_MARGIN",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_RESERVED_OUTPUT_TOKENS",
    "estimate_tokens",
    "estimate_payload_tokens",
    "effective_window",
    "check_overflow",
    "check_truncated",
    "reset_cache",
]

#: Cheap chars-per-token estimate (English/code mixed prose). Deliberately a
#: heuristic, not a real tokenizer: this has to run on every local call with
#: no dependency and no measurable latency added to the request.
CHARS_PER_TOKEN = 3.5

#: Inflates the raw chars/CHARS_PER_TOKEN estimate before it is compared to
#: the window. The heuristic undercounts code and non-English text (more,
#: shorter tokens per character); this margin is what makes "estimated to
#: fit" a safe claim instead of an optimistic one.
ESTIMATE_SAFETY_MARGIN = 1.15

#: Ollama's own hardcoded default context window when nothing else says
#: otherwise (no explicit num_ctx, no resident-model report, no env var).
DEFAULT_CONTEXT_WINDOW = 4096

#: Tokens reserved for the model's reply when the caller has no better
#: number. Smaller than any real num_predict already used in this codebase
#: (200-700, see proxy/backends.py), so it under-reserves rather than
#: over-refusing a request that would in fact have fit.
DEFAULT_RESERVED_OUTPUT_TOKENS = 256

#: `/api/ps` is a local, in-memory listing; it must never be what slows a
#: local call down.
_PS_TIMEOUT_S = 0.5

#: How long a `/api/ps` reading is trusted before asking again. A tool-
#: calling loop can make several calls a second to the same server; this
#: keeps the window source from adding a network round trip to each one.
_PS_CACHE_TTL_S = 5.0

_ps_cache: dict[str, tuple[float, int | None]] = {}
_ps_cache_lock = threading.Lock()


class ContextOverflow(Exception):
    """Raised instead of sending a prompt Ollama would silently truncate.

    A plain `Exception` subclass on purpose: `providers.call_llm` raising
    this is already caught by `router.py`'s chain-failover `except
    Exception` and tried against the next model with no further wiring; the
    hooks (`agent_loop.py`, `direct_executor.py`) and the proxy
    (`proxy/server.py`) each catch it explicitly to attach a clean reason.
    """


def estimate_tokens(chars: int) -> int:
    """Token estimate for `chars` characters of prompt-shaped text."""
    return math.ceil(max(1, chars) / CHARS_PER_TOKEN * ESTIMATE_SAFETY_MARGIN)


def estimate_payload_tokens(payload: dict) -> int:
    """Estimate the tokens an Ollama `/api/chat` or `/api/generate` payload will cost.

    Serializes the WHOLE payload rather than hand-picking `messages` /
    `tools` / `prompt`: simpler, and the fields it would otherwise skip
    (`model`, `options`, `keep_alive`) add at most a few dozen characters,
    which `ESTIMATE_SAFETY_MARGIN` already covers. This is the gap
    `hooks/context_budget.py`'s own estimate leaves open -- it sums only
    message content and never accounts for tool-definition JSON, which on a
    Claude Code request is routinely the larger half of the prompt.
    """
    try:
        chars = len(json.dumps(payload, default=str))
    except (TypeError, ValueError):
        chars = sum(len(str(v)) for v in payload.values())
    return estimate_tokens(chars)


def _name_matches(wanted: str, resident: str) -> bool:
    from llm_router.warm import model_matches

    return model_matches(wanted, resident)


def _fetch_ps_context_length(base_url: str, model: str | None, *, timeout: float) -> int | None:
    """The `context_length` Ollama's `/api/ps` reports for a resident model.

    `None` on ANY failure or shape mismatch: unreachable server, malformed
    JSON, a `/api/ps` build that does not report `context_length`, or the
    named model simply not being resident. `None` means "unknown", never "0"
    or "very large" -- the caller falls through to the next window source
    (this is the fail-open path exercised by
    `test_fails_open_when_api_ps_errors`).
    """
    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/api/ps")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 — operator-configured local Ollama URL
            data = json.loads(resp.read())
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return None
        for m in models:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or m.get("model") or "")
            if not name:
                continue
            if model and not _name_matches(model, name):
                continue
            ctx = m.get("context_length")
            if isinstance(ctx, int) and ctx > 0:
                return ctx
        return None
    except Exception as exc:                                  # noqa: BLE001 — fail-open: unknown, not zero
        from llm_router import failopen

        failopen.record("CHZ-FO-LOCAL-CTX-PS-UNREACHABLE", exc, detail=base_url)
        return None


def _ps_context_length(base_url: str, model: str | None, *, timeout: float = _PS_TIMEOUT_S) -> int | None:
    """`_fetch_ps_context_length`, cached briefly (see `_PS_CACHE_TTL_S`)."""
    key = f"{base_url}|{model or ''}"
    now = time.monotonic()
    with _ps_cache_lock:
        cached = _ps_cache.get(key)
        if cached is not None and now - cached[0] < _PS_CACHE_TTL_S:
            return cached[1]
    value = _fetch_ps_context_length(base_url, model, timeout=timeout)
    with _ps_cache_lock:
        _ps_cache[key] = (now, value)
    return value


def effective_window(
    *, num_ctx: int | None = None, base_url: str | None = None, model: str | None = None,
) -> tuple[int, str]:
    """The context window a call will actually run with, and where that came from.

    Priority, each step fail-open to the next:

    1. an explicit `num_ctx` the caller is about to send;
    2. the server's own report for a loaded model (`/api/ps`, only tried
       when `base_url` is given);
    3. `OLLAMA_CONTEXT_LENGTH` -- the env var the live server is tuned with;
    4. `DEFAULT_CONTEXT_WINDOW` -- Ollama's own hardcoded default.
    """
    if num_ctx:
        return int(num_ctx), "num_ctx"
    if base_url:
        ps_value = _ps_context_length(base_url, model)
        if ps_value:
            return ps_value, "api_ps"
    raw = os.environ.get("OLLAMA_CONTEXT_LENGTH", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value, "env"
        except ValueError:
            pass
    return DEFAULT_CONTEXT_WINDOW, "default"


def check_overflow(
    payload: dict,
    *,
    num_ctx: int | None = None,
    base_url: str | None = None,
    model: str | None = None,
    reserved_output: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
    site: str = "unknown",
) -> None:
    """Raise `ContextOverflow` if `payload` would not fit the effective window.

    Called right before a local call path would otherwise send `payload` to
    Ollama. Records `CHZ-FO-LOCAL-CTX-OVERFLOW` (visible in `llm-router
    doctor` / `status` via the existing fail-open counters, see
    `failopen.render_report`) and raises so the caller escalates instead of
    letting Ollama silently drop the front of the prompt.
    """
    estimated = estimate_payload_tokens(payload)
    window, source = effective_window(num_ctx=num_ctx, base_url=base_url, model=model)
    budget = window - reserved_output
    if estimated <= budget:
        return
    from llm_router import failopen

    detail = (f"site={site} model={model} estimated={estimated} window={window} "
              f"source={source} reserved_output={reserved_output}")
    failopen.record("CHZ-FO-LOCAL-CTX-OVERFLOW", detail=detail)
    raise ContextOverflow(
        f"estimated {estimated} prompt tokens (+{reserved_output} reserved for the reply) "
        f"exceeds the {window}-token window (source={source}); escalating instead of letting "
        "Ollama silently drop the front of the prompt"
    )


def check_truncated(
    prompt_eval_count: int | None,
    estimated_tokens: int,
    window: int,
    *,
    site: str = "unknown",
    model: str | None = None,
) -> bool:
    """True when a completed call's own numbers say Ollama truncated the prompt anyway.

    Two independent signatures, either is enough: `prompt_eval_count` landed
    AT the window (the server processed exactly what fit and nothing past
    it), or it came back well under what was actually sent
    (`--context-shift` discarding the front changes what the server reports
    it evaluated). The 0.6 threshold is deliberately generous -- a model's
    own chat template and tool-schema rendering legitimately differs from
    this module's chars/token estimate by a wide margin, and a false
    positive here (an operator chasing a phantom) is a lot cheaper than a
    false negative (a real truncation going unrecorded).

    Records `CHZ-FO-LOCAL-CTX-TRUNCATED` when it fires. Does not raise and
    does not decide what the caller does with the answer. Caveat the callers
    must respect: the "short" signature is weak evidence, because a warm
    KV cache makes Ollama report a low `prompt_eval_count` for an intact
    prompt. `direct_executor.call_ollama` therefore degrades the answer only
    when `prompt_eval_count >= window`; the short signature is recorded for
    `doctor` / `status` and nothing else.
    """
    if prompt_eval_count is None or prompt_eval_count <= 0:
        return False
    hit_window = prompt_eval_count >= window
    short = estimated_tokens > 0 and prompt_eval_count < estimated_tokens * 0.6
    if not (hit_window or short):
        return False
    from llm_router import failopen

    failopen.record(
        "CHZ-FO-LOCAL-CTX-TRUNCATED",
        detail=(f"site={site} model={model} prompt_eval_count={prompt_eval_count} "
                f"estimated={estimated_tokens} window={window}"),
    )
    return True


def reset_cache() -> None:
    """Drop the cached `/api/ps` reading. Test helper; never production."""
    with _ps_cache_lock:
        _ps_cache.clear()
