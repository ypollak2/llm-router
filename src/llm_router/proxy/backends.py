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
                  channel and ``anthropic/`` is the pass-through path itself.
"""

from __future__ import annotations

import copy
import importlib
from typing import Callable, Protocol

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


def _trim_unused_tools(body: dict) -> dict:
    """Keep only tools the conversation has already used plus the core file and
    shell tools. Unmeasured; offered as a lever, off by default."""
    core = {"Read", "Edit", "Write", "Bash", "Glob", "Grep"}
    used = set()
    for m in body.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), list):
            used |= {b.get("name") for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"}
    keep = core | used
    out = dict(body)
    out["tools"] = [t for t in body.get("tools") or [] if isinstance(t, dict) and t.get("name") in keep]
    return out


TRIMS: dict[str, Trim] = {
    "none": lambda body: body,
    # The spike's setting (2026-09-28, 6/6 golden tasks, 0/19 validation
    # failures): tool descriptions cut to 2,000 chars each.
    "tool-desc-2000": _trim_tool_descriptions(2000),
    "unused-tools": _trim_unused_tools,
}
DEFAULT_TRIM = "tool-desc-2000"


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


def apply_trims(body: dict, trims: list[Trim]) -> dict:
    out = copy.deepcopy(body)
    for trim in trims:
        out = trim(out)
    return out


# ── backends ─────────────────────────────────────────────────────────────────


class Backend(Protocol):
    async def complete(self, body: dict, timeout_s: float) -> tuple[dict | None, str | None, dict]:
        """``(anthropic_message | None, error | None, backend_usage)``."""


class OllamaBackend:
    """Ollama ``/api/chat`` with native tool calls, thinking off."""

    def __init__(self, model: str, client, *, base_url: str, num_ctx: int) -> None:
        self.model = model.split("/", 1)[1] if model.startswith("ollama/") else model
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx

    async def complete(self, body: dict, timeout_s: float) -> tuple[dict | None, str | None, dict]:
        payload = to_ollama(body, self.model, num_ctx=self.num_ctx)
        r = await self.client.post(self.base_url + "/api/chat", json=payload, timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
        usage = {"prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count"),
                 "prompt_eval_s": round((data.get("prompt_eval_duration") or 0) / 1e9, 2),
                 "eval_s": round((data.get("eval_duration") or 0) / 1e9, 2)}
        message, err = from_ollama(data, body)
        return message, err, usage


BACKENDS: dict[str, type] = {"ollama/": OllamaBackend}


def tool_capable(model: str) -> bool:
    return any(model.startswith(prefix) for prefix in BACKENDS)


# ── model choice ─────────────────────────────────────────────────────────────

_chain_cache: dict[tuple[str, str], list[str]] = {}


async def policy_chain(text: str) -> tuple[str, str, list[str]]:
    """``(task_type, complexity, chain)`` from the router's own policy."""
    from llm_router.classify import GATEWAY_POLICY, classify_signals
    from llm_router.config import get_config
    from llm_router.profiles import complexity_to_profile
    from llm_router.router import _build_and_filter_chain

    sig = classify_signals(text, GATEWAY_POLICY)
    key = (sig.task_type.value, sig.complexity.value)
    if key not in _chain_cache:
        c = sig.complexity
        _chain_cache[key] = await _build_and_filter_chain(
            sig.task_type, complexity_to_profile(c), None, c, c, get_config())
    return key[0], key[1], _chain_cache[key]


async def choose_model(text: str, pinned: str | None) -> dict:
    """``{"task_type", "complexity", "chain_head", "model"}``; ``model`` is None
    when the policy keeps the call on Claude."""
    task, cx, chain = await policy_chain(text)
    model = next((m for m in chain if tool_capable(m)), None)
    # A pin changes WHICH model serves, never WHETHER: the policy's decision to
    # keep a step on Claude (no tool-capable entry) stands.
    if model is not None and pinned:
        model = pinned if tool_capable(pinned) else None
    return {"task_type": task, "complexity": cx, "chain_head": chain[:4], "model": model}
