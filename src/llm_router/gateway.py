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

import json
import os
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from llm_router import paths
from llm_router.route_server import is_auto_model



def _load_dotenv() -> None:
    """Load provider API keys from llm_router's .env files into os.environ.

    A launchd/systemd-spawned gateway has a bare environment, so without this it
    has no GEMINI_API_KEY/etc. and every cloud-model route fails. Mirrors the
    hook's loader (no override of existing env)."""
    for env_path in (paths.state_path(".env"), Path.home() / ".env"):
        if paths.regular_file_or_none(env_path) is None:
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


# ── M-08: per-request authentication ────────────────────────────────────────
#
# Every route here can trigger a real, billed model call, and the audit found no
# per-request auth at all -- one grep hit, a comment acknowledging the gap. The
# bind IS gated (`net_bind.refuse_public_bind_or_exit`, loopback by default) and
# `_guard_cross_origin` above blocks browser CSRF and DNS rebinding, so this is
# not an open port. But any OTHER LOCAL PROCESS -- a malicious dependency,
# another user on a shared box, a compromised extension -- can still spend money
# through it.
#
# WHY THIS IS OPT-IN. Making a token mandatory would break every existing user on
# upgrade: clients set OPENAI_BASE_URL / ANTHROPIC_BASE_URL and send no
# Authorization header. A remediation that silently stops working traffic is a
# worse outcome than the gap it closes, so enforcement turns on when an operator
# provides a token and stays off otherwise -- with the exposure logged once at
# startup rather than left unsaid.
#
# `commands/sse.py` already requires Bearer on every request; this is the same
# contract, applied to the surface that actually spends money.
# Read as a LITERAL below, not through this constant. `env_registry`'s
# validation deliberately scans the AST for `os.environ.get("NAME")` as
# INDEPENDENT ground truth against the hand-written registry -- a name passed as
# a variable is invisible to it and reads as "declared but never read".
# `hooks/tool_intercept.py` records the same requirement.
_GATEWAY_TOKEN_ENV = "LLM_ROUTER_GATEWAY_TOKEN"


def _gateway_token_file():
    from llm_router import paths

    return paths.state_path("gateway.token")


def gateway_token() -> str | None:
    """The configured gateway token, or None when auth is not enabled.

    Env wins over the file so a container can inject one without writing state.
    Never creates the file: a token that appears by itself would enable auth on
    upgrade and break the traffic this is careful not to break.
    """
    import os as _os

    env = _os.environ.get("LLM_ROUTER_GATEWAY_TOKEN", "").strip()
    if env:
        return env
    try:
        f = _gateway_token_file()
        if f.exists():
            tok = f.read_text(encoding="utf-8").strip()
            return tok or None
    except OSError:
        return None
    return None


def _check_gateway_auth(headers) -> None:
    """401 unless the request carries the configured bearer token.

    No-op when no token is configured. Constant-time comparison: a timing oracle
    on a loopback socket is cheap to exploit from the same machine, which is
    exactly the attacker this defends against.
    """
    import secrets as _secrets

    expected = gateway_token()
    if not expected:
        return
    supplied = (headers.get("authorization") or "").strip()
    prefix = "bearer "
    if supplied[:len(prefix)].lower() != prefix:
        raise HTTPException(
            status_code=401,
            detail="this gateway requires Authorization: Bearer <token>",
        )
    if not _secrets.compare_digest(supplied[len(prefix):].strip(), expected):
        raise HTTPException(status_code=401, detail="invalid gateway token")


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
    # M-08. Applied in the middleware so it covers EVERY route, including ones
    # added later. A per-handler check is how a new endpoint ships unguarded.
    try:
        _check_gateway_auth(request.headers)
    except HTTPException as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


def _classify(prompt: str) -> tuple[str, str]:
    # Unified engine (llm_router.classify), gateway policy — richer task_type than the
    # old code/analyze regex; same 400/2000 length tiers + analyze low-signal default.
    # PLAN v16 P1.6 / D-72: these rules route. The one engine only runs AFTER them, in shadow
    # (logged beside the rules class); LLM_ROUTER_ENGINE_MODE=decide lets it decide instead.
    from llm_router import engine_door
    from llm_router.classify import GATEWAY_POLICY, classify_signals

    d = engine_door.decision(prompt, door="gateway")
    if d is not None:
        return d.task_type, d.complexity
    s = classify_signals(prompt, GATEWAY_POLICY)
    out = s.task_type.value, s.complexity.value
    engine_door.shadow(prompt, door="gateway", rules_task=out[0], rules_complexity=out[1])
    return out


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

    __slots__ = ("text", "input_tokens", "output_tokens", "cost_usd", "model",
                 "tool_calls", "finish_reason")

    def __init__(self, d: dict) -> None:
        self.text = d.get("text", "")
        # A.2-t: the tool calls the model made (OpenAI shape) and its own stop reason.
        self.tool_calls = d.get("tool_calls") or []
        self.finish_reason = d.get("finish_reason") or ""
        self.input_tokens = d.get("input_tokens", 0) or 0
        self.output_tokens = d.get("output_tokens", 0) or 0
        self.cost_usd = d.get("cost_usd", 0.0) or 0.0
        prov = d.get("provider") or ""
        mdl = d.get("model") or ""
        # route_and_call may return model as "provider/model" or bare — normalize
        # to the bare model name so f"{provider}/{model}" doesn't double the prefix.
        bare = mdl.split("/", 1)[1] if "/" in mdl and mdl.split("/", 1)[0] == prov else mdl
        self.model = _ModelRef(prov, bare)


