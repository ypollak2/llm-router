"""Multi-protocol HTTP gateway — route ANY LLM client through LLM Router.

One server, several wire formats, all backed by the same router
(``build_chain`` + ``execute_chain``). A client enrolls by pointing its base URL
here — no code change, whichever SDK it speaks:

    OpenAI     POST /v1/chat/completions   OPENAI_BASE_URL=http://127.0.0.1:17900/v1
    Anthropic  POST /v1/messages           ANTHROPIC_BASE_URL=http://127.0.0.1:17900
    Ollama     POST /api/chat,/api/generate   point OLLAMA_BASE_URL/host here

Every call is metered into ``~/.llm-router/usage.db`` + ``savings_log.jsonl`` like the
in-editor hook path, so external agents finally show up in the ledger (Surface-C fix).
Bind host/port come from the active preset (see ``llm_router.presets``).

Run:  llm_router gateway   (or: python -m llm_router.gateway)
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel



def _load_dotenv() -> None:
    """Load provider API keys from llm_router's .env files into os.environ.

    A launchd/systemd-spawned gateway has a bare environment, so without this it
    has no GEMINI_API_KEY/etc. and every cloud-model route fails. Mirrors the
    hook's loader (no override of existing env)."""
    for env_path in (Path.home() / ".llm-router" / ".env", Path.home() / ".env"):
        if not env_path.exists():
            continue
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            pass


_load_dotenv()  # at import, before any routing

app = FastAPI(title="LLM Router Gateway", version="2")


@app.middleware("http")
async def _guard_cross_origin(request, call_next):
    """CHZ-SEC-04: block browser CSRF / DNS-rebinding on this loopback gateway.

    Every route here can trigger a real (possibly paid) model call. The gateway
    binds loopback, but a malicious web page could POST to http://127.0.0.1:PORT
    from the user's browser (CSRF), or use DNS-rebinding (attacker.com -> 127.0.0.1)
    to reach it. Both carry a non-loopback ``Host`` and/or a cross-site
    ``Origin``/``Referer``; legitimate clients that set OPENAI_BASE_URL /
    ANTHROPIC_BASE_URL (curl, the OpenAI/Anthropic SDKs) send a loopback Host and
    no browser Origin, so they are unaffected.
    """
    from starlette.responses import JSONResponse

    from llm_router.route_server import is_forbidden_cross_origin

    if is_forbidden_cross_origin(request.headers):
        return JSONResponse(
            {"error": "forbidden: cross-origin request rejected"}, status_code=403
        )
    return await call_next(request)


# Gateway mode fronts host tools such as Codex/Cursor/Pi. Calling Codex or
# Gemini CLI again from inside the gateway can recurse or fail under launchd's
# trimmed PATH, so keep subprocess host backends out unless an operator opts in.
os.environ.setdefault("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex,gemini_cli")

def _classify(prompt: str) -> tuple[str, str]:
    # Unified engine (llm_router.classify), gateway policy — richer task_type than the
    # old code/analyze regex; same 400/2000 length tiers + analyze low-signal default.
    from llm_router.classify import GATEWAY_POLICY, classify_signals

    s = classify_signals(prompt, GATEWAY_POLICY)
    return s.task_type.value, s.complexity.value


class _ModelRef:
    """Tiny ``.provider`` / ``.model`` holder so the wire-format endpoints can keep
    formatting ``f"{r.model.provider}/{r.model.model}"`` unchanged."""

    __slots__ = ("provider", "model")

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model


