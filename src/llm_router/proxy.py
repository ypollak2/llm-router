"""OpenAI-compatible HTTP proxy that routes calls through llm-router's classifier.

Aider (and any other tool with `--openai-api-base` / `OPENAI_API_BASE` support)
points at this proxy. Every request is:

  1. Parsed to extract the user's prompt (last user message in messages[]).
  2. Classified via the same heuristics auto-route.py uses (lightweight).
  3. Routed to the cheapest capable model via litellm.acompletion.
  4. Logged to aider_usage table via log_aider_usage.

Start with: `llm-router-proxy --port 8765`
Then: `export OPENAI_API_BASE=http://localhost:8765/v1` and run `aider`.

v9.4.0.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import litellm
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger(__name__)

# Routing knobs (env-tunable):
#   LLM_ROUTER_PROXY_PORT — listen port (default 8765)
#   LLM_ROUTER_PROXY_HOST — listen host (default 127.0.0.1)
#   LLM_ROUTER_PROXY_DEFAULT — fallback model when classifier can't decide
#   LLM_ROUTER_PROXY_QUERY_MODEL — model for query/simple tasks
#   LLM_ROUTER_PROXY_CODE_MODEL — model for code tasks
#   LLM_ROUTER_PROXY_COMPLEX_MODEL — model for complex tasks
_DEFAULTS = {
    "query":   os.environ.get("LLM_ROUTER_PROXY_QUERY_MODEL",   "gemini/gemini-2.5-flash"),
    "code":    os.environ.get("LLM_ROUTER_PROXY_CODE_MODEL",    "gpt-4o"),
    "analyze": os.environ.get("LLM_ROUTER_PROXY_CODE_MODEL",    "gpt-4o"),
    "complex": os.environ.get("LLM_ROUTER_PROXY_COMPLEX_MODEL", "openai/o3"),
}

# Heuristic words → task type. Cheap classifier inline (no Ollama dependency for proxy MVP).
_QUERY_PATTERNS = ("what is", "what's", "define", "explain", "summarize", "tldr", "how does")
_CODE_PATTERNS  = ("implement", "fix", "refactor", "write a function", "add a test", "build", "generate code")
_COMPLEX_PATTERNS = ("design the architecture", "system design", "compare and analyze", "deep dive", "audit")


def _classify_prompt(prompt: str) -> tuple[str, str]:
    """Return (task_type, complexity). Lightweight heuristic — proxy MVP."""
    p = prompt.lower()
    if any(s in p for s in _COMPLEX_PATTERNS):
        return ("code" if "code" in p or "refactor" in p else "analyze", "complex")
    if any(s in p for s in _CODE_PATTERNS):
        return ("code", "moderate")
    if any(s in p for s in _QUERY_PATTERNS):
        return ("query", "simple")
    if len(p.split()) <= 8:
        return ("query", "simple")
    return ("code", "moderate")


def _pick_model(task_type: str, complexity: str) -> str:
    if complexity == "complex":
        return _DEFAULTS["complex"]
    return _DEFAULTS.get(task_type, _DEFAULTS["code"])


def _extract_last_user_prompt(messages: list[dict]) -> str:
    """Last `role: user` content. Aider sends a full message stack each turn."""
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                # OpenAI multi-modal content blocks
                return " ".join(
                    blk.get("text", "") for blk in c
                    if isinstance(blk, dict) and blk.get("type") == "text"
                )
    return ""


def create_app() -> FastAPI:
    app = FastAPI(title="llm-router-proxy", version="9.4.0")

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "service": "llm-router-proxy", "version": "9.4.0"}

    @app.get("/v1/models")
    async def list_models() -> dict:
        """Aider may probe this. Return a few synthetic model entries."""
        return {
            "object": "list",
            "data": [
                {"id": "llm-router/auto", "object": "model", "owned_by": "llm-router"},
                {"id": "llm-router/cheap", "object": "model", "owned_by": "llm-router"},
                {"id": "llm-router/balanced", "object": "model", "owned_by": "llm-router"},
                {"id": "llm-router/strong", "object": "model", "owned_by": "llm-router"},
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        from llm_router.cost import log_aider_usage

        body = await request.json()
        messages = body.get("messages", [])
        requested_model = body.get("model", "llm-router/auto")
        prompt = _extract_last_user_prompt(messages)

        # Classify + route
        task_type, complexity = _classify_prompt(prompt)
        # User can pin a tier via the requested model name
        rm = requested_model.lower()
        if rm.endswith("/cheap"):
            task_type, complexity = "query", "simple"
        elif rm.endswith("/strong"):
            task_type, complexity = "code", "complex"

        routed_model = _pick_model(task_type, complexity)
        baseline_model = body.get("metadata", {}).get(
            "aider_baseline", os.environ.get("AIDER_BASELINE", "gpt-4o")
        )

        log.info(
            "PROXY ROUTE: prompt_len=%d task=%s/%s requested=%s → routed=%s",
            len(prompt), task_type, complexity, requested_model, routed_model,
        )

        # Streaming? Aider usually streams. Forward stream=true to litellm.
        stream = bool(body.get("stream", False))

        # Build litellm call. We drop the requested model (it was a routing hint)
        # and substitute the routed model.
        call_kwargs = {
            "model": routed_model,
            "messages": messages,
            "stream": stream,
            **{k: v for k, v in body.items()
               if k not in ("model", "messages", "stream", "metadata")},
        }

        if stream:
            async def _stream_gen():
                acc_in = 0
                acc_out = 0
                async for chunk in await litellm.acompletion(**call_kwargs):
                    # Best-effort usage extraction from final chunk
                    u = getattr(chunk, "usage", None)
                    if u is not None:
                        acc_in = int(getattr(u, "prompt_tokens", 0) or 0)
                        acc_out = int(getattr(u, "completion_tokens", 0) or 0)
                    yield f"data: {json.dumps(chunk.model_dump())}\n\n"
                yield "data: [DONE]\n\n"
                # Log after stream completes
                try:
                    await log_aider_usage(
                        model=routed_model,
                        tokens_used=0,
                        complexity=complexity,
                        task_type=task_type,
                        input_tokens=acc_in,
                        output_tokens=acc_out,
                        baseline_model=baseline_model,
                    )
                except Exception as e:
                    log.warning("aider_usage log failed: %s", e)

            return StreamingResponse(_stream_gen(), media_type="text/event-stream")

        # Non-streaming
        try:
            resp = await litellm.acompletion(**call_kwargs)
        except Exception as e:
            log.error("litellm error: %s", e)
            raise HTTPException(status_code=502, detail=f"upstream error: {e}")

        d = resp.model_dump()
        usage = d.get("usage", {}) or {}
        try:
            await log_aider_usage(
                model=routed_model,
                tokens_used=0,
                complexity=complexity,
                task_type=task_type,
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
                cache_creation_input_tokens=int(
                    usage.get("cache_creation_input_tokens", 0) or 0
                ),
                cache_read_input_tokens=int(
                    usage.get("cache_read_input_tokens", 0) or 0
                ),
                baseline_model=baseline_model,
            )
        except Exception as e:
            log.warning("aider_usage log failed: %s", e)
        return JSONResponse(d)

    return app


def main() -> None:
    """CLI entry point: `llm-router-proxy [--port N] [--host H]`."""
    import argparse
    parser = argparse.ArgumentParser(description="llm-router OpenAI-compatible proxy")
    parser.add_argument("--port", type=int, default=int(os.environ.get("LLM_ROUTER_PROXY_PORT", 8765)))
    parser.add_argument("--host", default=os.environ.get("LLM_ROUTER_PROXY_HOST", "127.0.0.1"))
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    log.info("llm-router-proxy starting on http://%s:%d", args.host, args.port)
    log.info("Point Aider at this proxy:  export OPENAI_API_BASE=http://%s:%d/v1", args.host, args.port)

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