# "let the router choose" — never a real model name to be qualified. The set and
# its case-insensitive match live in route_server.is_auto_model (P0.6), so the
# gateway and the routing core cannot disagree about what "auto" means.


def _qualify_model(model: str | None, provider: str) -> str | None:
    """Qualify a bare wire-format model name with the endpoint's own provider.

    F-03 made the gateway honour the caller's `model` instead of discarding it,
    and forwarded it to `model_override` verbatim. But every real client sends a
    bare, provider-less name, because on its own API the provider is implied by the
    endpoint:

        OpenAI     model="gpt-4o"
        Anthropic  model="claude-haiku-4-5"
        Ollama     model="llama3.2"

    `model_override` requires `provider/model` and raises ValueError otherwise,
    which the gateway maps to HTTP 400 — so the fix that started honouring `model`
    also started rejecting every request from a normally-configured SDK client.
    Found by pointing the real anthropic SDK at a live gateway; no mocked test
    caught it because they all sent the already-qualified form.

    Each endpoint knows its own provider — that is what having separate endpoints
    means, and it is what makes the bare name unambiguous. An already-qualified
    name is passed through untouched.

    Sentinels meaning "you pick" are passed through untouched. Qualifying `auto`
    would turn "choose for me" into a demand for a model literally named
    `openai/auto`, which is the opposite of what the caller asked for.
    """
    if not model:
        return model
    if is_auto_model(model):
        return model
    return model if "/" in model else f"{provider}/{model}"


PROJECT_HEADER = "X-LLM-Router-Project"


def _resolve_project_scope(request, body_value: str | None = None) -> str | None:
    """The caller's project for OKF retrieval scope, or None to leave it alone.

    The gateway has the MCP server's defect for the same reason: long-lived
    process, cwd wherever it was launched, so retrieval scoped to it finds the
    wrong project or none. It has no roots capability, and the OpenAI/Anthropic/
    Ollama schemas are fixed, so four of the five endpoints cannot carry scope in
    the body. A header can, identically for every client.

    Precedence: header, then body, then None (env, then cwd, unchanged). The header
    wins because it is the more specific signal — a caller that set it on this
    request meant this request.

    C4, the access question. A header naming an arbitrary path is a read primitive
    into any project's knowledge store on this machine — the concern CHZ-AUD-024
    raised for session logs, arriving through a different door. Bounded by three
    facts: the gateway binds loopback, it already rejects cross-origin browser
    requests, and the store holds only paths and symbol names from the user's own
    repositories. So the default trusts a local caller but insists the path is
    real, and `LLM_ROUTER_PROJECT_ALLOWLIST` locks it down for anyone who wants
    that. A refused scope narrows context; it never fails the request, because
    losing retrieval is not worth losing the answer.
    """
    raw = ""
    try:
        raw = (request.headers.get(PROJECT_HEADER) or "").strip()
    except Exception:  # noqa: BLE001 — a header lookup must never break a route
        raw = ""
    if not raw:
        raw = (body_value or "").strip()
    if not raw:
        return None

    try:
        candidate = Path(raw).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return None
    # Must be a real directory: a typo would otherwise scope to nothing silently,
    # and a caller could probe for the existence of arbitrary paths by watching
    # for a different outcome.
    if not candidate.is_dir():
        return None

    allow = os.environ.get("LLM_ROUTER_PROJECT_ALLOWLIST", "").strip()
    if allow:
        for entry in allow.split(os.pathsep):
            entry = entry.strip()
            if not entry:
                continue
            try:
                root = Path(entry).expanduser().resolve()
            except Exception:  # noqa: BLE001
                continue
            # relative_to, not startswith: a string prefix would admit
            # `/srv/app-evil` under an allowlist of `/srv/app`, and `..` segments
            # are already gone because both sides are resolved.
            try:
                candidate.relative_to(root)
                break
            except ValueError:
                continue
        else:
            return None
    return str(candidate)


