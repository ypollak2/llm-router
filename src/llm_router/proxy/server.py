"""The proxy app: pass-through to Anthropic, per-step serving, fallback.

Enable for ONE Claude Code session, never globally::

    llm-router proxy --port 8787            # terminal 1
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude    # terminal 2

Auth: the client's ``authorization`` / ``x-api-key`` headers are forwarded to
Anthropic unchanged and are never written anywhere. ``accept-encoding`` is
forced to ``identity`` upstream: httpx otherwise asks for gzip, and relaying
the raw gzip bytes without their header broke Claude Code with "JSON Parse
error" in the spike.

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

from llm_router.proxy import ledger
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
from llm_router.proxy.loop_guard import (
    DEFAULT_MAX_CONSECUTIVE,
    DEFAULT_REPEAT_WINDOW,
    REASON_LOOP_GUARD,
    LoopGuard,
)
from llm_router.proxy.steps import STEP_CLASSES, classify_text, prev_tools, session_id_of, step_class
from llm_router.proxy.translate import (
    has_served_turn,
    is_thinking_rejection,
    parse_sse_usage,
    sse_from_message,
    without_thinking,
)

ANTHROPIC_UPSTREAM = "https://api.anthropic.com"
DEFAULT_PORT = 8787
DEFAULT_STEP_BUDGET_S = 30.0
DEFAULT_NUM_CTX = 32768
_HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding",
        "keep-alive", "proxy-authorization", "te", "trailer", "upgrade"}


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
        )


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


def build_app(cfg: ProxyConfig, *, client=None, backend_factory=None):
    """The Starlette app. ``client`` (an ``httpx.AsyncClient``) and
    ``backend_factory(model) -> Backend`` are injectable for tests."""
    import httpx
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response, StreamingResponse
    from starlette.routing import Route

    upstream = validate_upstream(cfg.upstream)
    trims = resolve_trims(cfg.trim)
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    guard = LoopGuard(cfg.loop_max_consecutive, cfg.loop_repeat_window)

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

    def write(row: dict) -> None:
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
        message, err, reason = None, None, None
        try:
            backend = make_backend(choice["model"])
            message, err, backend_usage = await asyncio.wait_for(
                backend.complete(apply_trims(body, trims), cfg.step_budget_s), timeout=cfg.step_budget_s)
            row["backend_usage"] = backend_usage
            if err:
                reason = "validation"
        except HedgeTimeout as exc:
            err, reason = str(exc), "hedge_timeout"
        except (asyncio.TimeoutError, httpx.TimeoutException):
            err, reason = f"exceeded step budget {cfg.step_budget_s}s", "budget_exceeded"
        except Exception as exc:  # noqa: BLE001 - any backend failure is a counted fallback
            err, reason = f"{type(exc).__name__}: {exc}", "backend_error"
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

    async def forward(request: Request, raw: bytes, body: dict | None, row: dict | None):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        headers["accept-encoding"] = "identity"
        url = upstream + request.url.path + (("?" + request.url.query) if request.url.query else "")
        t0 = time.monotonic()

        async def send(content: bytes):
            req = http.build_request(request.method, url, headers=headers, content=content)
            return await http.send(req, stream=True)

        try:
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
        else:
            try:
                data = json.loads(buf or b"{}")
            except ValueError:
                data = {}
            usage = data.get("usage") if isinstance(data, dict) else None
            stop = data.get("stop_reason") if isinstance(data, dict) else None
            msg_id = data.get("id") if isinstance(data, dict) else None
        row["usage"] = ledger.normalize_usage(usage)
        row["stop_reason"] = stop
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
        return await forward(request, raw, body, row)

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

USAGE = """\
llm-router proxy [--port N] [--steps continuation|off] [--step-budget-s S]
                 [--hedge-s S|off] [--model ollama/TAG] [--trim NAME[,NAME]]
                 [--num-ctx N] [--ollama-url URL] [--keep-alive -1|5m] [--no-warm-up]
                 [--loop-max-consecutive N] [--loop-repeat-window N]
llm-router proxy stats [--days N] [--json]

Opt-in, per session. Nothing is enabled until you point a session at it:
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
"""


def cmd_proxy(argv: list[str]) -> int:
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
                          loop_repeat_window=a.loop_repeat_window)
        app = build_app(cfg)
    except ValueError as exc:
        sys.stderr.write(f"llm-router proxy: {exc}\n")
        return 2

    import uvicorn

    steps = ",".join(sorted(cfg.steps)) or "off (pass-through only)"
    print(f"llm-router proxy -> http://{a.host}:{a.port}  steps={steps}  "
          f"hedge={cfg.hedge_s}s  budget={cfg.step_budget_s}s  trim={cfg.trim or 'fast'}  "
          f"loop_guard(max_consecutive={cfg.loop_max_consecutive}, repeat_window={cfg.loop_repeat_window})")
    print(f"  enable per session: ANTHROPIC_BASE_URL=http://{a.host}:{a.port} claude")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning", access_log=False)
    return 0
