"""The glue ``proxy/server.py`` calls when ``--local-agent`` is on.

Kept out of the server so the server change is a few lines: gate the step
(``capability.can_serve_locally``), compact it (``compact.compact``), and, on
an edit-shaped reply, run the validated edit protocol
(``capability.run_edit_protocol``) against the same Ollama model.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

from llm_router.local_agent import LocalAgentConfig
from llm_router.local_agent import capability, compact, constrain

EMBED_CACHE_NAME = "local_agent_tool_embeddings.json"


def _default_cache_path() -> Path:
    from llm_router import paths

    return paths.state_path(EMBED_CACHE_NAME)


class LocalAgent:
    def __init__(self, cfg: LocalAgentConfig, *, http=None, ollama_url: Callable[[], str] | None = None,
                 embed: compact.EmbedFn | None = None, breaker_fn=None,
                 cache_path: Path | None = None) -> None:
        self.cfg = cfg
        if embed is None and http is not None and ollama_url is not None:
            embed = _lazy_embedder(http, ollama_url, cfg.embed_model)
        self.retriever = compact.ToolRetriever(
            embed, cfg.embed_model, cache_path if cache_path is not None else _default_cache_path())
        self.breaker = capability.BreakerCache(fn=breaker_fn)

    async def gate(self, body: dict, enabled_steps, choice: dict) -> capability.Decision:
        step = capability.Step(body=body, enabled_steps=enabled_steps,
                               task_type=choice.get("task_type"), model=choice.get("model"))
        return await capability.can_serve_locally(step, self.breaker)

    async def prepare(self, body: dict) -> tuple[dict, dict]:
        return await compact.compact(body, self.retriever, self.cfg)

    async def edit_step(self, decision: capability.Decision, message: dict, body: dict, backend,
                        deadline: float) -> tuple[dict | None, str | None, dict]:
        return await capability.run_edit_protocol(decision, message, body, generator_for(backend), deadline)


def _lazy_embedder(http, ollama_url: Callable[[], str], model: str) -> compact.EmbedFn:
    async def embed(texts: list[str]) -> list[list[float]]:
        return await compact.ollama_embedder(http, ollama_url(), model)(texts)
    return embed


def generator_for(backend) -> capability.GenerateFn:
    """A plain-text completion on the step's own backend: its
    ``generate_text`` if it has one (tests), else Ollama ``/api/chat`` with the
    backend's model, URL, context size and keep-alive, thinking off, reply
    constrained to the edit-pairs JSON schema (``constrain.py``)."""
    if hasattr(backend, "generate_text"):
        return backend.generate_text

    async def generate(system: str, prompt: str, num_predict: int, timeout_s: float):
        payload = {"model": backend.model, "stream": False, "think": False,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                   "options": {"num_ctx": backend.num_ctx, "num_predict": num_predict, "temperature": 0}}
        if getattr(backend, "keep_alive", None) is not None:
            payload["keep_alive"] = backend.keep_alive
        t0 = time.monotonic()
        try:
            # Constrain the reply to the edit-pairs schema (constrain.py); an
            # older server that refuses a schema ``format`` gets the request
            # again, unconstrained, exactly as it was sent before.
            r = await asyncio.wait_for(
                backend.client.post(backend.base_url + "/api/chat",
                                    json=constrain.with_edit_format(payload), timeout=timeout_s),
                timeout=timeout_s)
            if constrain.is_format_rejection(r.status_code):
                r = await asyncio.wait_for(
                    backend.client.post(backend.base_url + "/api/chat", json=payload, timeout=timeout_s),
                    timeout=max(0.001, timeout_s - (time.monotonic() - t0)))
            r.raise_for_status()
            data = r.json()
        except Exception:  # noqa: BLE001 - an empty attempt is fed back / counted as a rejection
            return None, {"seconds": round(time.monotonic() - t0, 2)}
        if data.get("done_reason") == "length":
            return None, {"prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}
        return ((data.get("message") or {}).get("content") or None,
                {"prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")})

    return generate