async def _route(prompt: str, task_type: str | None, complexity: str | None,
                 prefer_model: str | None = None, project_root: str | None = None,
                 classify_text: str | None = None, *,
                 system: str | None = None, max_tokens: int | None = None,
                 temperature: float | None = None, messages: list | None = None,
                 tools: list | None = None, tool_choice: object | None = None):
    """Shared core for every wire-format endpoint: classify (if needed) → route
    through LLM Router's FULL router and adapt the result.

    Routes via :func:`llm_router.route_server.route_payload` → ``route_and_call``, so
    gateway traffic gets the same budget caps, caching, paid-spend cap, and cost
    logging as the native ``/route`` endpoint (and the standalone route server).
    ``prefer_model`` (the OpenAI ``model`` field) requests a specific tier.

    ``classify_text`` — WHAT WE CLASSIFY vs WHAT WE SEND (audit 2026-09-22, T-03)
    ---------------------------------------------------------------------------
    ``prompt`` is what the model receives and MUST keep the system prompt and the
    whole history; truncating it would change the answer. ``classify_text`` is
    what the complexity heuristic reads, and it must not.

    ``_resolve_profile`` thresholds on character length (<600 simple /
    600-2000 moderate / >2000 complex). Every wire endpoint used to hand it the
    flattened transcript, so a 1.7KB system preamble pushed ``"hi"`` from SIMPLE
    to MODERATE and a 2KB one to COMPLEX — measured, not assumed::

        _classify(_flatten([user "hi"]))                  -> ('analyze', 'simple')
        _classify(_flatten([system <1.7KB>, user "hi"]))  -> ('analyze', 'moderate')

    That is the product's flagship path ("route ANY LLM client, no code change")
    systematically over-routing trivial turns to expensive models — the exact
    opposite of what it is for. Passing ``None`` keeps the old behaviour, which
    is what the native ``/route`` endpoint wants: there, the caller's prompt IS
    the ask.

    ``system``, ``max_tokens``, ``temperature`` (P0.6, R-AGT-1): the caller's own
    values, forwarded to ``route_and_call``. They used to be dropped here, so a
    client's ``max_tokens`` cap and ``temperature`` never reached the model, and
    its system prompt arrived as a ``system:`` line inside the user text.

    ``messages`` (PLAN v16 P1.1 door 7): the caller's conversation before
    ``prompt``. Every completion endpoint passes a list (empty when there is no
    history), so ``route_and_call`` builds the pack with ``door="gateway"`` from
    the caller's own turns and never from this process's session. ``None`` (the
    native ``/route``) keeps the old path.

    ``tools`` / ``tool_choice`` (A.2-t): the caller's function-calling request, already
    normalised to the OpenAI form by the endpoint (``_openai_tools``, ``_responses_tools``,
    ``_anthropic_tools``). The reply's ``tool_calls`` are on the result.
    """
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="no prompt content")
    if not task_type or not complexity:
        _t, _c = _classify(classify_text if (classify_text or "").strip() else prompt)
        task_type, complexity = task_type or _t, complexity or _c

    from llm_router.route_server import route_payload_async
    try:
        out = await route_payload_async({
            "prompt": prompt,
            "task_type": task_type,
            "complexity": complexity,
            "model": prefer_model,
            "project_root": project_root,
            "system": system,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM Router routing failed: {e}")
    return _RoutedResult(out)


def _latest_user_turn_from_responses_input(value) -> str:
    """``_latest_user_turn`` for the OpenAI Responses ``input`` shape (T-03).

    A bare string input IS the user's ask, so it returns unchanged. A list is
    scanned for the last ``user`` item; a list of raw content-parts with no
    roles at all falls back to the flattened text, since there is no system
    preamble mixed into it to exclude.
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return str(value or "")
    saw_role = False
    for item in reversed(value):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role is None:
            continue
        saw_role = True
        if role != "user":
            continue
        c = item.get("content")
        if isinstance(c, list):
            c = " ".join(part.get("text", "") for part in c if isinstance(part, dict))
        if c:
            return str(c)
    return "" if saw_role else _flatten_responses_input(value)


def _latest_user_turn(messages: list) -> str:
    """The text of the most recent ``user`` message — what the caller is ASKING.

    This is the classification input for every wire-format endpoint (T-03). A
    system preamble is the client's configuration, not the user's request, and a
    prior assistant turn is history; neither says anything about how hard THIS
    turn is, but both inflate the character count the complexity heuristic
    thresholds on.

    Returns ``""`` when there is no user turn at all, which makes the caller fall
    back to the full flattened prompt rather than classifying nothing — an empty
    classification input would silently take the ``simple`` branch for every
    request, which is the same bug pointed the other way.
    """
    for m in reversed(messages or []):
        role = m.get("role", "user") if isinstance(m, dict) else getattr(m, "role", "user")
        if role != "user":
            continue
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(c, list):  # content-parts (OpenAI/Anthropic vision format)
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        if c:
            return str(c)
    return ""


_SYSTEM_ROLES = ("system", "developer")


def _content_text(c) -> str:
    if isinstance(c, list):  # content-parts (OpenAI/Anthropic vision format)
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("text"))
    return str(c or "")


def _split_system(messages) -> tuple[str | None, object]:
    """Split ``system``/``developer`` turns out of a message list (P0.6).

    Returns ``(system_text, remaining_messages)``. The system text is forwarded
    as the real system prompt instead of a ``system:`` line in the user text.
    When nothing but system turns is present, nothing is split: the request has
    no other content to send, and routing it as before beats a 400.
    """
    if not isinstance(messages, list):
        return None, messages
    sys_parts, rest = [], []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role in _SYSTEM_ROLES:
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            text = _content_text(c)
            if text:
                sys_parts.append(text)
        else:
            rest.append(m)
    if not sys_parts or not rest:
        return None, messages
    return "\n\n".join(sys_parts), rest


def _join_system(*parts: str | None) -> str | None:
    joined = "\n\n".join(p for p in parts if p)
    return joined or None


# Tool-call history the context pack renders itself (``context_pack._from_message``):
# Responses API typed items and OpenAI ``tool`` results. They pass through unchanged;
# rewriting them here used to drop the function calls and their outputs.
_TOOL_ITEM_TYPES = frozenset({
    "function_call", "function_call_output", "custom_tool_call",
    "custom_tool_call_output", "local_shell_call", "local_shell_call_output",
})


def _history_turn(item) -> dict | None:
    """One history item as a turn ``build_pack`` accepts, or None.

    ``user`` / ``assistant`` messages (including OpenAI ``tool_calls`` and
    Anthropic ``tool_use`` / ``tool_result`` blocks), OpenAI ``tool`` results and
    Responses API tool items pass unchanged: the pack renders their tool blocks as
    text. A bare string (the Responses API allows them in ``input``) becomes a user
    turn; any other role (legacy ``function``) or a role-less content part becomes
    a user turn labelled with that role, since providers reject unknown roles.
    """
    if isinstance(item, str):
        return {"role": "user", "content": item} if item.strip() else None
    if not isinstance(item, dict):
        return None
    role = item.get("role")
    if role in ("user", "assistant") and ("content" in item or item.get("tool_calls")):
        return item
    if role == "tool" or item.get("type") in _TOOL_ITEM_TYPES:
        return item
    text = _content_text(item.get("content", item.get("text")))
    if not text.strip():
        return None
    return {"role": "user", "content": f"[{role}] {text}" if role else text}


_TOOL_OUTPUT_TYPES = frozenset({
    "function_call_output", "custom_tool_call_output", "local_shell_call_output",
})


def _is_tool_output(item) -> bool:
    """An OpenAI ``tool`` message or a Responses tool-output item."""
    return isinstance(item, dict) and (
        item.get("role") == "tool" or item.get("type") in _TOOL_OUTPUT_TYPES)


def _trailing_request(last) -> str:
    """The request text of a trailing ``user`` turn, or ``""`` if it is not one.

    A user turn with text is that text, verbatim. A user turn that carries tool
    results (Anthropic ``tool_result`` blocks, errors included, with or without
    text beside them) is the turn as the context pack renders it, so the tool
    output is the request instead of being flattened away with the history."""
    if not isinstance(last, dict) or last.get("role", "user") != "user" or \
            last.get("type") not in (None, "message"):
        return ""
    content = last.get("content")
    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        from llm_router.context_pack import render_message

        return render_message(last)
    text = _content_text(content)
    return text if text.strip() else ""


def _request_and_history(messages, flatten=None) -> tuple[str, list]:
    """``(request, history)`` for one completion request (P1.1 door 7).

    The request is the trailing turn (``_trailing_request``), sent verbatim, or the
    trailing run of tool outputs (OpenAI ``tool`` messages / Responses ``*_output``
    items, one per parallel call) rendered as the context pack renders them. The
    turns before it are the history ``build_pack(door="gateway")`` packs, tool
    blocks included. Without a trailing request (an assistant prefill, a
    system-only request, raw content-parts) there is nothing to separate: the old
    flattened prompt is sent with no history, so nothing the caller sent is lost.
    """
    if isinstance(messages, list) and messages:
        start = len(messages)
        while start > 0 and _is_tool_output(messages[start - 1]):
            start -= 1
        if start < len(messages):
            from llm_router.context_pack import render_message

            request = "\n\n".join(r for r in map(render_message, messages[start:]) if r.strip())
        else:
            request = _trailing_request(messages[-1])
            start = len(messages) - 1
        if request:
            return request, [t for t in map(_history_turn, messages[:start]) if t]
    return (flatten or _flatten)(messages), []


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
    """Interpreter identity of the *running* daemon — lets ``llm-router doctor``
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
async def route(payload: dict, request: Request) -> dict:
    """Minimal native routing endpoint — same contract as ``llm_router.route_server``.

    Body: ``{"prompt", "complexity"?, "system"?, "task_type"?, "max_tokens"?,
    "temperature"?, "model"?, "messages"?}`` → ``{"text","model","provider","cost_usd",
    "input_tokens","output_tokens","complexity"}``. Goes through the same
    ``route_payload`` core as every other endpoint.
    """
    from llm_router.route_server import route_payload_async
    # F14 (D-165 (4)): the request is ``prompt`` (and the Ollama-style ``images`` field);
    # ``messages`` is the history before it and is flattened, never refused. Without this
    # an image part was silently dropped (or, as a non-string prompt, failed as a 502).
    _refuse_images_if_present({"images": payload.get("images")}, payload.get("prompt"))
    # Header wins over the body field; both may be absent.
    _scope = _resolve_project_scope(request, payload.get("project_root"))
    if _scope:
        payload = {**payload, "project_root": _scope}
    else:
        payload = {k: v for k, v in payload.items() if k != "project_root"}
    try:
        return await route_payload_async(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"route failed: {e}")


# ── Native: POST /ground (structural check, no model call) ───────────────────
@app.post("/ground")
async def ground(payload: dict, request: Request) -> dict:
    """Is this draft grounded in the material it was given, and in this repo?

    Body: ``{"draft", "context"?, "prompt"?, "project_root"?}`` ->
    ``{"relayable", "violations", "symbol_violations", "checked"}``.

    The check is STRUCTURAL and calls no model: it asks whether the files and
    symbols a draft cites actually exist. That is why it is worth exposing —
    any host that gets an answer from a cheap model can run it in a few
    milliseconds, with no second inference and no judge, and decide whether to
    relay the answer or do the work itself.

    What it does NOT do is assess correctness. A draft that cites only real
    files can still be wrong about them, so `relayable: true` means "nothing in
    this draft is provably invented", never "this draft is right". Callers that
    treat it as a quality score will be misled, so the field is named for what
    it decides.

    `symbol_violations` is only meaningful when the draft was given repo
    material: the symbol index knows THIS repository, so outside it the absence
    of a symbol is not evidence that the symbol does not exist. The `checked`
    field says which checks actually ran.
    """
    draft = payload.get("draft")
    if not isinstance(draft, str) or not draft.strip():
        raise HTTPException(status_code=400, detail="`draft` is required and must be a non-empty string")
    # Validate BEFORE coercing: `or ""` turns a falsy wrong type ([], 0, {})
    # into a valid empty string, so the type check never sees it and a caller
    # sending the wrong shape gets a clean 200 describing nothing.
    for field in ("context", "prompt"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"`{field}` must be a string")
    context = payload.get("context") or ""
    prompt = payload.get("prompt") or ""

    scope = _resolve_project_scope(request, payload.get("project_root"))
    prior = os.environ.get("LLM_ROUTER_PROJECT_ROOT")
    if scope:
        os.environ["LLM_ROUTER_PROJECT_ROOT"] = scope
    try:
        from llm_router import grounding

        path_violations = grounding.grounding_violations(draft, context, prompt)
        symbol_hits = grounding.symbol_violations(draft, context, prompt)
        relayable = grounding.draft_is_relayable(draft, context, prompt)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"grounding check failed: {e}")
    finally:
        # Restore rather than delete: the process may have had a scope already,
        # and leaking this request's project into the next one is the
        # cross-project contamination that OKF scoping exists to prevent.
        if scope:
            if prior is None:
                os.environ.pop("LLM_ROUTER_PROJECT_ROOT", None)
            else:
                os.environ["LLM_ROUTER_PROJECT_ROOT"] = prior

    return {
        "relayable": relayable,
        "violations": path_violations,
        "symbol_violations": symbol_hits,
        "checked": {
            "paths": True,
            # symbol_violations short-circuits without repo material, so saying
            # it "ran" when it could not would report a clean result it never
            # earned.
            "symbols": "<knowledge_context>" in context,
        },
    }


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list",
            "data": [{"id": "llm_router-auto", "object": "model", "owned_by": "llm_router"}]}