class _RoutedResult:
    """Adapts :func:`route_payload`'s JSON dict to the ``.text`` /
    ``.model.provider`` / ``.model.model`` / ``.input_tokens`` / ``.output_tokens``
    shape the gateway's wire-format endpoints expect."""

    __slots__ = ("text", "input_tokens", "output_tokens", "cost_usd", "model")

    def __init__(self, d: dict) -> None:
        self.text = d.get("text", "")
        self.input_tokens = d.get("input_tokens", 0) or 0
        self.output_tokens = d.get("output_tokens", 0) or 0
        self.cost_usd = d.get("cost_usd", 0.0) or 0.0
        prov = d.get("provider") or ""
        mdl = d.get("model") or ""
        # route_and_call may return model as "provider/model" or bare — normalize
        # to the bare model name so f"{provider}/{model}" doesn't double the prefix.
        bare = mdl.split("/", 1)[1] if "/" in mdl and mdl.split("/", 1)[0] == prov else mdl
        self.model = _ModelRef(prov, bare)


async def _route(prompt: str, task_type: str | None, complexity: str | None,
                 prefer_model: str | None = None):
    """Shared core for every wire-format endpoint: classify (if needed) → route
    through LLM Router's FULL router and adapt the result.

    Routes via :func:`llm_router.route_server.route_payload` → ``route_and_call``, so
    gateway traffic gets the same budget caps, caching, paid-spend cap, and cost
    logging as the native ``/route`` endpoint (and the standalone route server).
    ``prefer_model`` (the OpenAI ``model`` field) requests a specific tier.
    """
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="no prompt content")
    if not task_type or not complexity:
        _t, _c = _classify(prompt)
        task_type, complexity = task_type or _t, complexity or _c

    from llm_router.route_server import route_payload_async
    try:
        out = await route_payload_async({
            "prompt": prompt,
            "task_type": task_type,
            "complexity": complexity,
            "model": prefer_model,
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM Router routing failed: {e}")
    return _RoutedResult(out)


def _flatten(messages: list) -> str:
    parts = []
    for m in messages or []:
        role = m.get("role", "user") if isinstance(m, dict) else getattr(m, "role", "user")
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(c, list):  # content-parts (OpenAI/Anthropic vision format)
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        if c:
            parts.append(f"{role}: {c}")
    return "\n".join(parts)


def _flatten_responses_input(value) -> str:
    """Flatten OpenAI Responses ``input`` into the prompt text LLM Router routes.

    Supports the common shapes:
      - string input
      - list of message dicts with content as string
      - list of content parts like {"type": "input_text", "text": "..."}
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return str(value or "")

    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            if item:
                parts.append(str(item))
            continue

        role = item.get("role", "user")
        content = item.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("text")
            )
        else:
            text = str(content or "")
        if text:
            parts.append(f"{role}: {text}")
    return "\n".join(parts)


# ── health / discovery ───────────────────────────────────────────────────────
def _runtime_python() -> dict:
    """Interpreter identity of the *running* daemon — lets ``llm_router doctor``
    detect an orphaned interpreter (venv rebuilt under a different Python
    while the daemon kept running; lazy imports then 500)."""
    v = sys.version_info
    return {
        "python": f"{v.major}.{v.minor}.{v.micro}",
        "executable": sys.executable,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "llm_router-gateway",
            "formats": ["openai", "responses", "anthropic", "ollama", "route"],
            **_runtime_python()}


@app.get("/health")  # alias — parity with the standalone route server
def health() -> dict:
    return {"ok": True, **_runtime_python()}


# ── Native: POST /route (parity with the zero-dep route_server) ───────────────
@app.post("/route")
async def route(payload: dict) -> dict:
    """Minimal native routing endpoint — same contract as ``llm_router.route_server``.

    Body: ``{"prompt", "complexity"?, "system"?, "task_type"?, "max_tokens"?,
    "temperature"?, "model"?}`` → ``{"text","model","provider","cost_usd",
    "input_tokens","output_tokens","complexity"}``. Goes through the same
    ``route_payload`` core as every other endpoint.
    """
    from llm_router.route_server import route_payload_async
    try:
        return await route_payload_async(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"route failed: {e}")


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list",
            "data": [{"id": "llm_router-auto", "object": "model", "owned_by": "llm_router"}]}


@app.get("/api/tags")  # Ollama model-list shape
def ollama_tags() -> dict:
    return {"models": [{"name": "llm_router-auto", "model": "llm_router-auto"}]}


# ── OpenAI: POST /v1/chat/completions ────────────────────────────────────────
class _OAIRequest(BaseModel):
    model: str | None = None
    messages: list
    task_type: str | None = None
    complexity: str | None = None


@app.post("/v1/chat/completions")
async def openai_chat(req: _OAIRequest) -> dict:
    r = await _route(_flatten(req.messages), req.task_type, req.complexity, prefer_model=req.model)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": f"{r.model.provider}/{r.model.model}",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": r.text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": r.input_tokens, "completion_tokens": r.output_tokens,
                  "total_tokens": r.input_tokens + r.output_tokens},
    }


# ── OpenAI Responses: POST /v1/responses ────────────────────────────────────
class _ResponsesRequest(BaseModel):
    model: str | None = None
    input: object
    instructions: str | None = None
    task_type: str | None = None
    complexity: str | None = None


@app.post("/v1/responses")
async def openai_responses(req: _ResponsesRequest) -> dict:
    prompt = _flatten_responses_input(req.input)
    if req.instructions:
        prompt = f"system: {req.instructions}\n{prompt}"
    r = await _route(prompt, req.task_type, req.complexity, prefer_model=req.model)
    output_id = f"msg_{uuid.uuid4().hex[:24]}"
    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": f"{r.model.provider}/{r.model.model}",
        "output": [
            {
                "id": output_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": r.text,
                        "annotations": [],
                    }
                ],
            }
        ],
        "output_text": r.text,
        "usage": {
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "total_tokens": r.input_tokens + r.output_tokens,
        },
    }


# ── Anthropic: POST /v1/messages ─────────────────────────────────────────────
class _AnthropicRequest(BaseModel):
    model: str | None = None
    messages: list
    system: str | None = None
    max_tokens: int | None = None


@app.post("/v1/messages")
async def anthropic_messages(req: _AnthropicRequest) -> dict:
    prompt = (f"system: {req.system}\n" if req.system else "") + _flatten(req.messages)
    r = await _route(prompt, None, None)
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": f"{r.model.provider}/{r.model.model}",
        "content": [{"type": "text", "text": r.text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": r.input_tokens, "output_tokens": r.output_tokens},
    }


# ── Ollama: POST /api/chat  and  POST /api/generate ──────────────────────────
class _OllamaChat(BaseModel):
    model: str | None = None
    messages: list


class _OllamaGenerate(BaseModel):
    model: str | None = None
    prompt: str


@app.post("/api/chat")
async def ollama_chat(req: _OllamaChat) -> dict:
    r = await _route(_flatten(req.messages), None, None)
    return {
        "model": f"{r.model.provider}/{r.model.model}",
        "message": {"role": "assistant", "content": r.text},
        "done": True,
        "prompt_eval_count": r.input_tokens, "eval_count": r.output_tokens,
    }


@app.post("/api/generate")
async def ollama_generate(req: _OllamaGenerate) -> dict:
    r = await _route(req.prompt, None, None)
    return {
        "model": f"{r.model.provider}/{r.model.model}",
        "response": r.text,
        "done": True,
        "prompt_eval_count": r.input_tokens, "eval_count": r.output_tokens,
    }


def main() -> None:
    import uvicorn

    from llm_router import presets
    from llm_router.net_bind import refuse_public_bind_or_exit

    host, port = presets.bind()
    # RED6-04: this app has NO request authentication -- a whole-file grep for
    # `Depends(` returns zero, and _guard_cross_origin is a browser
    # CSRF/DNS-rebinding check that by its own docstring lets CLI/SDK traffic
    # through. Binding it publicly therefore exposes paid model calls to anything
    # that can reach the port.
    refuse_public_bind_or_exit(host, component="gateway")
    print(f"LLM Router Gateway [{presets.active_name()}] → http://{host}:{port}")
    print("  OpenAI    /v1/chat/completions   |  Anthropic /v1/messages   |  Ollama /api/chat,/api/generate")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