@app.get("/api/tags")  # Ollama model-list shape
def ollama_tags() -> dict:
    return {"models": [{"name": "llm_router-auto", "model": "llm_router-auto"}]}


# ── OpenAI: POST /v1/chat/completions ────────────────────────────────────────
# H-03. A client sending `tools` got a plausible prose reply and no error.
#
# `_OAIRequest` had no `tools` field, so Pydantic discarded it before the handler
# body ran -- no code path could even have logged it. `finish_reason` was the
# literal "stop", so a client could never observe `tool_use` either. An
# OpenAI-compatible client doing function calling therefore received a
# well-formed answer to a question it had not asked, with nothing to indicate
# that its tool definitions had been thrown away.
#
# A.2-t: the three OpenAI / Anthropic endpoints now SERVE tools (see _openai_tools and the
# A.2-t block below); `_TOOLS_UNSUPPORTED` and `_refuse_tools_if_present` remain for the two
# Ollama endpoints. The rest of this comment is the H-03 history.
#
# WHY THIS REFUSED RATHER THAN IMPLEMENTED. The router returned text: `route_and_call`
# has no tool-call channel, and no backend in the chain is wired for one. Building
# that is a feature, not a remediation, and half-building it would produce the
# same silent wrongness in a new place. Refusing is the honest behaviour -- the
# client learns immediately, in its own protocol, that this gateway cannot serve
# the request.
_TOOLS_UNSUPPORTED = (
    "This gateway routes to a text-completion backend and cannot execute tool "
    "or function calls. The request included tool definitions, which would have "
    "been silently ignored, so it is refused instead. Remove `tools` to route "
    "this prompt as a text completion, or call the provider directly for "
    "function calling."
)


def _refuse_tools_if_present(tools, tool_choice=None) -> None:
    """Raise 400 when a request asks for something this gateway cannot do.

    R10. This helper existed and was called from TWO of the five completion
    endpoints. `/v1/responses`, `/api/chat` and `/api/generate` kept the H-03
    defect for the same reason it existed in the first place: the fix was
    applied at call sites rather than enumerated over the surface.

    `tests/test_r10_refuse_what_cannot_be_served.py` now DISCOVERS every POST
    route on the app and requires each to be classified — a completion endpoint
    that must refuse, or an explicitly excused non-completion route. A route
    added later is unclassified and fails.
    """
    if tools:
        raise HTTPException(status_code=400, detail=_TOOLS_UNSUPPORTED)
    if tool_choice not in (None, "none"):
        raise HTTPException(status_code=400, detail=_TOOLS_UNSUPPORTED)


# P2.4 ext-a. `_content_text` keeps only the `text` of a content-part list, so an image
# part in the request was dropped without a word and the model answered a question about
# an image it never saw: the same silent wrongness R10 closed for `tools`. `route_and_call`
# has no image channel, so the gateway refuses instead. D-165 (4): only an image in the
# NEWEST user turn (the request) is refused; an image only in earlier turns keeps the
# handling it had before P2.4: the history is flattened (the context pack renders the
# part as `[image]`) and the request routes as text.
_IMAGES_UNSUPPORTED = (
    "This gateway routes to a text-completion backend and cannot read images. "
    "The request included image content, which would have been silently ignored, "
    "so it is refused instead. Remove the image parts to route this prompt as text."
)

_IMAGE_PART_TYPES = ("image", "image_url", "input_image", "image_file")
# Role-less content parts at the top of a Responses `input` list are one user turn.
_BARE_PART_TYPES = _IMAGE_PART_TYPES + ("text", "input_text", "input_file")


def _has_image(value) -> bool:
    """True when *value* (a content, a message, or a list of either) carries an image part."""
    if isinstance(value, list):
        return any(_has_image(v) for v in value)
    if isinstance(value, dict):
        if value.get("type") in _IMAGE_PART_TYPES or value.get("images"):
            return True
        return _has_image(value.get("content"))
    return False


def _newest_user_turn(value):
    """The newest user turn of a request field: the trailing run of role-less content
    parts (a bare Responses part list), else the last item whose role is ``user`` (a
    role-less typed item, such as a tool output, counts as the caller's). A dict (the
    Ollama generate ``images`` field) is the request itself; anything else has none."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, list):
        return None
    i = len(value)
    while i > 0 and isinstance(value[i - 1], dict) and "role" not in value[i - 1] \
            and value[i - 1].get("type") in _BARE_PART_TYPES:
        i -= 1
    if i < len(value):
        return value[i:]
    for item in reversed(value):
        if isinstance(item, dict) and item.get("role", "user") == "user":
            return item
    return None


def _refuse_images_if_present(*values) -> None:
    """Raise 400 when the newest user turn of any request field carries image content
    (P2.4 ext-a, R10 class; D-165 (4): images only in history are flattened, not refused).

    Called with the messages, the Responses ``input`` or the Ollama ``images`` field of
    every completion endpoint; `tests/test_r10_refuse_what_cannot_be_served.py` and
    `tests/test_p24_empty_chain_table.py` discover the routes."""
    if any(_has_image(_newest_user_turn(v)) for v in values):
        raise HTTPException(status_code=400, detail=_IMAGES_UNSUPPORTED)


# ── A.2-t: tools on the three OpenAI / Anthropic endpoints ───────────────────
# Each wire format is normalised to the OpenAI chat form (the router's one tools shape,
# and what LiteLLM sends every provider) and the reply is rendered back in the caller's
# own format. Only function tools exist here: a hosted tool (web_search, file_search,
# code_interpreter, an Anthropic server tool) runs on the vendor's side and this gateway
# cannot run it, so it is refused rather than dropped (the R10 rule).
def _tools_400(what: str) -> HTTPException:
    return HTTPException(status_code=400, detail=(
        f"This gateway serves function tools only; {what} cannot be served and would have "
        "been silently ignored, so the request is refused. Remove it, or call the provider "
        "directly."))


_TOOL_CHOICE_WORDS = ("none", "auto", "required")


def _function_tool(name, description, parameters, strict=None) -> dict:
    fn: dict = {"name": name, "parameters": parameters or {"type": "object", "properties": {}}}
    if description:
        fn["description"] = description
    if strict is not None:
        fn["strict"] = strict
    return {"type": "function", "function": fn}


def _normalise_tools(tools, tool_choice, convert, convert_choice) -> tuple[list | None, object | None]:
    """``(tools, tool_choice)`` in OpenAI chat form, or ``(None, None)`` for a request
    without tools. ``tool_choice`` other than ``none`` with no tools is refused, as it
    was before A.2 and as OpenAI's own API does: the caller is asking to be forced into
    a call it never offered."""
    if not tools:
        if tool_choice not in (None, "none"):
            raise _tools_400("`tool_choice` without `tools`")
        return None, None
    if not isinstance(tools, list):
        raise _tools_400("a `tools` value that is not a list")
    out = []
    for t in tools:
        if not isinstance(t, dict):
            raise _tools_400("a tool that is not an object")
        out.append(convert(t))
    return out, (convert_choice(tool_choice) if tool_choice is not None else None)


def _named_choice(name) -> dict:
    return {"type": "function", "function": {"name": name}}


def _openai_choice(c):
    if c in _TOOL_CHOICE_WORDS:
        return c
    if isinstance(c, dict) and c.get("type") == "function" and \
            isinstance((c.get("function") or {}).get("name"), str):
        return _named_choice(c["function"]["name"])
    raise _tools_400(f"`tool_choice` {json.dumps(c, default=str)[:80]}")


def _openai_tool(t: dict) -> dict:
    fn = t.get("function")
    if t.get("type") != "function" or not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
        raise _tools_400(f"tool type {t.get('type')!r}")
    return t


def _openai_tools(tools, tool_choice):
    """Chat Completions: already the OpenAI form; validated."""
    return _normalise_tools(tools, tool_choice, _openai_tool, _openai_choice)


def _responses_tool(t: dict) -> dict:
    if t.get("type") != "function" or not isinstance(t.get("name"), str):
        raise _tools_400(f"tool type {t.get('type')!r}")
    return _function_tool(t["name"], t.get("description"), t.get("parameters"), t.get("strict"))


def _responses_choice(c):
    if c in _TOOL_CHOICE_WORDS:
        return c
    if isinstance(c, dict) and c.get("type") == "function":
        name = c.get("name") or (c.get("function") or {}).get("name")
        if isinstance(name, str):
            return _named_choice(name)
    raise _tools_400(f"`tool_choice` {json.dumps(c, default=str)[:80]}")


def _responses_tools(tools, tool_choice):
    """Responses API: flat ``{"type": "function", "name", "parameters"}`` items."""
    return _normalise_tools(tools, tool_choice, _responses_tool, _responses_choice)


def _anthropic_tool(t: dict) -> dict:
    if t.get("type") not in (None, "custom") or not isinstance(t.get("name"), str):
        raise _tools_400(f"tool type {t.get('type')!r}")
    return _function_tool(t["name"], t.get("description"), t.get("input_schema"))


def _anthropic_choice(c):
    kind = c.get("type") if isinstance(c, dict) else None
    if kind in ("auto", "none"):
        return kind
    if kind == "any":
        return "required"
    if kind == "tool" and isinstance(c.get("name"), str):
        return _named_choice(c["name"])
    raise _tools_400(f"`tool_choice` {json.dumps(c, default=str)[:80]}")


def _anthropic_tools(tools, tool_choice):
    """Messages API: ``{"name", "description", "input_schema"}`` items."""
    return _normalise_tools(tools, tool_choice, _anthropic_tool, _anthropic_choice)


def _tool_input(tc: dict) -> dict:
    """The ``input`` object of an Anthropic ``tool_use`` block: the call's JSON arguments
    parsed (OpenAI carries them as a string, Anthropic as an object)."""
    raw = (tc.get("function") or {}).get("arguments") or ""
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise HTTPException(status_code=502, detail=(
            f"the model returned arguments for tool {(tc.get('function') or {}).get('name')!r} "
            "that are not a JSON object"))
    return value


STREAM_UNSUPPORTED = "streaming not supported yet (v16 A.2)"


def _refuse_stream(stream: bool) -> None:
    """P0.6 (R-AGT-3): ``stream: true`` is refused, not silently dropped.

    The field was undeclared, so Pydantic discarded it and the client got one
    JSON body where it expected an SSE stream. The OpenAI and Anthropic SDKs
    then fail inside their stream parser, far from the cause. Until streaming
    exists (A.2) the gateway says so in the client's own protocol.
    """
    if stream:
        raise HTTPException(status_code=400, detail=STREAM_UNSUPPORTED)


def _ollama_options(options: dict | None) -> tuple[int | None, float | None]:
    """``(max_tokens, temperature)`` from Ollama's ``options`` (num_predict, temperature)."""
    if not isinstance(options, dict):
        return None, None
    n = options.get("num_predict")
    # Ollama's -1 / -2 mean "no limit" / "fill context": not a cap to forward.
    return (n if isinstance(n, int) and n > 0 else None), options.get("temperature")


def _finish_reason(result) -> str:
    """The real reason, where the backend reports one.

    Still "stop" for an ordinary completion -- which is correct -- but derived
    rather than asserted, so a truncated answer is not reported as a complete
    one.
    """
    if getattr(result, "tool_calls", None):
        # A.2-t: some backends report "stop" beside a tool call; the caller must see
        # "tool_calls" or it never runs the tool.
        return "tool_calls"
    raw = getattr(result, "finish_reason", None) or getattr(result, "stop_reason", None)
    if isinstance(raw, str) and raw:
        return {"max_tokens": "length", "end_turn": "stop"}.get(raw, raw)
    # R15: this claim — "derived rather than asserted" — was false for every
    # response, because `LLMResponse` carried no `finish_reason` field at all
    # and this line was the only branch that ever ran. `providers.call_llm`
    # now populates it, so the derivation is real.
    #
    # The fallback stays "stop" ONLY because the OpenAI schema has no way to
    # say "the backend did not tell us": `finish_reason` is not nullable in
    # any client that parses it, and emitting "" or null breaks them. It is a
    # protocol obligation, not an assertion of success — and it is why the
    # bandit reads `response.finish_reason` directly rather than this function.
    return "stop"


class _OAIRequest(BaseModel):
    model: str | None = None
    messages: list
    task_type: str | None = None
    complexity: str | None = None
    # H-03: declared so the field is VISIBLE to the handler. Previously absent,
    # so Pydantic dropped it and the request looked like an ordinary completion.
    tools: list | None = None
    tool_choice: object | None = None
    # P0.6: declared so they are seen. `stream` is refused (A.2 builds it); the
    # rest are forwarded. `max_completion_tokens` is OpenAI's newer name.
    stream: bool = False
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    temperature: float | None = None
    # A.2-t: declared so they are seen. Accepted for client compatibility; the router
    # does not act on them (it returns the calls the model made, in one reply).
    parallel_tool_calls: bool | None = None
    stream_options: dict | None = None
    store: bool | None = None


@app.post("/v1/chat/completions")
async def openai_chat(req: _OAIRequest, request: Request) -> dict:
    _refuse_stream(req.stream)
    tools, tool_choice = _openai_tools(req.tools, req.tool_choice)
    _refuse_images_if_present(req.messages)
    system, messages = _split_system(req.messages)
    prompt, history = _request_and_history(messages)
    r = await _route(prompt, req.task_type, req.complexity,
                     prefer_model=_qualify_model(req.model, "openai"),
                     project_root=_resolve_project_scope(request),
                     classify_text=_latest_user_turn(req.messages),
                     system=system,
                     max_tokens=req.max_completion_tokens or req.max_tokens,
                     temperature=req.temperature, messages=history,
                     tools=tools, tool_choice=tool_choice)
    message: dict = {"role": "assistant", "content": r.text}
    if r.tool_calls:
        message = {"role": "assistant", "content": r.text or None, "tool_calls": r.tool_calls}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": f"{r.model.provider}/{r.model.model}",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": _finish_reason(r)}],
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
    # R10. H-03 was fixed on /v1/chat/completions and /v1/messages and NOT
    # here, because the fix was applied endpoint by endpoint and nothing
    # enumerated the endpoints. The Responses API is OpenAI's current function-
    # calling surface, so this was the likeliest of the three to be hit.
    tools: list | None = None
    tool_choice: object | None = None
    stream: bool = False  # P0.6: refused until A.2
    max_output_tokens: int | None = None
    temperature: float | None = None
    # A.2-t: declared so they are seen; accepted, not acted on (see _OAIRequest).
    parallel_tool_calls: bool | None = None
    store: bool | None = None
    include: list | None = None


@app.post("/v1/responses")
async def openai_responses(req: _ResponsesRequest, request: Request) -> dict:
    _refuse_stream(req.stream)
    tools, tool_choice = _responses_tools(req.tools, req.tool_choice)
    _refuse_images_if_present(req.input)
    # `instructions` is the Responses API's system prompt; system/developer
    # items in `input` are too. Both go to the model as its system prompt
    # (P0.6), and neither is classified (T-03, see _route).
    input_system, input_rest = _split_system(req.input)
    if isinstance(input_rest, str):
        prompt, history = input_rest, []
    else:
        prompt, history = _request_and_history(input_rest, _flatten_responses_input)
    classify_text = _latest_user_turn_from_responses_input(req.input)
    r = await _route(prompt, req.task_type, req.complexity,
                     prefer_model=_qualify_model(req.model, "openai"),
                     project_root=_resolve_project_scope(request),
                     classify_text=classify_text,
                     system=_join_system(req.instructions, input_system),
                     max_tokens=req.max_output_tokens,
                     temperature=req.temperature, messages=history,
                     tools=tools, tool_choice=tool_choice)
    output_id = f"msg_{uuid.uuid4().hex[:24]}"
    output = []
    if r.text or not r.tool_calls:
        output.append({
            "id": output_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": r.text, "annotations": []}],
        })
    for tc in r.tool_calls:
        output.append({
            "id": f"fc_{uuid.uuid4().hex[:24]}",
            "type": "function_call",
            "status": "completed",
            "call_id": tc["id"],
            "name": tc["function"]["name"],
            "arguments": tc["function"]["arguments"],
        })
    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": f"{r.model.provider}/{r.model.model}",
        "output": output,
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
    # H-03: same gap on the Anthropic wire format.
    tools: list | None = None
    tool_choice: object | None = None
    stream: bool = False  # P0.6: refused until A.2
    temperature: float | None = None


@app.post("/v1/messages")
async def anthropic_messages(req: _AnthropicRequest, request: Request) -> dict:
    _refuse_stream(req.stream)
    tools, tool_choice = _anthropic_tools(req.tools, req.tool_choice)
    _refuse_images_if_present(req.messages)
    # `req.system` is the model's system prompt and is never classified (T-03).
    prompt, history = _request_and_history(req.messages)
    r = await _route(prompt, None, None,
                     prefer_model=_qualify_model(req.model, "anthropic"),
                     project_root=_resolve_project_scope(request),
                     classify_text=_latest_user_turn(req.messages),
                     system=req.system or None,
                     max_tokens=req.max_tokens,
                     temperature=req.temperature, messages=history,
                     tools=tools, tool_choice=tool_choice)
    content: list = [{"type": "text", "text": r.text}] if r.text or not r.tool_calls else []
    content += [{"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"],
                 "input": _tool_input(tc)} for tc in r.tool_calls]
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": f"{r.model.provider}/{r.model.model}",
        "content": content,
        # H-03: derived where the backend reports one, so a truncated answer is
        # not returned as a completed turn.
        "stop_reason": {"length": "max_tokens", "tool_calls": "tool_use"}.get(
            _finish_reason(r), "end_turn"),
        "usage": {"input_tokens": r.input_tokens, "output_tokens": r.output_tokens},
    }


# ── Ollama: POST /api/chat  and  POST /api/generate ──────────────────────────
class _OllamaChat(BaseModel):
    model: str | None = None
    messages: list
    # R10. Ollama has supported `tools` on /api/chat since 0.3, and a client
    # pointed at this gateway instead of at Ollama gets prose back with
    # `done: true` and nothing to say its tool definitions were dropped.
    tools: list | None = None
    tool_choice: object | None = None
    options: dict | None = None  # P0.6: num_predict / temperature forwarded


class _OllamaGenerate(BaseModel):
    model: str | None = None
    prompt: str
    system: str | None = None  # P0.6: forwarded
    options: dict | None = None
    # /api/generate has no `tools` in Ollama's own API. Declared anyway: a
    # client that sends one is asking for something this gateway cannot do, and
    # silently accepting the request is the defect regardless of whether the
    # upstream API would have accepted it either.
    tools: list | None = None
    tool_choice: object | None = None
    # P2.4 ext-a: Ollama's base64 image list. Declared so the handler sees it (an
    # undeclared field is dropped by Pydantic) and refuses it.
    images: list | None = None


@app.post("/api/chat")
async def ollama_chat(req: _OllamaChat, request: Request) -> dict:
    _refuse_tools_if_present(req.tools, req.tool_choice)
    _refuse_images_if_present(req.messages)
    system, messages = _split_system(req.messages)
    max_tokens, temperature = _ollama_options(req.options)
    prompt, history = _request_and_history(messages)
    r = await _route(prompt, None, None,
                     prefer_model=_qualify_model(req.model, "ollama"),
                     project_root=_resolve_project_scope(request),
                     classify_text=_latest_user_turn(req.messages),
                     system=system, max_tokens=max_tokens, temperature=temperature,
                     messages=history)
    return {
        "model": f"{r.model.provider}/{r.model.model}",
        "message": {"role": "assistant", "content": r.text},
        "done": True,
        "prompt_eval_count": r.input_tokens, "eval_count": r.output_tokens,
    }


@app.post("/api/generate")
async def ollama_generate(req: _OllamaGenerate, request: Request) -> dict:
    _refuse_tools_if_present(req.tools, req.tool_choice)
    _refuse_images_if_present({"images": req.images})
    max_tokens, temperature = _ollama_options(req.options)
    r = await _route(req.prompt, None, None,
                     prefer_model=_qualify_model(req.model, "ollama"),
                     project_root=_resolve_project_scope(request),
                     system=req.system or None,
                     max_tokens=max_tokens, temperature=temperature, messages=[])
    return {
        "model": f"{r.model.provider}/{r.model.model}",
        "response": r.text,
        "done": True,
        "prompt_eval_count": r.input_tokens, "eval_count": r.output_tokens,
    }


def main() -> None:
    # Gateway mode fronts host tools such as Codex/Cursor/Pi. Calling Codex or
    # Gemini CLI again from inside the gateway can recurse or fail under launchd's
    # trimmed PATH, so keep subprocess host backends out unless an operator opts in.
    os.environ.setdefault("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex,gemini_cli")
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
